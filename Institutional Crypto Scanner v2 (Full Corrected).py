import asyncio
import json
import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Any, Optional

import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
from tabulate import tabulate
import websockets

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s [%(threadName)s] - %(message)s') # Added threadName

@dataclass
class Config:
    """Central configuration object."""
    # --- Market & Universe ---
    exchanges: List[str] = field(default_factory=lambda: ["binance"])
    market_type: str = "swap"
    quote_currency: str = "USDT"
    blacklist: List[str] = field(default_factory=lambda: ["USDC/USDT", "BUSD/USDT", "TUSD/USDT"])

    # --- Data & Features ---
    timeframe: str = "15m"
    lookback_period: int = 150
    z_score_window: int = 21
    bollinger_config: Dict[str, Any] = field(default_factory=lambda: {"len": 20, "std": 2.0})
    atr_config: Dict[str, Any] = field(default_factory=lambda: {"period": 14, "sl_multiplier": 2.0, "tp_multiplier": 4.0})
    swing_config: Dict[str, Any] = field(default_factory=lambda: {"lookback": 50})

    # --- Microstructure Engine Config ---
    oi_fetch_interval_sec: int = 60
    # FIX: Adjust OI concurrency and timeout again
    oi_fetch_concurrency: int = 12 # Increased concurrency slightly
    oi_semaphore_timeout_sec: int = 55 # Increased wait time significantly (close to interval)
    obi_levels: int = 5
    l2_stream_name: str = "depth5@100ms"
    websocket_batch_size: int = 100

    # --- Scoring & Calibration ---
    score_confidence_factor: float = 0.5
    ignition_obi_threshold: float = 0.05
    ignition_oi_delta_threshold_pct: float = 0.01
    score_weight_ignition: float = 0.4
    score_weight_specificity: float = 0.6
    spec_score_weight_oi: float = 2.0
    spec_score_weight_obi: float = 1.0
    spec_score_weight_cvd_div: float = 0.0

    # --- Risk & TP/SL ---
    tp_sl_strategy: str = "atr"
    adv_gate_lookback: int = 20
    adv_gate_min_dollar_volume: float = 1_000_000.0

    # --- Execution ---
    scan_interval_seconds: int = 60
    top_n_candidates: int = 5
    max_concurrent_ohlcv_fetches: int = 10

    # --- Liquidation WS Config ---
    liq_window_sec: int = 60 * 30
    liq_price_range_pct: float = 0.08
    liq_bin_step_pct: float = 0.0025
    liq_top_nodes: int = 4
    exchanges_liq: List[str] = field(default_factory=lambda: ["binance"])


# --- DATA ENGINE (OHLCV Only) ---
class DataEngine:
    def __init__(self, exchange: ccxt.Exchange): self.exchange = exchange
    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        symbol_formatted = symbol
        try:
            market_map = getattr(self.exchange, 'markets_by_id', None) or getattr(self.exchange, 'markets', None)
            if '/' not in symbol and market_map and symbol in market_map:
                symbol_formatted = market_map[symbol]['symbol']

            ohlcv = await self.exchange.fetch_ohlcv(symbol_formatted, timeframe, limit=limit)
            if not ohlcv: return None
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            if df.empty or 'close' not in df.columns or df['close'].isna().all(): return None
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
            return df if not df.empty else None
        except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeError) as e:
            # logging.debug(f"[DataEngine] Error fetching OHLCV for {symbol_formatted}: {type(e).__name__}")
            return None
        except Exception as e:
            logging.error(f"[DataEngine] Unexpected OHLCV error {symbol_formatted}: {e}", exc_info=False)
            return None

# --- MICROSTRUCTURE ENGINE (OI, CVD, OBI) ---
class MicrostructureEngine:
    def __init__(self, cfg: Config, symbols_ccxt: List[str]):
        self.cfg, self.symbols_ccxt = cfg, symbols_ccxt
        self.symbols_ws = [s.replace('/', '').lower() for s in symbols_ccxt]
        self.exchange_id = cfg.exchanges[0]
        self.exchange, self.loop, self._stop_event = None, None, None
        self._lock = threading.Lock()
        self._oi = defaultdict(lambda: {'openInterestValue': 0.0, 'timestamp': 0})
        self._oi_prev = defaultdict(lambda: {'openInterestValue': 0.0, 'timestamp': 0})
        self._cvd = defaultdict(float)
        self._obi = defaultdict(float)
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="MicrostructureThread")

    def start(self):
        if not self._thread.is_alive():
            logging.info("Starting MicrostructureEngine...")
            self._thread.start()

    async def stop(self):
        if self.loop and self._thread.is_alive() and self._stop_event:
            logging.info("Stopping MicrostructureEngine...")
            if asyncio.get_running_loop() != self.loop: self.loop.call_soon_threadsafe(self._stop_event.set)
            else: self._stop_event.set()
            await asyncio.sleep(2)
            logging.info("MicrostructureEngine stop signal sent.")

    def _run_forever(self):
        self.loop = asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        self._stop_event = asyncio.Event()
        try:
             self.exchange = getattr(ccxt, self.exchange_id)({
                  'asyncio_loop': self.loop, 'options': {'defaultType': self.cfg.market_type},
                  'enableRateLimit': True, 'rateLimit': 150,
                  'timeout': 30000, 'connectTimeout': 15000
             })
             logging.info(f"[MicroEngine] Initialized internal CCXT: {self.exchange.id}")
             self.loop.run_until_complete(self.exchange.load_markets(True))
             logging.info(f"[MicroEngine] Loaded internal markets.")

             ws_tasks, batch_size = [], self.cfg.websocket_batch_size
             logging.info(f"[MicroEngine] Creating WS tasks (batch: {batch_size})...")
             for i in range(0, len(self.symbols_ws), batch_size):
                 batch = self.symbols_ws[i:i + batch_size]
                 ws_tasks.extend([self._trade_stream_batch_loop(batch), self._l2_stream_batch_loop(batch)])
             oi_task = self._oi_loop(self.symbols_ccxt)
             all_tasks = ws_tasks + [oi_task]
             main_tasks_future = asyncio.gather(*all_tasks, return_exceptions=True)
             stop_waiter = self.loop.create_task(self._stop_event.wait(), name="MicroStopWaiter")
             logging.info(f"[MicroEngine] Starting {len(all_tasks)} background tasks.")

             done, pending = self.loop.run_until_complete(asyncio.wait(
                  [main_tasks_future, stop_waiter], return_when=asyncio.FIRST_COMPLETED
             ))
             if main_tasks_future in done:
                  res = main_tasks_future.result(); [logging.error(f"[MicroEngine] Task {i} failed: {r}", exc_info=False) for i,r in enumerate(res) if isinstance(r,Exception)]
             if stop_waiter in done: logging.info("[MicroEngine] Stop received.")

             logging.info("[MicroEngine] Cancelling tasks..."); cancelled=0
             for task in pending:
                  if not task.done(): task.cancel(); cancelled+=1
             if cancelled > 0: self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True)); logging.info(f"[MicroEngine] {cancelled} tasks cancelled.")
        except Exception as e: logging.critical(f"[MicroEngine] Loop error: {e}", exc_info=True)
        finally:
             if self.exchange:
                  logging.info("[MicroEngine] Closing internal CCXT...")
                  try: self.loop.run_until_complete(self.exchange.close())
                  except Exception as e: logging.warning(f"[MicroEngine] Error closing internal exchange: {e}")
             logging.info("[MicroEngine] Background loop finished.")
             if self.loop and not self.loop.is_closed(): self.loop.close(); self.loop = None

    async def _get_oi_internal(self, symbol: str) -> Dict[str, Any]:
        default_res = {'openInterestValue': 0.0, 'timestamp': 0}
        try:
            params = {}
            if self.exchange and self.exchange.markets:
                market = self.exchange.market(symbol)
                if market:
                    m_type = market.get('type')
                    if self.exchange.id == 'binance':
                        if market.get('inverse'): params['type'] = 'inverse'
                        elif market.get('linear'): params['type'] = 'linear'
                        elif m_type == 'future': params['type'] = 'future'
                        elif m_type == 'swap': params['type'] = 'linear'

            if not self.exchange or not self.exchange.has['fetchOpenInterest']: return default_res
            oi_data = await self.exchange.fetch_open_interest(symbol, params=params)
            oi_v = oi_data.get('openInterestValue') or oi_data.get('info',{}).get('openInterest') or oi_data.get('info',{}).get('sumOpenInterestValue') or 0.0
            ts = oi_data.get('timestamp', int(time.time() * 1000))
            try: oi_f = float(oi_v)
            except: oi_f = 0.0
            return {'openInterestValue': oi_f, 'timestamp': ts}
        except (ccxt.NetworkError, ccxt.ExchangeError, ccxt.NotSupported, ccxt.RequestTimeout) as e:
             # logging.debug(f"[MicroEngine] OI fetch error ({symbol}): {type(e).__name__}")
             return default_res
        except Exception as e:
            logging.error(f"[MicroEngine] Unexpected OI fetch error ({symbol}): {type(e).__name__}", exc_info=False)
            return default_res

    async def _oi_loop(self, symbols: List[str]):
        oi_sem = asyncio.Semaphore(self.cfg.oi_fetch_concurrency)
        await asyncio.sleep(5)
        while not self._stop_event.is_set():
            start_t = time.time()
            try:
                logging.info(f"[MicroEngine] Fetching OI for {len(symbols)} symbols (concurrency: {self.cfg.oi_fetch_concurrency})...")
                tasks = []
                async def fetch_one(sym):
                     try:
                          # FIX: Use configured semaphore timeout
                          async with asyncio.timeout(self.cfg.oi_semaphore_timeout_sec):
                              async with oi_sem:
                                   if self._stop_event.is_set(): return None
                                   # Removed explicit asyncio.timeout() around fetch call
                                   return await self._get_oi_internal(sym)
                     except asyncio.TimeoutError:
                          logging.warning(f"[MicroEngine] Timeout waiting for OI semaphore: {sym}")
                          return None
                     except Exception as e_inner:
                          logging.error(f"[MicroEngine] Error in fetch_one({sym}): {e_inner}")
                          return None

                for sym in symbols:
                     if self._stop_event.is_set(): break
                     tasks.append(fetch_one(sym))
                if self._stop_event.is_set(): break

                results = await asyncio.wait_for(
                     asyncio.gather(*tasks, return_exceptions=True),
                     timeout=max(30.0, self.cfg.oi_fetch_interval_sec * 0.95)
                )

                new_oi, ok_cnt, err_cnt = {}, 0, 0
                default_res_oi = {'openInterestValue': 0.0, 'timestamp': 0}
                for sym, res in zip(symbols, results):
                    if isinstance(res, Exception) or res is None:
                        err_cnt += 1; new_oi[sym] = self._oi.get(sym, default_res_oi)
                    elif isinstance(res, dict):
                        ok_cnt += 1; new_oi[sym] = res
                    else: err_cnt += 1; new_oi[sym] = self._oi.get(sym, default_res_oi)

                duration = time.time() - start_t
                with self._lock: self._oi_prev = self._oi.copy(); self._oi = defaultdict(lambda: default_res_oi, new_oi)

                log_lvl = logging.WARNING if err_cnt > len(symbols) * 0.3 else logging.INFO
                logging.log(log_lvl, f"[MicroEngine] OI Update: {ok_cnt} ok, {err_cnt} failed in {duration:.2f}s.")

                sleep = max(1.0, self.cfg.oi_fetch_interval_sec - duration)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep)
                    break
                except asyncio.TimeoutError:
                    pass

            except asyncio.TimeoutError: logging.warning("[MicroEngine] OI gather timed out."); await asyncio.sleep(5)
            except asyncio.CancelledError: break
            except Exception as e: logging.error(f"[MicroEngine] OI loop error: {e}", exc_info=True); await asyncio.sleep(30)
        logging.info("[MicroEngine] OI loop finished.")

    def _get_multiplex_url(self, streams: List[str]) -> Optional[str]:
        if not streams: return None
        path = '/'.join(streams); url = f"wss://fstream.binance.com/stream?streams={path}"
        if len(url) > 8100: logging.error(f"[MicroEngine] WS URL > 8100 ({len(url)}). Reduce batch?"); return None
        return url

    async def _websocket_handler(self, url: str, proc_fn: callable, label: str):
        if not url: logging.error(f"[MicroEngine] No URL for {label}."); return
        delay = 5
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=10, open_timeout=30) as ws:
                    logging.info(f"[MicroEngine] Connected: {label} ({url[:60]}...)."); delay = 5
                    while not self._stop_event.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=70)
                            try: proc_fn(json.loads(msg))
                            except json.JSONDecodeError: pass
                            except Exception as e: logging.error(f"[MicroEngine] Proc error ({label}): {e}", exc_info=False)
                        except asyncio.TimeoutError:
                            try: await asyncio.wait_for(await ws.ping(), timeout=10)
                            except asyncio.TimeoutError: logging.warning(f"[MicroEngine] Ping FAILED ({label}). Reconnecting..."); break
            except asyncio.CancelledError: break
            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, asyncio.TimeoutError): pass
            except websockets.exceptions.InvalidURI: logging.error(f"[MicroEngine] Invalid URI ({label}). Stopping."); break
            except websockets.exceptions.InvalidStatusCode as e:
                logging.error(f"[MicroEngine] WS status {e.status_code} ({label}). Retrying...");
                if e.status_code == 414: logging.critical(f"FATAL: HTTP 414 ({label}). Reduce batch_size! Stopping."); break
            except OSError as e: logging.error(f"[MicroEngine] OS error ({label}): {e}. Retrying...")
            except Exception as e: logging.error(f"[MicroEngine] WS error ({label}): {e}. Retrying...", exc_info=True)
            if self._stop_event.is_set(): break
            try: await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError: delay = min(delay * 1.5, 60)
            else: break
        logging.info(f"[MicroEngine] WS handler stopped: {label}.")

    def _process_trade(self, data: Dict):
        d = data.get('data'); sym = d.get('s') if isinstance(d, dict) else None
        q_str = d.get('q') if isinstance(d, dict) else None
        m_bool = d.get('m') if isinstance(d, dict) else None # 'm' is boolean
        if not sym or q_str is None or m_bool is None: return
        try:
            q = float(q_str)
            delta = q if not m_bool else -q
            if math.isfinite(delta):
                with self._lock: self._cvd[sym.upper()] += delta
        except (ValueError, TypeError): pass

    async def _trade_stream_batch_loop(self, batch: List[str]):
        streams = [f"{s.lower()}@aggTrade" for s in batch]; url = self._get_multiplex_url(streams)
        label = f"Trade Batch ({len(batch)} syms, e.g., {batch[0] if batch else 'N/A'})"; await self._websocket_handler(url, self._process_trade, label)

    def _process_l2(self, data: Dict):
        d=data.get('data'); sym=d.get('s'); bids=d.get('b',[]); asks=d.get('a',[])
        if not isinstance(d, dict) or not sym or not bids or not asks: return
        obi=self._calc_obi(bids, asks);
        if math.isfinite(obi):
            with self._lock:
                self._obi[sym.upper()] = obi
    async def _l2_stream_batch_loop(self, batch: List[str]):
        streams = [f"{s.lower()}@{self.cfg.l2_stream_name}" for s in batch]; url = self._get_multiplex_url(streams)
        label = f"L2 Batch ({len(batch)} syms, e.g., {batch[0] if batch else 'N/A'})"; await self._websocket_handler(url, self._process_l2, label)

    def _calc_obi(self, bids: List, asks: List) -> float:
        try:
            b_vol=sum(float(l[1]) for l in bids[:self.cfg.obi_levels] if isinstance(l,(list,tuple)) and len(l)==2 and float(l[1])>0)
            a_vol=sum(float(l[1]) for l in asks[:self.cfg.obi_levels] if isinstance(l,(list,tuple)) and len(l)==2 and float(l[1])>0)
            t_vol=b_vol+a_vol; return (b_vol-a_vol)/t_vol if t_vol>1e-9 else 0.0
        except: return 0.0

    def get_microstructure_signals(self, sym_ccxt: str) -> Dict[str, Any]:
        sym_ws=sym_ccxt.replace('/','').upper(); default_res={'openInterestValue':0.0,'timestamp':0}
        with self._lock:
            oi_d=self._oi.get(sym_ccxt, default_res); oi_p_d=self._oi_prev.get(sym_ccxt, default_res)
            oi,oi_p=oi_d['openInterestValue'],oi_p_d['openInterestValue']
            cvd=self._cvd.get(sym_ws,0.0); obi=self._obi.get(sym_ws,0.0)
        oi_delta=(oi-oi_p)/oi_p if oi_p>1e-9 else 0.0
        return {"oi_delta_pct": oi_delta, "perp_cvd": cvd, "obi": obi, "cvd_spot_perp_divergence": 0.0}

# --- FEATURE ENGINE (Uses ffill) ---
class FeatureEngine:
    def __init__(self, cfg: Config): self.cfg = cfg
    def engineer_features(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        req_len = max(self.cfg.bollinger_config["len"], self.cfg.z_score_window, self.cfg.atr_config["period"], self.cfg.swing_config["lookback"], 1)
        if df is None or len(df) < req_len: return None
        try:
            required=['open','high','low','close','volume']
            if not all(c in df.columns for c in required): return None
            for c in required: df[c]=pd.to_numeric(df[c], errors='coerce')
            df.dropna(subset=['open','high','low','close'], inplace=True)
            if len(df) < req_len: return None

            bb_len, bb_std=self.cfg.bollinger_config["len"], self.cfg.bollinger_config["std"]; min_p_bb=max(1,bb_len//2)
            df['bb_ma']=df['close'].rolling(bb_len,min_periods=min_p_bb).mean()
            df['bb_std']=df['close'].rolling(bb_len,min_periods=min_p_bb).std()
            df['bb_upper']=df['bb_ma']+(df['bb_std']*bb_std); df['bb_lower']=df['bb_ma']-(df['bb_std']*bb_std)
            df['bb_width']=(df['bb_upper']-df['bb_lower'])/df['bb_ma'].replace(0,np.nan)

            z_win=self.cfg.z_score_window; min_p_z=max(1,z_win//2)
            c_m=df['close'].rolling(z_win,min_periods=min_p_z).mean(); c_s=df['close'].rolling(z_win,min_periods=min_p_z).std().replace(0,np.nan)
            v_m=df['volume'].rolling(z_win,min_periods=min_p_z).mean(); v_s=df['volume'].rolling(z_win,min_periods=min_p_z).std().replace(0,np.nan)
            df['z_score_price']=(df['close']-c_m)/c_s; df['z_score_volume']=(df['volume']-v_m)/v_s

            atr_p=self.cfg.atr_config["period"]; min_p_atr=max(1,atr_p//2)
            hl=df['high']-df['low']; hc=abs(df['high']-df['close'].shift()); lc=abs(df['low']-df['close'].shift())
            tr=np.maximum(hl,np.maximum(hc,lc)); tr.fillna(0,inplace=True)
            df['atr']=tr.ewm(span=atr_p,adjust=False,min_periods=min_p_atr).mean()

            sw_lb=min(self.cfg.swing_config["lookback"],len(df)); min_p_sw=max(1,sw_lb//2)
            if sw_lb>0: df['swing_high'],df['swing_low']=df['high'].rolling(sw_lb,center=True,min_periods=min_p_sw).max(),df['low'].rolling(sw_lb,center=True,min_periods=min_p_sw).min()
            else: df['swing_high'],df['swing_low']=df['high'],df['low']

            df.ffill(inplace=True)
            df.dropna(inplace=True)
            return df if not df.empty else None
        except Exception as e: logging.error(f"Feature engineering failed: {e}", exc_info=True); return None

# --- SCORING MODEL ---
class ScoringModel:
    def __init__(self, cfg: Config): self.cfg = cfg
    def apply_ignition_filter(self, df: pd.DataFrame, micro: Dict) -> bool:
        if df is None or df.empty: return False
        try:
            last=df.iloc[-1]; bbw,zp,zv=last.get('bb_width',np.nan),last.get('z_score_price',np.nan),last.get('z_score_volume',np.nan)
            if pd.isna(bbw) or pd.isna(zp) or pd.isna(zv): return False
            base_ok = (bbw < 0.06 and zp > 0 and zv > 0)
            oi_d,obi=micro.get("oi_delta_pct",-1.0),micro.get("obi",-1.0)
            micro_ok = (oi_d > self.cfg.ignition_oi_delta_threshold_pct and obi > self.cfg.ignition_obi_threshold)
            return base_ok and micro_ok
        except Exception: return False
    def generate_composite_score(self, df: pd.DataFrame, micro: Dict) -> float:
        if df is None or df.empty: return 0.0
        try:
            last=df.iloc[-1]; zp,zv=last.get("z_score_price",0.0),last.get("z_score_volume",0.0)
            ign_s=(0.0 if pd.isna(zp) else float(zp))+(0.0 if pd.isna(zv) else float(zv))
            oi_d,obi=micro.get("oi_delta_pct",0.0),micro.get("obi",0.0)
            oi_s=float(oi_d)*self.cfg.spec_score_weight_oi if isinstance(oi_d,(int,float)) else 0.0
            obi_s=float(obi)*self.cfg.spec_score_weight_obi if isinstance(obi,(int,float)) else 0.0
            spec_s=oi_s+obi_s
            final=(ign_s*self.cfg.score_weight_ignition)+(spec_s*self.cfg.score_weight_specificity)
            return float(final) if pd.notna(final) else 0.0
        except Exception: return 0.0
    def calibrate_confidence(self, score: float) -> float:
        if not isinstance(score,(int,float)) or pd.isna(score): return 0.5
        clamped=max(-20, min(20, score));
        try: return 1 / (1 + math.exp(-self.cfg.score_confidence_factor * clamped))
        except OverflowError: return 1.0 if clamped > 0 else 0.0

# --- RISK MANAGER ---
class RiskManager:
    def __init__(self, cfg: Config): self.cfg = cfg
    async def run_pre_trade_gates(self, sym: str, df: pd.DataFrame) -> bool:
        min_bars=max(self.cfg.adv_gate_lookback,1)
        if df is None or len(df)<min_bars: return False
        if not self._check_adv_gate(sym,df): return False
        return self._check_var_gate(sym)
    def _check_adv_gate(self, sym: str, df: pd.DataFrame) -> bool:
        try:
            lb=min(self.cfg.adv_gate_lookback,len(df)); rel=df.iloc[-lb:]
            if 'volume' not in rel.columns or 'close' not in rel.columns: return False
            avg_v,avg_p=rel['volume'].mean(),rel['close'].mean()
            if pd.isna(avg_v) or pd.isna(avg_p) or avg_p<=0: return False
            avg_dv=avg_v*avg_p
            if avg_dv < self.cfg.adv_gate_min_dollar_volume:
                logging.debug(f"[{sym}] FAILED ADV: ${avg_dv:,.0f}<${self.cfg.adv_gate_min_dollar_volume:,.0f}")
                return False
            return True
        except Exception: return False
    def _check_var_gate(self, sym: str) -> bool: return True # STUB

# --- LIQUIDATION MANAGER ---
@dataclass
class LiqNode: price: float; density: float
class LiquidationWSManager:
    def __init__(self, cfg: Config):
        self.cfg,self._data,self._lock=cfg,defaultdict(lambda:defaultdict(lambda:deque(maxlen=1000))),threading.Lock()
        self._thread=threading.Thread(target=self._run_forever,daemon=True,name="LiquidationThread")
        self.loop,self._stop_event=None,None
    def start(self):
        if not self._thread.is_alive(): logging.info("Starting LiqMan..."); self._thread.start()
    async def stop(self):
        if self.loop and self._thread.is_alive() and self._stop_event:
            logging.info("Stopping LiqMan..."); L = asyncio.get_running_loop()
            if L != self.loop: self.loop.call_soon_threadsafe(self._stop_event.set)
            else: self._stop_event.set()
            await asyncio.sleep(1)
    def _run_forever(self):
        self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop); self._stop_event=asyncio.Event()
        try:
            stop_waiter=self.loop.create_task(self._stop_event.wait()); connect_task=self.loop.create_task(self._connect_all())
            done,pending=self.loop.run_until_complete(asyncio.wait([connect_task,stop_waiter],return_when=asyncio.FIRST_COMPLETED))
            if stop_waiter in done: logging.info("[LiqMan] Stop received.")
            logging.info("[LiqMan] Cancelling tasks..."); [task.cancel() for task in pending if not task.done()]; self.loop.run_until_complete(asyncio.gather(*pending,return_exceptions=True))
        except Exception as e: logging.critical(f"[LiqMan] Loop error: {e}", exc_info=True)
        finally:
             logging.info("[LiqMan] Loop finished.")
             if self.loop and not self.loop.is_closed(): self.loop.close(); self.loop=None
    async def _connect_all(self): await asyncio.gather(*[self._listen(ex) for ex in self.cfg.exchanges_liq])
    async def _listen(self, ex: str):
        urls={'binance':'wss://fstream.binance.com/ws/!forceOrder@arr'}; url=urls.get(ex.lower()); delay=5
        if not url: return
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url,ping_interval=30,ping_timeout=10,open_timeout=20) as ws:
                    logging.info(f"[LiqMan] Connected: {ex}."); delay=5
                    while not self._stop_event.is_set():
                        try: msg=await asyncio.wait_for(ws.recv(),timeout=70); self._process_message(ex,json.loads(msg))
                        except asyncio.TimeoutError:
                            try: await asyncio.wait_for(await ws.ping(),timeout=10)
                            except asyncio.TimeoutError: break
                        except Exception: pass
            except asyncio.CancelledError: break
            except Exception: pass
            if self._stop_event.is_set(): break
            try: await asyncio.wait_for(self._stop_event.wait(),timeout=delay)
            except asyncio.TimeoutError: delay=min(delay*1.5,60)
            else: break
        logging.info(f"[LiqMan] Listener stopped: {ex}.")
    def _process_message(self, ex: str, data: Dict):
        try:
            if ex=='binance':
                o=data.get('o'); sym=o.get('s')
                if not isinstance(o,dict) or not sym or not sym.endswith(self.cfg.quote_currency.upper()): return
                p_str=o.get('ap') if o.get('ap') and float(o.get('ap',0))>0 else o.get('p');
                if not p_str: return
                p,q,s,ts=float(p_str),float(o['q']),o['S'],int(data['E'])
                if p>0 and q>0 and s in ['BUY','SELL']:
                    with self._lock: self._data[ex][sym].append((ts,p,q,s))
        except Exception: pass
    def get_heatmap(self, sym_ccxt: str, cur_p: float) -> Dict:
        sym_ws=sym_ccxt.replace('/','').upper(); empty={"long_clusters":[], "short_clusters":[]}
        if cur_p<=0: return empty
        ts_cut=int(time.time()*1000)-(self.cfg.liq_window_sec*1000); p_min,p_max=cur_p*(1-self.cfg.liq_price_range_pct),cur_p*(1+self.cfg.liq_price_range_pct); bin_sz=max((cur_p*self.cfg.liq_bin_step_pct),1e-9)
        lb,sb=defaultdict(float),defaultdict(float)
        with self._lock: liqs=[liq for ex in self.cfg.exchanges_liq for liq in list(self._data[ex].get(sym_ws,[]))]
        if not liqs: return empty
        for (ts,p,q,s) in liqs:
            if ts<ts_cut or not (p_min<=p<=p_max): continue; bin_p=round(p/bin_sz)*bin_sz
            if bin_p<=0: continue
            if s=='SELL': lb[bin_p]+=q
            elif s=='BUY': sb[bin_p]+=q
        lc=[{"price":p,"density":d} for p,d in lb.items() if p<cur_p and d>0]; sc=[{"price":p,"density":d} for p,d in sb.items() if p>cur_p and d>0]
        top_l=sorted(lc,key=lambda x:x["density"],reverse=True)[:self.cfg.liq_top_nodes]; top_s=sorted(sc,key=lambda x:x["density"],reverse=True)[:self.cfg.liq_top_nodes]
        return {"long_clusters":top_l, "short_clusters":top_s}

# --- TP_SL_Calculator ---
class TP_SL_Calculator:
    def __init__(self, cfg: Config, liq_ws: LiquidationWSManager): self.cfg, self.liq_ws = cfg, liq_ws
    def calculate(self, df: pd.DataFrame, sym: str) -> Optional[Dict]:
        if df is None or df.empty or 'close' not in df.columns or df['close'].isna().all(): return None
        try:
            strat = self.cfg.tp_sl_strategy.lower(); last_p_idx = df['close'].last_valid_index()
            last_p = float(df.loc[last_p_idx, 'close']) if last_p_idx is not None else np.nan
            if pd.isna(last_p) or last_p <= 0: return None
            if strat=="atr": res=self._calc_atr(df,last_p)
            elif strat=="swing": res=self._calc_swing(df,last_p)
            elif strat=="liquidation": res=self._calc_liq(sym,last_p)
            else: res=self._calc_atr(df,last_p)
            if res and (res.get('sl',-1)<=0 or res.get('entry',-1)<=0 or res.get('tp1',-1)<=0 or res['sl']>=res['entry'] or res['tp1']<=res['entry']): return None
            return res
        except Exception: return None
    def _last_valid(self, s: pd.Series) -> Optional[float]:
        idx = s.last_valid_index(); v = s[idx] if idx is not None else None; return float(v) if v is not None and pd.notna(v) and v > 1e-9 else None
    def _calc_atr(self, df: pd.DataFrame, lp: float) -> Optional[Dict]:
        atr = self._last_valid(df['atr']); slm, tpm = self.cfg.atr_config["sl_multiplier"], self.cfg.atr_config["tp_multiplier"]
        if atr is None: return None; sl, tp1 = lp - (atr * slm), lp + (atr * tpm); return self._format(lp, sl, [tp1])
    def _calc_swing(self, df: pd.DataFrame, lp: float) -> Optional[Dict]:
        lb = min(self.cfg.swing_config['lookback'], len(df)); atr = self._last_valid(df['atr']);
        if lb < 2: return None
        sl_val = df['swing_low'].iloc[-lb:-1].min() if 'swing_low' in df.columns else np.nan; sl = sl_val if pd.notna(sl_val) and sl_val > 0 else lp * 0.98
        highs = df['high'].iloc[-lb:-1] if 'high' in df.columns else pd.Series(dtype=float); vh = highs[highs > lp]; tp1_val = vh.max()
        tp1 = tp1_val if pd.notna(tp1_val) else (lp + (atr * self.cfg.atr_config["tp_multiplier"]) if atr else lp * 1.03)
        return self._format(lp, sl, [tp1])
    def _calc_liq(self, sym: str, lp: float) -> Optional[Dict]:
        hm = self.liq_ws.get_heatmap(sym, lp)
        sl = max(hm["long_clusters"], key=lambda x: x["density"])["price"] * 0.998 if hm["long_clusters"] else lp * 0.985
        tps = sorted([c["price"] for c in hm["short_clusters"]]) if hm["short_clusters"] else [lp * 1.015, lp * 1.03]
        return self._format(lp, sl, tps)
    def _format(self, e: float, sl: float, tps: List) -> Dict:
        fallback = {"entry": -1, "sl": -1, "tp1": -1, "tp2": -1, "tp3": -1, "max": -1}
        if not (isinstance(e,(int,float)) and e>0 and isinstance(sl,(int,float)) and sl>0 and isinstance(tps,list)): return fallback
        sl = min(sl, e * 0.9995); vtps = sorted([tp for tp in tps if isinstance(tp, (int, float)) and tp > e])
        if not vtps: vtps = [e * 1.01, e * 1.02, e * 1.03]
        tp1 = vtps[0]; tp2 = vtps[1] if len(vtps) > 1 else tp1 * 1.01; tp3 = vtps[2] if len(vtps) > 2 else tp2 * 1.01
        tp2 = max(tp2, tp1 * 1.001); tp3 = max(tp3, tp2 * 1.001)
        if not (0 < sl < e < tp1 <= tp2 <= tp3): sl, tp1, tp2, tp3 = e * 0.98, e * 1.02, e * 1.04, e * 1.06
        return {"entry": round(e,6), "sl": round(sl,6), "tp1": round(tp1,6), "tp2": round(tp2,6), "tp3": round(tp3,6), "max": round(tp3,6)}

# --- AppContext ---
@dataclass
class AppContext: cfg: Config; exchange: ccxt.Exchange; data_engine: DataEngine; micro_engine: MicrostructureEngine; feature_engine: FeatureEngine; scoring_model: ScoringModel; risk_manager: RiskManager; tp_sl_calculator: TP_SL_Calculator; liq_ws_manager: LiquidationWSManager; ohlcv_semaphore: asyncio.Semaphore

# --- process_symbol ---
async def process_symbol(symbol: str, ctx: AppContext) -> Optional[Dict]:
    try:
        async with ctx.ohlcv_semaphore: df_ohlcv = await ctx.data_engine.get_ohlcv(symbol, ctx.cfg.timeframe, ctx.cfg.lookback_period)
        min_bars = max(ctx.cfg.lookback_period, ctx.cfg.adv_gate_lookback, ctx.cfg.swing_config['lookback'], 5)
        if df_ohlcv is None or len(df_ohlcv) < min_bars: return None
        micro = ctx.micro_engine.get_microstructure_signals(symbol)
        df_feat = await asyncio.to_thread(ctx.feature_engine.engineer_features, df_ohlcv.copy())
        if df_feat is None: return None
        if not await ctx.risk_manager.run_pre_trade_gates(symbol, df_feat): return None
        if not ctx.scoring_model.apply_ignition_filter(df_feat, micro): return None
        # logging.info(f"[{symbol}] Passed checks.")
        score = ctx.scoring_model.generate_composite_score(df_feat, micro); conf = ctx.scoring_model.calibrate_confidence(score)
        params = ctx.tp_sl_calculator.calculate(df_feat, symbol);
        if params is None: return None
        e,sl,tp1 = params["entry"], params["sl"], params["tp1"]
        if not (0<sl<e<tp1): return None
        risk,reward=e-sl,tp1-e; rr=reward/risk if risk>1e-9 else 0.0; expect=(conf*reward)-((1-conf)*risk)
        return {"symbol": symbol, "score": score, "confidence": conf, "rr": rr, "expectancy": expect, "params": params, "micro": micro}
    except Exception as e: logging.error(f"Error processing {symbol}: {e}", exc_info=True); return None

# --- main ---
async def main():
    cfg = Config(); exchange_id = cfg.exchanges[0]
    exchange, micro_engine, liq_ws_manager = None, None, None
    main_task = None
    try:
        exchange = getattr(ccxt, exchange_id)({'asyncio_loop':asyncio.get_event_loop(),'options':{'defaultType':cfg.market_type},'enableRateLimit':True,'rateLimit':100,'timeout':30000,'connectTimeout':15000})
        logging.info(f"Initialized CCXT: {exchange.id}"); logging.info(f"Loading markets...")
        await exchange.load_markets(True)

        symbols_ccxt = []
        for sym, m in exchange.markets.items():
            try:
                is_swap, is_linear, is_inverse = m.get('swap', False), m.get('linear', False), m.get('inverse', False)
                type_ok = (cfg.market_type == 'swap' and (is_swap or is_linear or is_inverse)) or (cfg.market_type == 'future' and m.get('future', False))
                if (m.get('quote','').upper()==cfg.quote_currency.upper() and type_ok and m.get('active',True) and m['symbol'] not in cfg.blacklist):
                     symbols_ccxt.append(m['symbol'])
            except: pass
        if not symbols_ccxt: logging.critical(f"No active '{cfg.market_type}' markets for {cfg.quote_currency}. Exiting."); return
        logging.info(f"Found {len(symbols_ccxt)} active symbols.")

        data_engine=DataEngine(exchange); liq_ws_manager=LiquidationWSManager(cfg); micro_engine=MicrostructureEngine(cfg,symbols_ccxt)
        feature_engine=FeatureEngine(cfg); scoring_model=ScoringModel(cfg); risk_manager=RiskManager(cfg); tp_sl_calculator=TP_SL_Calculator(cfg,liq_ws_manager)
        ohlcv_sem=asyncio.Semaphore(cfg.max_concurrent_ohlcv_fetches)
        liq_ws_manager.start(); micro_engine.start()
        ctx=AppContext(cfg,exchange,data_engine,micro_engine,feature_engine,scoring_model,risk_manager,tp_sl_calculator,liq_ws_manager,ohlcv_sem)
        logging.info("Components initialized. Waiting 15s for WS..."); await asyncio.sleep(15)

        main_task = asyncio.current_task() # Get ref to main task for cancellation check

        while True: # Main Scan Loop
            if main_task and main_task.cancelled(): break
            start_t=time.time(); logging.info(f"--- Scan Cycle Start ({len(symbols_ccxt)} symbols) ---")
            tasks=[process_symbol(sym,ctx) for sym in symbols_ccxt]; results=await asyncio.gather(*tasks,return_exceptions=True)
            duration=time.time()-start_t; logging.info(f"--- Symbol Processing Done ({duration:.2f}s) ---")

            trades,err_cnt=[],0
            for i,r in enumerate(results):
                if isinstance(r,Exception): err_cnt+=1
                elif isinstance(r,dict): trades.append(r)
            if err_cnt>0: logging.warning(f"{err_cnt}/{len(symbols_ccxt)} symbols failed processing.")

            if not trades: logging.info("Scan Results: No candidates passed.")
            else:
                top=sorted(trades,key=lambda x:x.get("expectancy",-float('inf')),reverse=True)[:cfg.top_n_candidates]
                logging.info(f"Scan Results: {len(trades)} candidates. Top {len(top)}:")
                headers=["#","Sym","Score","Conf","RR","Expect","OIΔ%","CVD","OBI","Entry","SL","TP1"]
                table=[]
                for i,c in enumerate(top,1):
                    p,m=c.get("params",{}),c.get("micro",{})
                    cvd=m.get('perp_cvd',0); cvd_str=f"{cvd:,.0f}" if isinstance(cvd,(int,float)) else "?"
                    table.append([
                        i, c.get("symbol","?").replace(f"/{cfg.quote_currency}",""), f"{c.get('score',0):.2f}", f"{c.get('confidence',0):.0%}",
                        f"{c.get('rr',0):.1f}", f"{c.get('expectancy',0):.4f}", f"{m.get('oi_delta_pct',0):.1%}",
                        cvd_str, f"{m.get('obi',0):.2f}", f"{p.get('entry',0):.4f}", f"{p.get('sl',0):.4f}", f"{p.get('tp1',0):.4f}"
                    ])
                try: print(tabulate(table,headers=headers,tablefmt="fancy_grid",floatfmt=".4f"))
                except Exception as e: logging.error(f"Table display error: {e}"); [print(row) for row in table]

            wait=max(5.0,cfg.scan_interval_seconds-duration); logging.info(f"--- Scan Cycle End. Wait {wait:.1f}s... ---")
            try: await asyncio.wait_for(asyncio.sleep(wait), timeout=wait + 1)
            except asyncio.TimeoutError: pass
            except asyncio.CancelledError: break

    except asyncio.CancelledError: logging.info("Main task cancelled during execution.")
    except Exception as e: logging.critical(f"Unhandled error in main: {e}", exc_info=True)
    finally: # Graceful Shutdown
        logging.info("Initiating shutdown sequence...")
        stop_tasks = []
        try: current_loop = asyncio.get_running_loop(); loop_is_running = current_loop.is_running()
        except RuntimeError: current_loop, loop_is_running = None, False

        if micro_engine:
             if loop_is_running: stop_tasks.append(current_loop.create_task(micro_engine.stop()))
        if liq_ws_manager:
             if loop_is_running: stop_tasks.append(current_loop.create_task(liq_ws_manager.stop()))

        if stop_tasks and loop_is_running:
             logging.info("Waiting for background services to stop...")
             await asyncio.gather(*stop_tasks, return_exceptions=True)

        if exchange:
            logging.info("Closing main CCXT connection...")
            try: await exchange.close()
            except Exception as e: logging.warning(f"Error closing main exchange: {e}")
        logging.info("Shutdown complete.")

# --- Graceful Shutdown Logic ---
async def shutdown_logic(loop, sig=None):
    if sig: logging.info(f"Received exit signal {sig.name}...")
    logging.info("Initiating graceful shutdown...")
    tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task(loop=loop)]
    if not tasks:
        if loop.is_running(): loop.stop(); return

    logging.info(f"Cancelling {len(tasks)} tasks...")
    [task.cancel() for task in tasks]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    cancelled_exceptions = 0
    for res in results:
         if isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError):
              logging.warning(f"Exception during task cancellation: {res}"); cancelled_exceptions +=1
    if cancelled_exceptions > 0: logging.warning(f"{cancelled_exceptions} exceptions during shutdown.")
    else: logging.info("All tasks cancelled.")
    if loop.is_running(): loop.stop()

# --- Entry Point ---
if __name__ == "__main__":
    logging.info("Initializing Scanner...")
    try: asyncio.run(main())
    except KeyboardInterrupt: logging.info("Interrupted by user (KeyboardInterrupt).")
    except Exception as e: logging.critical(f"Script failed: {e}", exc_info=True)
    finally: logging.info("Script finished.")

