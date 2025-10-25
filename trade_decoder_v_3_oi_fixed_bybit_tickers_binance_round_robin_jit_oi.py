#!/usr/bin/env python3
"""
Trade Decoder — v3 OI‑Fixed (Bybit tickers + Binance round‑robin + JIT OI)
-----------------------------------------------------------------------------
What’s new in this OI‑fixed build:
  • **Bybit OI source switched to /v5/market/tickers** (single bulk call returns
    `openInterest` for all symbols every cycle) → immediate, non‑zero OI.
  • **Binance OI polling is round‑robin** across symbols to avoid rate spikes and
    long loops; safer + more frequent OI for active symbols.
  • **JIT OI refresh for Top‑K** right before printing the table to ensure the
    displayed rows always have the freshest OI.
  • **Five OI columns** retained: OI 6h %, OI Fuel 1h %, OI MTF, Unwind 15m,
    OI$ Pulse 15m %.
  • **More precise OIΔ (+.2f)** in TRIGGERS so small moves don’t round to 0.0.

Notes:
  • Uses free official endpoints only. Designed to stay below rate limits and
    favor WebSockets for realtime, per the anti‑ban blueprint.

Run:
  python3 trade_decoder_v3_oi_fixed.py --exchanges BINANCE,BYBIT --max-symbols 0 --top-k 5
"""

import os, sys, time, math, uuid, hashlib, random, signal, statistics, asyncio
from dataclasses import dataclass, field
from typing import Dict, Deque, List, Tuple, Optional
from collections import deque
from datetime import datetime, timezone
from tabulate import tabulate

try:
    import aiohttp
    try:
        import orjson as json
    except Exception:
        import json  # type: ignore
    try:
        import orjson as _orjson
        def _jdumps(obj):
            return _orjson.dumps(obj).decode()
        def _jloads(s):
            if isinstance(s, (bytes, bytearray, memoryview)):
                return _orjson.loads(s)
            return _orjson.loads(s.encode())
    except Exception:
        import json as _pyjson
        def _jdumps(obj):
            return _pyjson.dumps(obj)
        def _jloads(s):
            if isinstance(s, (bytes, bytearray, memoryview)):
                s = s.decode()
            return _pyjson.loads(s)
    try:
        import uvloop  # type: ignore
        UVLOOP = True
    except Exception:
        UVLOOP = False
except Exception as e:
    print("[ERROR] Missing deps. pip install aiohttp orjson uvloop tabulate"); raise

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

# ----------------------------- state -------------------------------

@dataclass
class TradeStats:
    cvd: float = 0.0
    cvd_series: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=300))
    sizes: Deque[float] = field(default_factory=lambda: deque(maxlen=300))

@dataclass
class OIStats:
    last_oi: Optional[float] = None
    prev_oi: Optional[float] = None
    last_mark_price: Optional[float] = None
    hist: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=2048))  # (ts, oi)

    def update(self, oi: float, ts: Optional[float] = None):
        ts = ts or time.time()
        self.prev_oi = self.last_oi
        self.last_oi = oi
        if not self.hist or ts - self.hist[-1][0] >= 30:
            self.hist.append((ts, oi))

    @property
    def oi_delta_pct(self) -> float:
        if self.last_oi is None or self.prev_oi is None or self.prev_oi == 0:
            return 0.0
        return (self.last_oi - self.prev_oi) / self.prev_oi * 100.0

    def _value_at_age(self, age_seconds: int) -> Optional[float]:
        if not self.hist: return None
        cutoff = time.time() - age_seconds
        for i in range(len(self.hist)-1, -1, -1):
            ts, v = self.hist[i]
            if ts <= cutoff:
                return v
        return self.hist[0][1] if self.hist else None

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
    price_hist: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=2048))

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
        return self.price_hist[0][1] if self.price_hist else None

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

    def cvd_slope_per_min(self) -> float:
        s = list(self.trade_stats.cvd_series)
        if len(s) < 2: return 0.0
        t0, v0 = s[0]; t1, v1 = s[-1]
        dt_min = max((t1 - t0) / 60.0, 1e-9)
        return (v1 - v0) / dt_min

    def cvd_z_score(self) -> float:
        s = [v for (_, v) in self.trade_stats.cvd_series]
        if len(s) < 10: return 0.0
        med = statistics.median(s); mad = median_abs_deviation(s)
        if mad == 0: return 0.0
        z = modified_z_score(s[-1], med, mad)
        return max(min(z, 5.0), -5.0)

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
                        # log common ban/limit statuses for visibility
                        if r.status in (418,429):
                            print(f"[{self.NAME}-REST] Status {r.status} for {url.split('/')[-1]} params={params}")
                        await asyncio.sleep(0.25); continue
                    return _jloads(await r.read())
            except Exception as e:
                await asyncio.sleep(0.35)
        return None
    async def get_universe(self) -> List[str]: raise NotImplementedError
    def shard(self, universe: List[str], size: int) -> List[List[str]]: return [universe[i:i+size] for i in range(0, len(universe), size)]
    async def ws_consume(self, scanner, shard_id: int, symbols: List[str]): raise NotImplementedError
    async def poll_open_interest(self, scanner): raise NotImplementedError
    async def snapshot_oi(self, symbol: str) -> Optional[float]: raise NotImplementedError

class BinanceConnector(BaseConnector):
    NAME = "BINANCE"
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self._rr_cursor = 0  # round‑robin cursor for OI polling

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
                if not scanner._shutdown.is_set():
                    await asyncio.sleep(jitter)
                else:
                    break
        print(f"[BINANCE-WS{shard_id}] Exiting WS consumer loop.")

    async def poll_open_interest(self, scanner):
        # Round‑robin: update a slice of symbols each cycle to avoid long loops & bans
        SLICE = 60  # tune: how many symbols to hit per cycle
        while not scanner._shutdown.is_set():
            all_syms = [s.symbol for s in scanner.states.values() if s.exch == self.NAME]
            if not all_syms:
                await asyncio.sleep(self.cfg.oi_poll_secs); continue
            start_idx = self._rr_cursor
            end_idx = min(start_idx + SLICE, len(all_syms))
            batch = all_syms[start_idx:end_idx]
            if not batch:  # wrap
                self._rr_cursor = 0
                continue
            for sym in batch:
                if scanner._shutdown.is_set(): break
                try:
                    data = await self._rest_json("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": sym})
                    oi = float(data.get("openInterest","0")) if data else 0.0
                    st = scanner.states.get(f"{self.NAME}:{sym}")
                    if st: st.oi_stats.update(oi)
                except Exception: pass
                await asyncio.sleep(0.01)
            self._rr_cursor = end_idx if end_idx < len(all_syms) else 0
            try:
                await asyncio.wait_for(scanner._shutdown.wait(), timeout=self.cfg.oi_poll_secs)
                break
            except asyncio.TimeoutError:
                pass
        print("[BINANCE-OI] Exiting OI poller loop.")

    async def snapshot_oi(self, symbol: str) -> Optional[float]:
        data = await self._rest_json("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": symbol})
        if data:
            try: return float(data.get("openInterest","0"))
            except Exception: return None
        return None

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
        url = "wss://stream.bybit.com/v5/public/linear"
        args = self._topics(symbols); sub = {"op": "subscribe", "args": args}
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
                if not scanner._shutdown.is_set():
                    await asyncio.sleep(jitter)
                else:
                    break
        print(f"[BYBIT-WS{shard_id}] Exiting WS consumer loop.")

    async def poll_open_interest(self, scanner):
        # Bulk pull via /v5/market/tickers → has openInterest for all linear symbols
        while not scanner._shutdown.is_set():
            try:
                data = await self._rest_json("https://api.bybit.com/v5/market/tickers", params={"category": "linear"})
                if data and data.get("result") and data["result"].get("list"):
                    for it in data["result"]["list"]:
                        sym = it.get("symbol"); oi_str = it.get("openInterest")
                        if not sym or oi_str is None: continue
                        try:
                            oi = float(oi_str)
                        except Exception:
                            continue
                        st = scanner.states.get(f"{self.NAME}:{sym}")
                        if st: st.oi_stats.update(oi)
            except Exception:
                pass
            try:
                await asyncio.wait_for(scanner._shutdown.wait(), timeout=self.cfg.oi_poll_secs)
                break
            except asyncio.TimeoutError:
                pass
        print("[BYBIT-OI] Exiting OI poller loop.")

    async def snapshot_oi(self, symbol: str) -> Optional[float]:
        t = await self._rest_json("https://api.bybit.com/v5/market/tickers", params={"category": "linear", "symbol": symbol})
        try:
            it = (t or {}).get("result", {}).get("list", [])
            if it:
                return float(it[0].get("openInterest"))
        except Exception:
            return None
        return None

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
        headers = {"User-Agent": "trade-decoder-v3-oi-fixed/1.0"}
        timeout = aiohttp.ClientTimeout(sock_read=30, total=60)
        self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        for con in self.cons.values(): con.set_session(self.session)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        print("[SHUTDOWN] Signaling tasks to stop...")
        self._shutdown.set()
        if self.session:
            print("[SHUTDOWN] Closing HTTP session...")
            await self.session.close(); await asyncio.sleep(0.25)
        print("[SHUTDOWN] Session closed.")

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
                p = float(data["p"]); q = float(data["q"]); m = bool(data["m"])  # m=True means SELL aggressor
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
            print(f"[WARN] Error processing {topic} data: {e!r}. Payload: {payload}")

    # ---- scoring ----
    def _score(self, st: SymbolState) -> Tuple[float, dict]:
        pump_z = max((st.pump_pct - self.cfg.pump_threshold_pct)/5.0, 0.0)
        oi_z   = max((st.oi_stats.oi_delta_pct - 0.5)/0.5, 0.0)  # more sensitive
        cvd_z  = st.cvd_z_score()
        whale_z = max(min(st.last_whale_z/3.0, 1.5), 0.0)
        fr     = st.funding_rate if st.funding_rate is not None else 0.0
        fr_z   = max(fr*100.0/0.02, 0.0) if fr != 0 else 0.0
        conf   = 30*pump_z + 25*max(cvd_z,0.0) + 20*oi_z + 15*whale_z + 10*fr_z
        if self.cfg.strict_gating:
            if st.pump_pct < self.cfg.pump_threshold_pct and st.cvd_slope_per_min() <= 0:
                conf = 0.0
        return max(0.0, min(conf, 100.0)), {
            "pump_pct": st.pump_pct,
            "oi_delta_pct": st.oi_stats.oi_delta_pct,
            "cvd_slope": st.cvd_slope_per_min(),
            "whale_z": st.last_whale_z,
            "funding_rate": fr,
        }

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
                if data and data.get("result") and data["result"].get("list"):
                    for row in data["result"]["list"]:
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

    async def _jit_refresh_oi(self, tops: List[Tuple[str, SymbolState, float, dict]]):
        # Pull fresh OI snapshots for the displayed rows to avoid 0.0s
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
            if conf > 0: rows.append((key, st, conf, details))
        rows.sort(key=lambda x: (x[2], x[0]), reverse=True)
        timestamp_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        title = f"✨ Trade Decoder Top {min(len(rows), self.cfg.top_k)} • run_id {self.run_id[:8]} • {timestamp_str}"
        if not rows:
            print(f"\n=== Warm-up: {warm_sym} have 4h+price, {warm_trade} have ≥{self.cfg.warmup_min_trades} trades, {warm_oi} have OI — scanning... ===\n"); return

        top = rows[: self.cfg.top_k]
        # JIT OI to ensure table shows non‑zero values where possible
        try:
            await self._jit_refresh_oi(top)
        except Exception as e:
            print(f"[JIT-OI] Error: {e!r}")

        plan_tasks = [self._plan(st) for key, st, conf, details in top]
        try:
            plans = await asyncio.gather(*plan_tasks)
        except Exception as e:
            print(f"[WARN] Error generating trade plans: {e!r}"); plans = [(0.0,)*6] * len(top)

        headers = [
            "EX","SYMBOL","ENTRY","SL 🛡️","TP1 🎯","TP2 🎯","TP3 🎯",
            "CONF 📈","RR (TP1)","DECAY ⏳",
            "OI 6h %","OI Fuel 1h %","OI MTF","Unwind 15m","OI$ Pulse 15m %",
            "TRIGGERS",
        ]
        table_data: List[List[str]] = []
        for i, (key, st, conf, details) in enumerate(top):
            entry, sl, tp1, tp2, tp3, rr = plans[i]
            if entry <= 0 or sl <= 0 or tp1 <= entry or sl >= entry: continue

            oi_6h = st.oi_stats.pchange_minutes(360)
            oi_fuel_1h = st.oi_stats.pchange_between(60, 120)
            oi_mtf = (st.oi_stats.pchange_minutes(25) + st.oi_stats.pchange_minutes(60) + st.oi_stats.pchange_minutes(180)) / 3.0
            unwind_15m = -st.oi_stats.pchange_minutes(15)
            oi_dollar_pulse = st.oi_dollar_pulse_15m()

            fr = (st.funding_rate or 0.0); fr_str = f"{fr:+.4f}"
            fr_emoji = "💰" if fr <= -0.0002 else ("🔥" if fr >= 0.0003 else "⚪")
            trig = " | ".join([
                f"pump {st.pump_pct:+.1f}%",
                f"OIΔ {st.oi_stats.oi_delta_pct:+.{self.cfg.oi_precision}f}%",
                f"CVD {st.cvd_slope_per_min():+.0f}/m",
                f"🐋z {max(min(st.last_whale_z,5.0),-5.0):+.1f}",
                f"fr {fr_emoji} {fr_str}",
            ])

            prec = 4 if entry < 10 else (3 if entry < 100 else 2)
            table_data.append([
                st.exch, st.symbol,
                f"{entry:.{prec}f}", f"{sl:.{prec}f}", f"{tp1:.{prec}f}", f"{tp2:.{prec}f}", f"{tp3:.{prec}f}",
                f"{conf:5.1f}", f"{rr:.1f}", f"{self._decay(st)}m",
                f"{oi_6h:+.2f}%", f"{oi_fuel_1h:+.2f}%", f"{oi_mtf:+.2f}", f"{unwind_15m:+.2f}%", f"{oi_dollar_pulse:+.2f}%",
                trig,
            ])

        print(f"\n{title}\n")
        if not table_data:
            print("--- No valid trade plans generated for top candidates. ---")
        else:
            print(tabulate(
                table_data,
                headers=headers,
                tablefmt="grid",
                numalign="right",
                stralign="left",
                maxcolwidths=[None,None,None,None,None,None,None,None,None,None,10,12,8,11,15,65],
            ))
        print()

    async def run(self):
        if UVLOOP: import uvloop as _uv; _uv.install()
        # build universes
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
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
        print("[SCORER] Exiting scoring loop.")

# ------------------------------- CLI -------------------------------

def _parse_args(argv: List[str]) -> Config:
    import argparse
    p = argparse.ArgumentParser(description="Trade Decoder — v3 OI‑Fixed")
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
    a = p.parse_args(argv)
    ex = [e.strip().upper() for e in a.exchanges.split(",") if e.strip()]
    return Config(
        exchanges=ex, max_symbols=a.max_symbols, streams_per_ws=a.streams_per_ws, pump_threshold_pct=a.pump_threshold_pct,
        oi_poll_secs=a.oi_poll_secs, score_interval_secs=a.score_interval_secs, top_k=a.top_k, atr_len=a.atr_len, atr_k=a.atr_k,
        run_seconds=a.run_seconds, warmup_min_trades=a.warmup_min_trades, qps_limit=a.qps_limit, qps_capacity=a.qps_capacity,
        strict_gating=a.strict_gating, oi_precision=a.oi_precision,
    )

async def main():
    cfg = _parse_args(sys.argv[1:])
    scanner = Scanner(cfg)
    try:
        await scanner.__aenter__(); await scanner.run()
    except asyncio.CancelledError:
        print("[MAIN] Main task cancelled.")
    except BaseException as e:
        print(f"[MAIN] Unhandled exception: {e!r}")
    finally:
        if scanner: await scanner.__aexit__(None, None, None)
        await asyncio.sleep(0.5)
        print("[MAIN] Script finished.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MAIN] KeyboardInterrupt during startup/shutdown. Exiting.")
    except Exception as e:
        print(f"[TOPLEVEL] Script failed: {e!r}")
