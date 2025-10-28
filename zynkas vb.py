#!/usr/bin/env python3
"""
Trade Decoder — v4 Alpha Engine
--------------------------------
Implements the roadmap from "From Zero‑OI to Alpha Engine" plus the earlier
WS/REST hardening and OI history seeding.

Major upgrades
• Robust OI parsing (no silent 0.0s) and exchange‑specific history seeding.
• Multi‑TF OI metrics + OI Fuel + OI$ Pulse + Unwind.
• Normalized CVD momentum (MAD‑z) + optional bullish divergence flag.
• Confidence score weights are now configurable via CLI.
• JIT OI snapshots for the displayed Top‑K to avoid stale prints.
• Structured logging with levels and exchange/symbol context.
• Backtest stub: deterministic loop over historical 1m klines & OI hist,
  a TradeLogger, and KPI summary (win rate, expectancy, PF, MDD) for a
  single symbol (extensible to multi‑symbol).

Run (live):
  python3 trade_decoder_v4_alpha.py --exchanges BINANCE,BYBIT --top-k 5

Run (quick backtest stub example):
  python3 trade_decoder_v4_alpha.py --mode backtest --symbol BTCUSDT --venue BINANCE \
    --start "2024-09-01" --end "2024-09-07" --min-conf 70
"""

import os, sys, time, math, uuid, hashlib, random, signal, statistics, asyncio, logging
from dataclasses import dataclass, field
from typing import Dict, Deque, List, Tuple, Optional, Any
from collections import deque
from datetime import datetime, timezone, timedelta
from tabulate import tabulate

try:
    import aiohttp
    try:
        import orjson as json
    except Exception:
        import json  # type: ignore
    try:
        import orjson as _orjson
        def _jdumps(obj): return _orjson.dumps(obj).decode()
        def _jloads(s):
            if isinstance(s, (bytes, bytearray, memoryview)): return _orjson.loads(s)
            return _orjson.loads(s.encode())
    except Exception:
        import json as _pyjson
        def _jdumps(obj): return _pyjson.dumps(obj)
        def _jloads(s):
            if isinstance(s, (bytes, bytearray, memoryview)): s = s.decode()
            return _pyjson.loads(s)
    try:
        import uvloop  # type: ignore
        UVLOOP = True
    except Exception:
        UVLOOP = False
except Exception as e:
    print("[ERROR] Missing deps. pip install aiohttp orjson uvloop tabulate"); raise

# ----------------------------- logging -----------------------------

def setup_logging(level="INFO"):
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(level=lvl, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("decoder")

# ----------------------------- helpers -----------------------------

def median_abs_deviation(seq: List[float]) -> float:
    if not seq: return 0.0
    med = statistics.median(seq)
    devs = [abs(x - med) for x in seq]
    return statistics.median(devs)

def modified_z_score(x: float, median: float, mad: float) -> float:
    if mad == 0: return 0.0
    return 0.6745 * (x - median) / mad

class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int):
        self.rate = rate_per_sec; self.capacity = capacity
        self.tokens = capacity; self.last = time.monotonic()
    async def acquire(self, tokens: int = 1):
        while self.tokens < tokens:
            now = time.monotonic(); elapsed = now - self.last
            self.last = now; self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            await asyncio.sleep(0.01)
        self.tokens -= tokens

# ------------------------------ config -----------------------------

@dataclass
class Config:
    exchanges: List[str] = field(default_factory=lambda: ["BINANCE","BYBIT"])
    max_symbols: int = 0
    streams_per_ws: int = 50
    pump_threshold_pct: float = 10.0
    oi_poll_secs: int = 30
    score_interval_secs: int = 30
    top_k: int = 5
    atr_len: int = 14
    atr_k: float = 2.0
    run_seconds: int = 0
    base_decay_minutes: int = 46
    warmup_min_trades: int = 30
    qps_limit: float = 8.0
    qps_capacity: int = 16
    strict_gating: bool = False
    oi_precision: int = 2
    # scoring weights
    w_pump: float = 30
    w_cvd: float = 25
    w_oi_delta: float = 12
    w_oi_mtf: float = 8
    w_oi_fuel: float = 8
    w_whale: float = 15
    w_funding: float = 10
    # modes
    mode: str = "live"  # or "backtest"
    venue: str = "BINANCE"
    symbol: str = "BTCUSDT"
    start: str = ""
    end: str = ""
    min_conf: float = 0.0
    log_level: str = "INFO"

# ----------------------------- state -------------------------------

@dataclass
class TradeStats:
    cvd: float = 0.0
    cvd_series: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=600))
    sizes: Deque[float] = field(default_factory=lambda: deque(maxlen=600))
    slope_hist: Deque[float] = field(default_factory=lambda: deque(maxlen=600))

@dataclass
class OIStats:
    last_oi: Optional[float] = None
    prev_oi: Optional[float] = None
    last_mark_price: Optional[float] = None
    hist: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4096))  # (ts, oi)

    def update(self, oi: float, ts: Optional[float] = None):
        ts = ts or time.time()
        self.prev_oi = self.last_oi
        self.last_oi = oi
        if not self.hist or ts - self.hist[-1][0] >= 30:
            self.hist.append((ts, oi))

    @property
    def oi_delta_pct(self) -> float:
        if self.last_oi is None or self.prev_oi is None or self.prev_oi == 0: return 0.0
        return (self.last_oi - self.prev_oi) / self.prev_oi * 100.0

    def _value_at_age(self, age_seconds: int) -> Optional[float]:
        if not self.hist: return None
        cutoff = time.time() - age_seconds
        for i in range(len(self.hist)-1, -1, -1):
            ts, v = self.hist[i]
            if ts <= cutoff: return v
        return self.hist[0][1]

    def pchange_minutes(self, minutes: float) -> float:
        if not self.hist or self.last_oi is None: return 0.0
        ref = self._value_at_age(int(minutes*60))
        if ref is None or ref == 0: return 0.0
        return (self.last_oi - ref) / ref * 100.0

    def pchange_between(self, newer_minutes: float, older_minutes: float) -> float:
        a = self._value_at_age(int(newer_minutes*60))
        b = self._value_at_age(int(older_minutes*60))
        if a is None or b in (None, 0): return 0.0
        return (a - b) / b * 100.0

    # OI Fuel: relative deviation vs EMA of OI over a lookback (minutes)
    def oi_fuel(self, lookback_min: int = 60) -> float:
        if not self.hist: return 0.0
        now = self.last_oi or 0.0
        if now == 0.0: return 0.0
        # Build EMA from history within window
        cutoff = time.time() - lookback_min*60
        vals = [v for (ts, v) in self.hist if ts >= cutoff]
        if not vals: return 0.0
        # simple EMA
        alpha = 2/(len(vals)+1)
        ema = vals[0]
        for v in vals[1:]:
            ema = alpha*v + (1-alpha)*ema
        if ema == 0: return 0.0
        return (now/ema - 1.0) * 100.0

@dataclass
class SymbolState:
    exch: str
    symbol: str
    kline_open_4h: Optional[float] = None
    last_price: Optional[float] = None
    pump_pct: float = 0.0
    trade_stats: TradeStats = field(default_factory=TradeStats)
    oi_stats: OIStats = field(default_factory=OIStats)
    funding_rate: Optional[float] = None
    last_whale_z: float = 0.0
    bootstrapped_4h: bool = False
    price_hist: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4096))
    # divergence flags
    bull_div: bool = False

    def update_kline_4h(self, kline_open: float, last_price: float):
        self.kline_open_4h = kline_open; self.last_price = last_price
        self.pump_pct = (last_price - kline_open) / kline_open * 100.0 if kline_open else 0.0

    def _record_price(self, price: float, ts: Optional[float] = None):
        ts = ts or time.time()
        if not self.price_hist or ts - self.price_hist[-1][0] >= 60:
            self.price_hist.append((ts, price))

    def price_at_age(self, minutes: float) -> Optional[float]:
        if not self.price_hist: return None
        cutoff = time.time() - int(minutes*60)
        for i in range(len(self.price_hist)-1, -1, -1):
            ts, p = self.price_hist[i]
            if ts <= cutoff: return p
        return self.price_hist[0][1]

    def update_mark(self, mark_price: Optional[float], funding_rate: Optional[float]):
        if mark_price is not None:
            self.oi_stats.last_mark_price = mark_price; self.last_price = mark_price
            self._record_price(mark_price)
            if self.kline_open_4h:
                self.pump_pct = (mark_price - self.kline_open_4h) / self.kline_open_4h * 100.0
        if funding_rate is not None:
            self.funding_rate = funding_rate

    def update_trade(self, price: float, qty: float, taker_is_buy: bool):
        signed = qty if taker_is_buy else -qty
        self.trade_stats.cvd += signed
        now = time.time()
        self.trade_stats.cvd_series.append((now, self.trade_stats.cvd))
        self.trade_stats.sizes.append(qty)
        if price > 0: self._record_price(price, ts=now)
        if len(self.trade_stats.sizes) >= 20:
            median = statistics.median(self.trade_stats.sizes)
            mad = median_abs_deviation(list(self.trade_stats.sizes))
            self.last_whale_z = modified_z_score(qty, median, mad)

    def cvd_slope_per_min(self, window_sec: int = 300) -> float:
        s = list(self.trade_stats.cvd_series)
        if len(s) < 2: return 0.0
        t1 = s[-1][0];
        # find first index within window
        idx = 0
        for i in range(len(s)-1, -1, -1):
            if t1 - s[i][0] <= window_sec: idx = i
            else: break
        t0, v0 = s[idx]; t1, v1 = s[-1]
        dt_min = max((t1 - t0) / 60.0, 1e-9)
        return (v1 - v0) / dt_min

    def cvd_mom_z(self) -> float:
        slope = self.cvd_slope_per_min(300)
        self.trade_stats.slope_hist.append(slope)
        hist = list(self.trade_stats.slope_hist)
        if len(hist) < 20: return 0.0
        med = statistics.median(hist)
        mad = median_abs_deviation(hist)
        return modified_z_score(slope, med, mad)

    def detect_bullish_divergence(self) -> bool:
        # crude pivot‑low divergence: price lower‑low while CVD higher‑low
        if len(self.price_hist) < 20 or len(self.trade_stats.cvd_series) < 20: return False
        def pivots(series: List[Tuple[float,float]], w: int = 3) -> List[Tuple[int,float]]:
            out = []
            for i in range(w, len(series)-w):
                _, v = series[i];
                if all(v < series[j][1] for j in range(i-w, i)) and all(v < series[j][1] for j in range(i+1, i+w+1)):
                    out.append((i, v))
            return out
        price_piv = pivots(list(self.price_hist), 3)
        cvd_piv = pivots(list(self.trade_stats.cvd_series), 3)
        if len(price_piv) < 2 or len(cvd_piv) < 2: return False
        p1, p2 = price_piv[-2], price_piv[-1]
        c1, c2 = cvd_piv[-2], cvd_piv[-1]
        if p2[0] > c2[0]:  # ensure cvd pivot not too old
            pass
        return (p2[1] < p1[1]) and (c2[1] > c1[1])

    def oi_dollar_pulse_15m(self) -> float:
        now_oi = self.oi_stats.last_oi or 0.0
        now_px = self.last_price or self.oi_stats.last_mark_price or 0.0
        past_oi = self.oi_stats._value_at_age(15*60) or 0.0
        past_px = self.price_at_age(15) or 0.0
        num = (now_oi*now_px) - (past_oi*past_px)
        den = (past_oi*past_px) + 1e-3
        return (num / den) * 100.0 if den != 0 else 0.0

# --------------------------- connectors ----------------------------

class BaseConnector:
    NAME = "BASE"
    def __init__(self, cfg: Config):
        self.cfg = cfg; self.session = None; self._limiter = TokenBucket(cfg.qps_limit, cfg.qps_capacity)
    def set_session(self, session): self.session = session
    async def _rest_json(self, url: str, params: Optional[dict] = None):
        assert self.session is not None
        await self._limiter.acquire()
        for _ in range(3):
            try:
                async with self.session.get(url, params=params) as r:
                    if r.status != 200:
                        if r.status in (418,429): log.warning(f"{self.NAME}-REST {r.status} {url.split('/')[-1]} {params}")
                        await asyncio.sleep(0.25); continue
                    return _jloads(await r.read())
            except aiohttp.ClientError as e:
                await asyncio.sleep(0.35)
        return None
    # centralized OI parsing per doc
    def _parse_oi_value(self, oi_raw: Any, symbol: str) -> Optional[float]:
        if oi_raw is None:
            log.warning(f"{self.NAME}:{symbol} missing openInterest")
            return None
        try:
            oi = float(oi_raw)
            if oi < 0:
                log.warning(f"{self.NAME}:{symbol} negative OI {oi_raw!r}")
                return None
            return oi
        except (ValueError, TypeError):
            log.warning(f"{self.NAME}:{symbol} invalid OI {oi_raw!r}")
            return None
    async def get_universe(self) -> List[str]: raise NotImplementedError
    def shard(self, universe: List[str], size: int) -> List[List[str]]: return [universe[i:i+size] for i in range(0, len(universe), size)]
    async def ws_consume(self, scanner, shard_id: int, symbols: List[str]): raise NotImplementedError
    async def poll_open_interest(self, scanner): raise NotImplementedError
    async def snapshot_oi(self, symbol: str) -> Optional[float]: raise NotImplementedError
    async def history_open_interest(self, symbol: str, limit_5m: int = 72) -> List[Tuple[int,float]]: raise NotImplementedError

class BinanceConnector(BaseConnector):
    NAME = "BINANCE"
    def __init__(self, cfg: Config):
        super().__init__(cfg); self._rr_cursor = 0
    async def get_universe(self) -> List[str]:
        ex = await self._rest_json("https://fapi.binance.com/fapi/v1/exchangeInfo"); syms = []
        if ex and ex.get("symbols"):
            syms = [s["symbol"] for s in ex["symbols"] if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"]
        if self.cfg.max_symbols and self.cfg.max_symbols > 0:
            tks = await self._rest_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
            vol_map = {t.get("symbol"): float(t.get("quoteVolume","0")) for t in (tks or [])}
            syms.sort(key=lambda s: vol_map.get(s,0.0), reverse=True); syms = syms[:self.cfg.max_symbols]
        return syms
    def _streams(self, symbols: List[str]) -> str:
        parts = []
        for s in symbols:
            sl = s.lower(); parts += [f"{sl}@kline_4h", f"{sl}@aggTrade", f"{sl}@markPrice@1s"]
        return "/".join(parts)
    async def ws_consume(self, scanner, shard_id: int, symbols: List[str]):
        url = f"wss://fstream.binance.com/stream?streams={self._streams(symbols)}"
        assert self.session is not None
        base_delay, max_delay, retries = 5.0, 120.0, 0
        while not scanner._shutdown.is_set():
            try:
                async with self.session.ws_connect(url, heartbeat=15, timeout=30.0) as ws:
                    print(f"[BINANCE-WS{shard_id}] {len(symbols)} syms / {len(symbols)*3} topics")
                    base_delay, retries = 5.0, 0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = _jloads(msg.data); stream = payload.get("stream") or ""; data = payload.get("data") or {}
                            await scanner._on_binance_stream(stream, data)
                        elif msg.type == aiohttp.WSMsgType.ERROR: break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            print(f"[BINANCE-WS{shard_id}] Connection closed explicitly."); break
            except asyncio.CancelledError:
                print(f"[BINANCE-WS{shard_id}] Task cancelled."); break
            except Exception as e:
                retries += 1; delay = min(base_delay * (2 ** retries), max_delay)
                jitter = delay * random.uniform(0.5, 1.5)
                print(f"[BINANCE-WS{shard_id}] Reconnect {retries} after {e!r}. Sleep {jitter:.1f}s")
                base_delay = min(base_delay * 1.5, max_delay)
                if not scanner._shutdown.is_set(): await asyncio.sleep(jitter)
                else: break
        print(f"[BINANCE-WS{shard_id}] Exiting WS consumer loop.")
    async def poll_open_interest(self, scanner):
        SLICE = 60
        while not scanner._shutdown.is_set():
            all_syms = [s.symbol for s in scanner.states.values() if s.exch == self.NAME]
            if not all_syms: await asyncio.sleep(self.cfg.oi_poll_secs); continue
            start_idx = self._rr_cursor; end_idx = min(start_idx+SLICE, len(all_syms)); batch = all_syms[start_idx:end_idx]
            if not batch: self._rr_cursor = 0; continue
            for sym in batch:
                if scanner._shutdown.is_set(): break
                try:
                    data = await self._rest_json("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": sym})
                    oi = self._parse_oi_value((data or {}).get("openInterest"), sym)
                    if oi is not None:
                        st = scanner.states.get(f"{self.NAME}:{sym}")
                        if st: st.oi_stats.update(oi)
                except Exception: pass
                await asyncio.sleep(0.01)
            self._rr_cursor = end_idx if end_idx < len(all_syms) else 0
            try:
                await asyncio.wait_for(scanner._shutdown.wait(), timeout=self.cfg.oi_poll_secs)
                break
            except asyncio.TimeoutError: pass
        print("[BINANCE-OI] Exiting OI poller loop.")
    async def snapshot_oi(self, symbol: str) -> Optional[float]:
        data = await self._rest_json("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": symbol})
        if data:
            return self._parse_oi_value(data.get("openInterest"), symbol)
        return None
    async def history_open_interest(self, symbol: str, limit_5m: int = 72) -> List[Tuple[int,float]]:
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        js = await self._rest_json(url, params={"symbol": symbol, "period": "5m", "limit": limit_5m})
        out: List[Tuple[int,float]] = []
        if isinstance(js, list):
            for it in js:
                try:
                    ts = int(it.get("timestamp"))//1000
                    oi = float(it.get("sumOpenInterest"))
                    out.append((ts, oi))
                except Exception:
                    pass
        return out

class BybitConnector(BaseConnector):
    NAME = "BYBIT"
    async def get_universe(self) -> List[str]:
        info = await self._rest_json("https://api.bybit.com/v5/market/instruments-info", params={"category": "linear"})
        syms = []
        if info and info.get("result") and info["result"].get("list"):
            for it in info["result"]["list"]:
                if it.get("quoteCoin") == "USDT" and it.get("status") == "Trading": syms.append(it["symbol"])
        if self.cfg.max_symbols and self.cfg.max_symbols > 0:
            tks = await self._rest_json("https://api.bybit.com/v5/market/tickers", params={"category": "linear"})
            vol_map = {}
            if tks and tks.get("result") and tks["result"].get("list"):
                for it in tks["result"]["list"]:
                    try: vol_map[it["symbol"]] = float(it.get("turnover24h","0"))
                    except Exception: pass
            syms.sort(key=lambda s: vol_map.get(s,0.0), reverse=True); syms = syms[:self.cfg.max_symbols]
        return syms
    def _topics(self, symbols: List[str]) -> List[str]:
        out = []
        for s in symbols: out += [f"kline.240.{s}", f"publicTrade.{s}", f"tickers.{s}"]
        return out
    async def ws_consume(self, scanner, shard_id: int, symbols: List[str]):
        url = "wss://stream.bybit.com/v5/public/linear"; args = self._topics(symbols); sub = {"op": "subscribe", "args": args}
        assert self.session is not None
        base_delay, max_delay, retries = 5.0, 120.0, 0
        while not scanner._shutdown.is_set():
            try:
                async with self.session.ws_connect(url, heartbeat=15, timeout=30.0) as ws:
                    await ws.send_str(_jdumps(sub))
                    print(f"[BYBIT-WS{shard_id}] {len(symbols)} syms / {len(args)} topics")
                    base_delay, retries = 5.0, 0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = _jloads(msg.data); topic = payload.get("topic") or ""; data = payload.get("data") or payload.get("result") or {}
                            await scanner._on_bybit_stream(topic, data)
                        elif msg.type == aiohttp.WSMsgType.ERROR: break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            print(f"[BYBIT-WS{shard_id}] Connection closed explicitly."); break
            except asyncio.CancelledError:
                print(f"[BYBIT-WS{shard_id}] Task cancelled."); break
            except Exception as e:
                retries += 1; delay = min(base_delay * (2 ** retries), max_delay)
                jitter = delay * random.uniform(0.5, 1.5)
                print(f"[BYBIT-WS{shard_id}] Reconnect {retries} after {e!r}. Sleep {jitter:.1f}s")
                base_delay = min(base_delay * 1.5, max_delay)
                if not scanner._shutdown.is_set(): await asyncio.sleep(jitter)
                else: break
        print(f"[BYBIT-WS{shard_id}] Exiting WS consumer loop.")
    async def poll_open_interest(self, scanner):
        while not scanner._shutdown.is_set():
            try:
                data = await self._rest_json("https://api.bybit.com/v5/market/tickers", params={"category": "linear"})
                if data and data.get("result") and data["result"].get("list"):
                    for it in data["result"]["list"]:
                        sym = it.get("symbol"); oi = self._parse_oi_value(it.get("openInterest"), sym or "?")
                        if sym and oi is not None:
                            st = scanner.states.get(f"{self.NAME}:{sym}")
                            if st: st.oi_stats.update(oi)
            except Exception: pass
            try:
                await asyncio.wait_for(scanner._shutdown.wait(), timeout=self.cfg.oi_poll_secs)
                break
            except asyncio.TimeoutError: pass
        print("[BYBIT-OI] Exiting OI poller loop.")
    async def snapshot_oi(self, symbol: str) -> Optional[float]:
        t = await self._rest_json("https://api.bybit.com/v5/market/tickers", params={"category": "linear", "symbol": symbol})
        try:
            it = (t or {}).get("result", {}).get("list", [])
            if it: return self._parse_oi_value(it[0].get("openInterest"), symbol)
        except Exception: return None
        return None
    async def history_open_interest(self, symbol: str, limit_5m: int = 72) -> List[Tuple[int,float]]:
        js = await self._rest_json("https://api.bybit.com/v5/market/open-interest", params={"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": limit_5m})
        out: List[Tuple[int,float]] = []
        try:
            lst = (js or {}).get("result", {}).get("list", [])
            for it in lst:
                ts = int(it.get("timestamp"))//1000 if it.get("timestamp") else None
                oi = float(it.get("openInterest")) if it.get("openInterest") is not None else None
                if ts is None or oi is None: continue
                out.append((ts, oi))
        except Exception:
            pass
        return out

# ---------------------------- scanner ------------------------------

class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg; self.states: Dict[str, SymbolState] = {}; self._shutdown = asyncio.Event()
        self._run_start_ts = time.time()
        rid_src = f"{int(self._run_start_ts//60)}|{cfg.exchanges}|{cfg.max_symbols}|{cfg.pump_threshold_pct}"
        self.run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha1(rid_src.encode()).hexdigest()))
        self.session: Optional[aiohttp.ClientSession] = None
        self.cons = {"BINANCE": BinanceConnector(cfg), "BYBIT": BybitConnector(cfg)}

    async def __aenter__(self):
        setup_logging(self.cfg.log_level)
        headers = {"User-Agent": "trade-decoder-v4-alpha/1.0"}
        timeout = aiohttp.ClientTimeout(sock_read=30, total=60)
        self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        for con in self.cons.values(): con.set_session(self.session)
        return self
    async def __aexit__(self, exc_type, exc, tb):
        log.info("[SHUTDOWN] Signaling tasks to stop...")
        self._shutdown.set()
        if self.session:
            log.info("[SHUTDOWN] Closing HTTP session...")
            await self.session.close(); await asyncio.sleep(0.25)
        log.info("[SHUTDOWN] Session closed.")

    # ---- stream handlers ----
    async def _on_binance_stream(self, stream: str, data: dict):
        sym = stream.split("@", 1)[0].upper(); key = f"BINANCE:{sym}"
        st = self.states.get(key)
        if not st: return
        if stream.endswith("@kline_4h"):
            k = data.get("k", {})
            if k.get("i") != "4h": return
            try: st.update_kline_4h(float(k["o"]), float(k["c"]))
            except (ValueError, KeyError): pass
        elif stream.endswith("@aggTrade"):
            try:
                p = float(data["p"]); q = float(data["q"]); m = bool(data["m"])  # m=True is SELL aggressor
                st.update_trade(p, q, taker_is_buy=(not m))
            except (ValueError, KeyError): pass
        elif stream.endswith("@markPrice@1s"):
            mp = data.get("p"); fr = data.get("r")
            try:
                mpf = float(mp) if mp is not None else None; frf = float(fr) if fr is not None else None
                st.update_mark(mpf, frf)
                if st.kline_open_4h is None and not st.bootstrapped_4h:
                    asyncio.create_task(self._bootstrap_4h_binance(st))
            except (ValueError, KeyError): pass

    async def _bootstrap_4h_binance(self, st: SymbolState):
        if st.bootstrapped_4h: return
        st.bootstrapped_4h = True
        con = self.cons["BINANCE"]
        data = await con._rest_json("https://fapi.binance.com/fapi/v1/klines", params={"symbol": st.symbol, "interval": "4h", "limit": 2})
        try:
            last = data[-1]; st.update_kline_4h(float(last[1]), float(last[4]))
        except Exception:
            st.bootstrapped_4h = False

    async def _on_bybit_stream(self, topic: str, payload):
        parts = topic.split(".")
        if len(parts) < 2: return
        kind = parts[0]; sym = parts[-1].upper(); key = f"BYBIT:{sym}"
        st = self.states.get(key)
        if not st: return
        try:
            if kind == "kline":
                k = payload[0] if isinstance(payload, list) and payload else (payload if isinstance(payload, dict) else {})
                if k.get('confirm') == False: return
                o_str = k.get("open", k.get("o")); c_str = k.get("close", k.get("c"))
                o = float(o_str) if o_str else 0.0; c = float(c_str) if c_str else 0.0
                if o > 0 and c > 0: st.update_kline_4h(o, c)
            elif kind == "publicTrade":
                rows = payload if isinstance(payload, list) else [payload]
                for t in rows:
                    p_str = t.get("p") or t.get("price"); q_str = t.get("v") or t.get("size")
                    p = float(p_str) if p_str else 0.0; q = float(q_str) if q_str else 0.0
                    s = t.get("S") or t.get("side"); taker_is_buy = (str(s).lower() == "buy")
                    if p > 0 and q > 0: st.update_trade(p, q, taker_is_buy)
            elif kind == "tickers":
                d = payload if isinstance(payload, dict) else {}
                mp = d.get("markPrice") or d.get("lastPrice"); fr = d.get("fundingRate")
                mpf = float(mp) if mp else None; frf = float(fr) if fr else None
                st.update_mark(mpf, frf)
        except (ValueError, TypeError, KeyError) as e:
            log.warning(f"Error processing {topic}: {e!r}")

    # ---- scoring ----
    def _score(self, st: SymbolState) -> Tuple[float, dict]:
        pump_z = max((st.pump_pct - self.cfg.pump_threshold_pct)/5.0, 0.0)
        oi_delta_z   = max((st.oi_stats.oi_delta_pct - 0.3)/0.3, 0.0)
        cvd_z  = max(self._cap(st.cvd_mom_z(), 5.0), 0.0)
        whale_z = max(min(st.last_whale_z/3.0, 1.5), 0.0)
        fr     = st.funding_rate if st.funding_rate is not None else 0.0
        fr_z   = max(fr*100.0/0.02, 0.0) if fr != 0 else 0.0
        # New sources
        oi_mtf = (st.oi_stats.pchange_minutes(25) + st.oi_stats.pchange_minutes(60) + st.oi_stats.pchange_minutes(180)) / 3.0
        oi_fuel = st.oi_stats.oi_fuel(60)
        oi_mtf_z  = max(oi_mtf/5.0, 0.0)
        oi_fuel_z = max(oi_fuel/5.0, 0.0)
        div_bonus = 0.0
        st.bull_div = st.detect_bullish_divergence()
        if st.bull_div: div_bonus = 5.0

        conf   = (
            self.cfg.w_pump*pump_z + self.cfg.w_cvd*cvd_z + self.cfg.w_oi_delta*oi_delta_z +
            self.cfg.w_oi_mtf*oi_mtf_z + self.cfg.w_oi_fuel*oi_fuel_z +
            self.cfg.w_whale*whale_z + self.cfg.w_funding*fr_z + div_bonus
        )
        if self.cfg.strict_gating:
            if st.pump_pct < self.cfg.pump_threshold_pct and st.cvd_slope_per_min() <= 0:
                conf = 0.0
        return max(0.0, min(conf, 100.0)), {
            "pump_pct": st.pump_pct,
            "oi_delta_pct": st.oi_stats.oi_delta_pct,
            "oi_mtf": oi_mtf,
            "oi_fuel": oi_fuel,
            "cvd_z": cvd_z,
            "whale_z": st.last_whale_z,
            "funding_rate": fr,
            "bull_div": st.bull_div
        }

    @staticmethod
    def _cap(x: float, m: float) -> float: return -m if x < -m else (m if x > m else x)

    async def _fetch_1m(self, exch: str, symbol: str, length: int = 200):
        try:
            if exch == "BINANCE":
                con = self.cons["BINANCE"]
                data = await con._rest_json("https://fapi.binance.com/fapi/v1/klines", params={"symbol": symbol, "interval": "1m", "limit": length})
                return [(float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in (data or [])]
            if exch == "BYBIT":
                con = self.cons["BYBIT"]
                data = await con._rest_json("https://api.bybit.com/v5/market/kline", params={"category": "linear", "symbol": symbol, "interval": "1", "limit": length})
                out = []
                if data and data.get("result"):
                    # === THIS IS THE FIX FROM THE LAST ERROR ===
                    for row in data["result"].get("list", []):
                        if len(row) >= 6:
                            try: out.append((float(row[1]), float(row[2]), float(row[3]), float(row[4])))
                            except (ValueError, IndexError): pass
                return list(reversed(out))
        except Exception:
            return []
        return []

    @staticmethod
    def _atr(ohlc: List[Tuple[float, float, float, float]], length: int = 14) -> Optional[float]:
        if len(ohlc) < length + 1: return None
        trs = []
        try:
            prev_close = ohlc[0][3]
            for (_, h, l, c) in ohlc[1:]:
                tr = max(h - l, abs(h - prev_close), abs(l - prev_close)); trs.append(tr); prev_close = c
        except (TypeError, IndexError): return None
        if len(trs) < length: return None
        try: return statistics.mean(trs[-length:])
        except statistics.StatisticsError: return None

    async def _plan(self, st: SymbolState) -> Tuple[float, float, float, float, float, float]:
        ohlc = await self._fetch_1m(st.exch, st.symbol, 200)
        last = (st.last_price or 0.0)
        if last == 0.0 and ohlc: last = ohlc[-1][3]
        if last == 0.0: return (0.0,)*6
        atr = self._atr(ohlc, self.cfg.atr_len) or (last*0.002)
        entry = last*(1-0.001)
        sl = entry - self.cfg.atr_k*atr
        sl = min(sl, entry * 0.999)
        r = max(entry - sl, 1e-9)
        tp1, tp2, tp3 = entry+2*r, entry+3*r, entry+4*r
        rr = (tp1-entry)/r if r > 1e-9 else 0.0
        return entry, sl, tp1, tp2, tp3, rr

    def _decay(self, st: SymbolState) -> int:
        vol = abs(st.pump_pct); base = self.cfg.base_decay_minutes
        if vol >= 20: return max(20, int(base*0.6))
        if vol >= 10: return max(25, int(base*0.8))
        return base

    async def _seed_oi_history(self, tops: List[Tuple[str, SymbolState, float, dict]]):
        tasks = []
        for key, st, conf, details in tops:
            con = self.cons.get(st.exch)
            if not con: continue
            need = not st.oi_stats.hist or (time.time() - st.oi_stats.hist[0][0] < 360*60)
            tasks.append(con.history_open_interest(st.symbol, limit_5m=72) if need else asyncio.sleep(0, result=[]))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, (key, st, conf, details) in enumerate(tops):
            hist = results[i]
            if isinstance(hist, list) and hist:
                hist.sort(key=lambda x: x[0])
                st.oi_stats.hist.clear(); st.oi_stats.prev_oi = None; st.oi_stats.last_oi = None
                for ts, oi in hist:
                    st.oi_stats.update(oi, ts=ts)

    async def _jit_refresh_oi(self, tops: List[Tuple[str, SymbolState, float, dict]]):
        tasks = []
        for key, st, conf, details in tops:
            con = self.cons.get(st.exch)
            if not con: continue
            tasks.append(con.snapshot_oi(st.symbol))
        snaps = await asyncio.gather(*tasks, return_exceptions=True)
        for i, (key, st, conf, details) in enumerate(tops):
            val = snaps[i]
            if isinstance(val, (int, float)) and val > 0:
                st.oi_stats.update(float(val))

    async def _rank_and_print(self):
        rows = []
        warm_sym = warm_trade = warm_oi = 0
        for key, st in list(self.states.items()):
            if st.kline_open_4h is None or st.last_price is None or st.last_price <= 0: continue
            warm_sym += 1
            if len(st.trade_stats.cvd_series) < self.cfg.warmup_min_trades: continue
            warm_trade += 1
            if st.oi_stats.last_oi is not None: warm_oi += 1
            conf, details = self._score(st)
            if conf > self.cfg.min_conf: rows.append((key, st, conf, details))
        rows.sort(key=lambda x: (x[2], x[0]), reverse=True)
        timestamp_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        title = f"✨ Trade Decoder Top {min(len(rows), self.cfg.top_k)} • run_id {self.run_id[:8]} • {timestamp_str}"
        if not rows:
            print(f"\n=== Warm-up: {warm_sym} have 4h+price, {warm_trade} have ≥{self.cfg.warmup_min_trades} trades, {warm_oi} have OI — scanning... ===\n"); return

        top = rows[: self.cfg.top_k]
        try:
            await self._seed_oi_history(top)
        except Exception as e:
            log.warning(f"SEED-OI error: {e!r}")
        try:
            await self._jit_refresh_oi(top)
        except Exception as e:
            log.warning(f"JIT-OI error: {e!r}")

        plan_tasks = [self._plan(st) for key, st, conf, details in top]
        try:
            plans = await asyncio.gather(*plan_tasks)
        except Exception as e:
            log.warning(f"plan() error: {e!r}"); plans = [(0.0,)*6] * len(top)

        headers = [
            "SYMBOL","ENTRY","SL","TP1","TP2","TP3",
            "CONF","RR (TP1)","DECAY",
            "OI 6h %","OI Fuel 1h %","OI MTF","Unwind 15m","OI$ Pulse 15m %",
            "TRIGGERS",
        ]
        table_data: List[List[str]] = []
        for i, (key, st, conf, details) in enumerate(top):
            entry, sl, tp1, tp2, tp3, rr = plans[i]
            if entry <= 0 or sl <= 0 or tp1 <= entry or sl >= entry: continue

            oi_6h = st.oi_stats.pchange_minutes(360)
            oi_fuel_1h = st.oi_stats.oi_fuel(60)
            oi_mtf = (st.oi_stats.pchange_minutes(25) + st.oi_stats.pchange_minutes(60) + st.oi_stats.pchange_minutes(180)) / 3.0
            unwind_15m = -st.oi_stats.pchange_minutes(15)
            oi_dollar_pulse = st.oi_dollar_pulse_15m()

            fr = (st.funding_rate or 0.0); fr_str = f"{fr:+.4f}"
            fr_emoji = "💰" if fr <= -0.0002 else ("🔥" if fr >= 0.0003 else "⚪")
            
            # === FIX 1: Changed " | " to "\n" to stack triggers vertically ===
            trig = "\n".join([
                f"pump {st.pump_pct:+.1f}%",
                f"OIΔ {st.oi_stats.oi_delta_pct:+.{self.cfg.oi_precision}f}%",
                f"OI_fuel {oi_fuel_1h:+.1f}%",
                f"CVDz {self._cap(st.cvd_mom_z(),5.0):+.1f}",
                ("🟢div" if st.bull_div else "⚪div"),
                f"fr {fr_emoji} {fr_str}",
            ])

            prec = 4 if entry < 10 else (3 if entry < 100 else 2)
            table_data.append([
                st.symbol,
                f"{entry:.{prec}f}", f"{sl:.{prec}f}", f"{tp1:.{prec}f}", f"{tp2:.{prec}f}", f"{tp3:.{prec}f}",
                f"{conf:5.1f}", f"{rr:.1f}", f"{self._decay(st)}m",
                f"{oi_6h:+.2f}%", f"{oi_fuel_1h:+.2f}%", f"{oi_mtf:+.2f}", f"{unwind_15m:+.2f}%", f"{oi_dollar_pulse:+.2f}%",
                trig,
            ])

        print(f"\n{title}\n")
        if not table_data:
            print("--- No valid trade plans generated for top candidates. ---")
        else:
            # === FIX 2: Changed last maxcolwidth from 65 to 25 ===
            print(tabulate(
                table_data,
                headers=headers,
                tablefmt="grid",
                numalign="right",
                stralign="left",
                maxcolwidths=[None,None,None,None,None,None,None,None,None,10,12,8,11,15,25],
            ))
        print()

    # ------------------------- backtest stub -------------------------
    async def backtest(self):
        # single‑symbol, minute‑bar stepper with simple RR exits
        venue = self.cfg.venue.upper(); sym = self.cfg.symbol.upper()
        con = self.cons.get(venue)
        if not con:
            print(f"[BACKTEST] Unknown venue {venue}"); return
        st = SymbolState(venue, sym)
        self.states[f"{venue}:{sym}"] = st
        # pull recent 1m klines for a bounded date range (naive loop)
        def _parse_date(s: str) -> int:
            return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()*1000)
        start_ms = _parse_date(self.cfg.start) if self.cfg.start else int((datetime.now(timezone.utc)-timedelta(days=3)).timestamp()*1000)
        end_ms   = _parse_date(self.cfg.end)   if self.cfg.end   else int(datetime.now(timezone.utc).timestamp()*1000)
        # fetch sequentially in chunks of 1000 bars
        kl = []
        cursor = start_ms
        while cursor < end_ms:
            params = {"symbol": sym, "interval": "1m", "limit": 1000, "startTime": cursor}
            if venue == "BINANCE":
                js = await con._rest_json("https://fapi.binance.com/fapi/v1/klines", params=params)
                if not js: break
                for row in js:
                    ts = int(row[0]); o,h,l,c = map(float, row[1:5]); kl.append((ts,o,h,l,c))
                cursor = js[-1][0] + 60_000
            else:  # BYBIT
                js = await con._rest_json("https://api.bybit.com/v5/market/kline", params={"category":"linear","symbol":sym,"interval":"1","limit":1000,"start":cursor})
                lst = (js or {}).get("result",{}).get("list",[])
                if not lst: break
                for row in reversed(lst):  # bybit returns newest first
                    ts = int(row[0]); o,h,l,c = float(row[1]), float(row[2]), float(row[3]), float(row[4]); kl.append((ts,o,h,l,c))
                cursor = lst[0][0] + 60_000
        if not kl:
            print("[BACKTEST] No klines fetched."); return
        # bootstrap OI hist
        hist = await con.history_open_interest(sym, 72)
        for ts, oi in hist:
            st.oi_stats.update(oi, ts=ts)
        # walk bars
        equity = 1_000.0; peak = equity
        wins=losses=0; gross_p=gross_l=0.0
        open_tr = None  # (entry, sl, tp1, ts)
        for ts,o,h,l,c in kl:
            # simulate mark stream
            st.update_mark(c, None)
            # fake 4h open once
            if st.kline_open_4h is None:
                st.update_kline_4h(o, c)
            # update cvd with a proxy (close‑open volume sign)
            st.update_trade(c, max(1.0, abs(c-o)), taker_is_buy=(c>=o))
            conf,_ = self._score(st)
            # entry on bar close if strong
            if open_tr is None and conf >= max(70, self.cfg.min_conf):
                entry, sl, tp1, tp2, tp3, rr = await self._plan(st)
                if entry>0 and sl>0 and tp1>entry:
                    open_tr = (entry, sl, tp1, ts)
            # manage open
            if open_tr:
                entry, sl, tp1, ets = open_tr
                # check stop/tp with bar extremes
                hit_tp = h>=tp1
                hit_sl = l<=sl
                if hit_tp or hit_sl:
                    r = entry-sl
                    pnl = (tp1-entry) if hit_tp else (sl-entry)
                    equity += pnl
                    peak = max(peak, equity)
                    if pnl>0: wins+=1; gross_p+=pnl
                    else: losses+=1; gross_l+=-pnl
                    open_tr = None
        # KPIs
        total = wins+losses
        win_rate = (wins/total*100.0) if total else 0.0
        loss_rate= (losses/total*100.0) if total else 0.0
        profit_factor = (gross_p/gross_l) if gross_l>0 else float('inf')
        expectancy = (win_rate/100.0)*(gross_p/max(1,wins)) - (loss_rate/100.0)*(gross_l/max(1,losses)) if total else 0.0
        mdd = (peak-equity)  # naive; end‑equity drawdown from peak
        print(f"\n[BACKTEST] {venue}:{sym} bars={len(kl)} trades={total} win%={win_rate:.1f} PF={profit_factor:.2f} Exp={expectancy:.3f} MDD={mdd:.2f} PnL={equity-1000:.2f}\n")

    async def run(self):
        if UVLOOP: import uvloop as _uv; _uv.install()
        # modes
        if self.cfg.mode.lower() == "backtest":
            async with self:
                await self.backtest()
            return
        # ---- live mode ----
        for name in list(self.cons.keys()):
            if name not in self.cfg.exchanges: del self.cons[name]
        universes: Dict[str, List[str]] = {}
        for name, con in self.cons.items():
            try:
                syms = await con.get_universe(); universes[name] = syms
                print(f"[BOOT] {name}: monitoring {'ALL' if self.cfg.max_symbols<=0 else len(syms)} symbols ({len(syms)})")
                for s in syms: self.states[f"{name}:{s}"] = SymbolState(name, s)
            except Exception as e:
                print(f"[BOOT] {name}: universe error {e!r}")
        if not universes:
            print("[BOOT] No exchanges ready."); return

        tasks: List[asyncio.Task] = []
        try:
            loop = asyncio.get_running_loop()
            for name, con in self.cons.items():
                syms = universes.get(name, [])
                if not syms: continue
                shards = con.shard(syms, self.cfg.streams_per_ws)
                for i, slc in enumerate(shards):
                    try:
                        task = loop.create_task(con.ws_consume(self, i+1, slc)); tasks.append(task); await asyncio.sleep(2.0)
                    except Exception as e:
                        print(f"[BOOT-ERR] Failed to start WS shard {name}-{i+1}: {e!r}")
                try:
                    task = loop.create_task(con.poll_open_interest(self)); tasks.append(task)
                except Exception as e:
                    print(f"[BOOT-ERR] Failed to start OI poller for {name}: {e!r}")
            try:
                task = loop.create_task(self._scorer()); tasks.append(task)
            except Exception as e:
                print(f"[BOOT-ERR] Failed to start scorer: {e!r}")

            stop = asyncio.Event()
            def _sig():
                print("\n[SIGNAL] Shutdown requested..."); stop.set(); self._shutdown.set()
            for sig_name in (signal.SIGINT, signal.SIGTERM):
                try: loop.add_signal_handler(sig_name, _sig)
                except NotImplementedError: pass

            print("[BOOT] All tasks started. Waiting for signal or timer...")
            stop_task = loop.create_task(stop.wait())
            wait_tasks = [stop_task]
            timer_task = None
            if self.cfg.run_seconds > 0:
                timer_task = loop.create_task(asyncio.sleep(self.cfg.run_seconds))
                wait_tasks.append(timer_task)
            done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done:
                print("[SIGNAL] Stop event received.")
                if timer_task and not timer_task.done(): timer_task.cancel()
            elif timer_task and timer_task in done:
                print(f"[TIMER] Run duration ({self.cfg.run_seconds}s) elapsed. Initiating shutdown."); self._shutdown.set()

            print("[SHUTDOWN] Cancelling running tasks...")
            for task in tasks:
                if task not in done and not task.done(): task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            print(f"[SHUTDOWN] All {len(tasks)} tasks processed.")
            for i, res in enumerate(results):
                if isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError):
                    print(f"[SHUTDOWN-WARN] Task {i} raised exception during shutdown: {res!r}")
        except Exception as e:
            print(f"[FATAL] Unhandled error in run loop: {e!r}"); self._shutdown.set()
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            print("[RUNNER] Exiting.")

    async def _scorer(self):
        await asyncio.sleep(15)
        print("[SCORER] Starting scoring loop.")
        while not self.cfg.score_interval_secs <= 0 and not self._shutdown.is_set():
            try:
                await self._rank_and_print()
            except Exception as e:
                print(f"[SCORE] Error: {e!r}")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.cfg.score_interval_secs)
                break
            except asyncio.TimeoutError: pass
            except asyncio.CancelledError: break
        print("[SCORER] Exiting scoring loop.")

# ------------------------------- CLI -------------------------------

def _parse_args(argv: List[str]) -> Config:
    import argparse
    p = argparse.ArgumentParser(description="Trade Decoder — v4 Alpha Engine")
    p.add_argument("--exchanges", type=str, default="BINANCE,BYBIT")
    p.add_argument("--max-symbols", type=int, default=0)
    p.add_argument("--streams-per-ws", type=int, default=50)
    p.add_argument("--pump-threshold-pct", type=float, default=10.0)
    p.add_argument("--oi-poll-secs", type=int, default=30)
    p.add_argument("--score-interval-secs", type=int, default=30)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--atr-len", type=int, default=14)
    p.add_argument("--atr-k", type=float, default=2.0)
    p.add_argument("--run-seconds", type=int, default=0)
    p.add_argument("--warmup-min-trades", type=int, default=30)
    p.add_argument("--qps-limit", type=float, default=8.0)
    p.add_argument("--qps-capacity", type=int, default=16)
    p.add_argument("--strict-gating", action="store_true")
    p.add_argument("--oi-precision", type=int, default=2)
    p.add_argument("--log-level", type=str, default="INFO")
    # scoring weights / thresholds
    p.add_argument("--w-pump", type=float, default=30)
    p.add_argument("--w-cvd", type=float, default=25)
    p.add_argument("--w-oi-delta", type=float, default=12)
    p.add_argument("--w-oi-mtf", type=float, default=8)
    p.add_argument("--w-oi-fuel", type=float, default=8)
    p.add_argument("--w-whale", type=float, default=15)
    p.add_argument("--w-funding", type=float, default=10)
    p.add_argument("--min-conf", type=float, default=0.0)
    # modes
    p.add_argument("--mode", type=str, default="live")
    p.add_argument("--venue", type=str, default="BINANCE")
    p.add_argument("--symbol", type=str, default="BTCUSDT")
    p.add_argument("--start", type=str, default="")
    p.add_argument("--end", type=str, default="")
    a = p.parse_args(argv)
    ex = [e.strip().upper() for e in a.exchanges.split(",") if e.strip()]
    return Config(
        exchanges=ex, max_symbols=a.max_symbols, streams_per_ws=a.streams_per_ws, pump_threshold_pct=a.pump_threshold_pct,
        oi_poll_secs=a.oi_poll_secs, score_interval_secs=a.score_interval_secs, top_k=a.top_k, atr_len=a.atr_len, atr_k=a.atr_k,
        run_seconds=a.run_seconds, warmup_min_trades=a.warmup_min_trades, qps_limit=a.qps_limit, qps_capacity=a.qps_capacity,
        strict_gating=a.strict_gating, oi_precision=a.oi_precision, log_level=a.log_level,
        w_pump=a.w_pump, w_cvd=a.w_cvd, w_oi_delta=a.w_oi_delta, w_oi_mtf=a.w_oi_mtf, w_oi_fuel=a.w_oi_fuel, w_whale=a.w_whale, w_funding=a.w_funding,
        mode=a.mode, venue=a.venue, symbol=a.symbol, start=a.start, end=a.end, min_conf=a.min_conf
    )

async def main():
    cfg = _parse_args(sys.argv[1:])
    scanner = Scanner(cfg)
    try:
        if cfg.mode.lower() == "backtest":
            async with scanner as s:
                await s.backtest()
        else:
            await scanner.__aenter__(); await scanner.run()
    except asyncio.CancelledError:
        print("[MAIN] Main task cancelled.")
    except BaseException as e:
        print(f"[MAIN] Unhandled exception: {e!r}")
    finally:
        if scanner and scanner.session: await scanner.__aexit__(None, None, None)
        await asyncio.sleep(0.3)
        print("[MAIN] Finished.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] KeyboardInterrupt during startup/shutdown. Exiting.")
    except Exception as e:
        print(f"[TOPLEVEL] Script failed: {e!r}")