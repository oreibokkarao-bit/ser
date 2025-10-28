#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ignition & Fuel — Ultra WS Scanner (Top‑N) — Binance/Bybit/Bitget
Doc-driven upgrades (honest, free endpoints only).

New vs pro_plus:
- Prob. calibration hooks: logistic + isotonic LUT (file-based)
- Composite score on normalized features (rolling Z)
- Multi‑TF CVD & OI deltas
- Order‑Book Imbalance (OBI) for Top‑N via depth WS
- Optional liquidation bus tracking (!forceOrder@arr)
- 1m bar aggregator + ATR(14) and optional ATR-based SL/TP
- Macro regime veto via BTCUSDT 1m → 60m drawdown/volatility
- Coverage Guard → detects 24h >|10%| movers and triggers shard reload
- JSONL persistence and optional PNG heatmap exports for Top‑N books
- Optional light VaR gate (volatility budget approximation)

All APIs/streams are official & free.
"""

import asyncio
import json
import logging
import math
import os
import random
import signal
import sys
import time
import hashlib
from math import ceil
from dataclasses import dataclass, field
from collections import deque, defaultdict
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlparse, urlencode

# Third-party
import aiohttp
import websockets
import numpy as np
import pandas as pd
from tabulate import tabulate

try:
    import psutil  # optional
except Exception:
    psutil = None

# ------------------------------
# CLI / Config
# ------------------------------
import argparse

def parse_args():
    p = argparse.ArgumentParser(description="Ignition & Fuel — Ultra WS Scanner (Top‑N)")
    p.add_argument("--config", type=str, default="", help="Path to optional JSON config file")
    p.add_argument("--top-n", type=int, default=3)
    p.add_argument("--usdph", type=float, default=100_000.0)
    p.add_argument("--rr-min", type=float, default=1.2)
    p.add_argument("--wp-min", type=float, default=0.55)
    p.add_argument("--analysis-sec", type=int, default=60)
    p.add_argument("--analysis-fast-sec", type=int, default=20)
    p.add_argument("--freeze-sec", type=int, default=180)
    p.add_argument("--corr-max", type=float, default=0.90)
    p.add_argument("--slip-buckets", type=str, default="1000,5000,10000")
    p.add_argument("--lut", type=str, default="", help="Isotonic LUT JSON for win% mapping: {'z':[...],'p':[...]}")
    p.add_argument("--logreg", type=str, default="", help="Logistic calibration JSON: {'w0':..,'w1':..} on z")
    p.add_argument("--json-logs", action="store_true")
    p.add_argument("--metrics", action="store_true")
    p.add_argument("--heatmap", action="store_true", help="Export Top‑N depth heatmaps as PNG (matplotlib required)")
    p.add_argument("--save-jsonl", type=str, default="", help="Directory to write JSONL artifacts (features/picks)")
    # ATR & risk gates
    p.add_argument("--atr-risk", action="store_true", help="Use ATR(14) based SL/TP instead of fixed %")
    p.add_argument("--atr-len", type=int, default=14)
    p.add_argument("--sl-atr", type=float, default=1.4)
    p.add_argument("--tp-atr", type=str, default="0.8,1.5,2.3")
    p.add_argument("--var-budget", type=float, default=0.0, help="Approx VaR budget in USD per position (0=off)")
    # Macro gate
    p.add_argument("--macro", action="store_true", help="Enable macro regime veto from BTCUSDT 1m→60m drawdown/vol")
    # Liquidation bus
    p.add_argument("--liq-bus", action="store_true", help="Track Binance liquidation bus (!forceOrder@arr)")
    return p.parse_args()

ARGS = parse_args()

def load_config(path: str) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load config file {path}: {e}", file=sys.stderr)
        return {}

CONF_FILE = load_config(ARGS.config)

# ------------------------------
# Logging
# ------------------------------
def setup_logging(json_logs: bool):
    handlers = [logging.FileHandler("trading_bot.log"), logging.StreamHandler()]
    fmt = "%(message)s" if json_logs else "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)

setup_logging(ARGS.json_logs)
logger = logging.getLogger("ignition_fuel_ultra")

def slog(event: str, **kv):
    if ARGS.json_logs:
        row = {"ts": time.time(), "event": event}; row.update(kv)
        print(json.dumps(row, ensure_ascii=False))
    else:
        logger.info(f"{event} | " + " ".join(f"{k}={v}" for k, v in kv.items()))

# ------------------------------
# Global Config
# ------------------------------
class Config:
    UNIVERSE_REFRESH_SEC = CONF_FILE.get("universe_refresh_sec", 15*60)
    BINANCE_SHARD_SIZE = CONF_FILE.get("binance_shard_size", 400)
    BINANCE_MAX_CONNS  = CONF_FILE.get("binance_max_conns", 4)
    MAX_TOTAL_STREAMS  = CONF_FILE.get("max_total_streams", 1600)
    BYBIT_BATCH_SUB_SIZE = CONF_FILE.get("bybit_batch_sub_size", 150)
    BITGET_BATCH_SUB_SIZE= CONF_FILE.get("bitget_batch_sub_size", 150)
    ANALYSIS_EVERY_SEC = ARGS.analysis_sec or CONF_FILE.get("analysis_every_sec", 60)
    ANALYSIS_FAST_SEC  = ARGS.analysis_fast_sec or CONF_FILE.get("analysis_fast_sec", 20)
    TRADES_MAXLEN_HI = CONF_FILE.get("trades_maxlen_hi", 1500)
    TRADES_MAXLEN_LO = CONF_FILE.get("trades_maxlen_lo", 600)
    LIQ_WINDOW_SEC   = CONF_FILE.get("liq_window_sec", 3600)
    # default % levels (overridden by ATR mode)
    TP1_PCT = CONF_FILE.get("tp1_pct", 0.12)
    TP2_PCT = CONF_FILE.get("tp2_pct", 0.22)
    TP3_PCT = CONF_FILE.get("tp3_pct", 0.35)
    SL_PCT  = CONF_FILE.get("sl_pct", 0.10)
    MIN_USD_PER_HOUR = ARGS.usdph or CONF_FILE.get("min_usd_per_hour", 100_000.0)
    MIN_RR_TP1       = ARGS.rr_min or CONF_FILE.get("min_rr_tp1", 1.2)
    MIN_WIN_PROB     = ARGS.wp_min or CONF_FILE.get("min_win_prob", 0.55)
    TOP_N = ARGS.top_n or CONF_FILE.get("top_n", 3)
    FREEZE_SEC = ARGS.freeze_sec or CONF_FILE.get("freeze_sec", 180)
    CORR_MAX   = ARGS.corr_max or CONF_FILE.get("corr_max", 0.90)

class ConfigData:
    CACHE_DIR = CONF_FILE.get("cache_dir", ".cache/http")
    DEFAULT_TTL = CONF_FILE.get("http_default_ttl", 60)
    RL = CONF_FILE.get("rate_limits", {
        "fapi.binance.com": (10, 5),
        "api.bybit.com":    (6, 3),
        "api.bitget.com":   (6, 3),
        "api.binance.com":  (5, 3),  # for BTCUSDT spot klines
    })
    REST_CONCURRENCY = CONF_FILE.get("rest_concurrency", 12)
    CACHE_MAX_AGE = CONF_FILE.get("cache_max_age", 3600*6)
    CACHE_MAX_FILES = CONF_FILE.get("cache_max_files", 8000)

class ConfigEnrichers:
    ENABLE_OI = CONF_FILE.get("enable_oi", True)
    ENABLE_FUNDING = CONF_FILE.get("enable_funding", True)
    ENABLE_DEPTH = CONF_FILE.get("enable_depth", True)
    ENABLE_BINANCE_MARKPRICE_WS = CONF_FILE.get("enable_binance_markprice_ws", True)
    DEPTH_SNAPSHOT_TTL = CONF_FILE.get("depth_snapshot_ttl", 2)
    DEPTH_LIMIT = CONF_FILE.get("depth_limit", 1000)

SLIP_BUCKETS = [int(x) for x in (ARGS.slip_buckets.split(",") if ARGS.slip_buckets else ["1000","5000","10000"])]

# calibration files
ISO_LUT = None
if ARGS.lut:
    try:
        with open(ARGS.lut, "r", encoding="utf-8") as f:
            ISO_LUT = json.load(f)
    except Exception as e:
        slog("iso_lut_load_fail", error=str(e))
LOGREG = None
if ARGS.logreg:
    try:
        with open(ARGS.logreg, "r", encoding="utf-8") as f:
            LOGREG = json.load(f)  # {"w0":..., "w1":...}
    except Exception as e:
        slog("logreg_load_fail", error=str(e))

# ------------------------------
# Rate limiter + Cache
# ------------------------------
class TokenBucket:
    def __init__(self, capacity: int, rate_per_sec: float):
        self.capacity = capacity
        self.tokens = capacity
        self.rate = rate_per_sec
        self.updated = time.time()
        self._lock = asyncio.Lock()
    async def take(self, tokens=1):
        async with self._lock:
            now = time.time()
            elapsed = now - self.updated
            if elapsed > 0:
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return
        await asyncio.sleep(max(0.05, tokens / self.rate))
        await self.take(tokens)

class RLManager:
    def __init__(self):
        self.buckets = {}
    def _bucket_for(self, host: str) -> TokenBucket:
        if host not in self.buckets:
            cap, rate = ConfigData.RL.get(host, (5,2))
            self.buckets[host] = TokenBucket(cap, rate)
        return self.buckets[host]
    async def wait(self, host: str):
        await self._bucket_for(host).take(1)

class DiskCache:
    def __init__(self, base_dir: str):
        self.base = base_dir
        os.makedirs(self.base, exist_ok=True)
    def _key(self, url: str, params: Optional[dict]):
        raw = url + ("?" + urlencode(sorted(params.items())) if params else "")
        h = hashlib.sha256(raw.encode()).hexdigest()
        host = urlparse(url).hostname or "unknown"
        dirp = os.path.join(self.base, host)
        os.makedirs(dirp, exist_ok=True)
        return os.path.join(dirp, h + ".json")
    def load(self, url: str, params: Optional[dict], ttl: int):
        path = self._key(url, params)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if (time.time() - doc.get("ts", 0)) <= ttl:
                return doc
            return doc
        except Exception:
            return None
    def save(self, url: str, params: Optional[dict], payload: dict, headers: dict):
        path = self._key(url, params)
        out = {"ts": time.time(), "headers": {k.lower(): v for k, v in headers.items()}, "payload": payload}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f)

_rl = RLManager()
_cache = DiskCache(ConfigData.CACHE_DIR)

async def http_get_json_cached(session: aiohttp.ClientSession, url, params=None, ttl=ConfigData.DEFAULT_TTL, timeout=15):
    host = urlparse(url).hostname or "unknown"
    cached = _cache.load(url, params, ttl)
    headers = {}
    if cached and "headers" in cached:
        etag = cached["headers"].get("etag")
        if etag: headers["If-None-Match"] = etag
        lm = cached["headers"].get("last-modified")
        if lm: headers["If-Modified-Since"] = lm
    for attempt in range(5):
        try:
            await _rl.wait(host)
            async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                if resp.status == 304 and cached:
                    _cache.save(url, params, cached["payload"], dict(resp.headers))
                    return cached["payload"]
                if resp.status in (429,418,451,403,500,503):
                    txt = await resp.text()
                    slog("http_backoff", status=resp.status, url=url, msg=txt[:160], attempt=attempt)
                    await asyncio.sleep((2**attempt)+random.random())
                    continue
                resp.raise_for_status()
                try:
                    data = await resp.json()
                except Exception:
                    txt = await resp.text()
                    raise RuntimeError(f"Non-JSON response from {url}: {txt[:160]}")
                _cache.save(url, params, data, dict(resp.headers)); return data
        except Exception as e:
            slog("http_retry", url=url, error=str(e), attempt=attempt); await asyncio.sleep((2**attempt)+random.random())
    raise RuntimeError(f"GET failed for {url} after retries")

async def cache_janitor():
    while True:
        await asyncio.sleep(900)
        try:
            base = ConfigData.CACHE_DIR
            if not os.path.isdir(base): continue
            entries = []
            for root, _, files in os.walk(base):
                for fn in files:
                    if fn.endswith(".json"):
                        p = os.path.join(root, fn)
                        try: st = os.stat(p); entries.append((p, st.st_mtime))
                        except Exception: pass
            now = time.time()
            for p, m in entries:
                if now - m > ConfigData.CACHE_MAX_AGE:
                    try: os.remove(p)
                    except Exception: pass
            entries = [(p, os.stat(p).st_mtime) for p,_ in entries if os.path.exists(p)]
            if len(entries) > ConfigData.CACHE_MAX_FILES:
                entries.sort(key=lambda x: x[1])
                for i in range(len(entries)-ConfigData.CACHE_MAX_FILES):
                    try: os.remove(entries[i][0])
                    except Exception: pass
            slog("cache_janitor_done")
        except Exception as e:
            slog("cache_janitor_err", error=str(e))

# ------------------------------
# Universe
# ------------------------------
class Universe:
    def __init__(self, http: aiohttp.ClientSession, sem: asyncio.Semaphore):
        self.http = http; self.sem = sem
        self.binance_fut: List[str] = []; self.bybit_linear: List[str] = []; self.bitget_usdt_fut: List[str] = []

    async def refresh(self):
        await asyncio.gather(self._refresh_binance_futures(), self._refresh_bybit_linear(), self._refresh_bitget_usdt_futures())
        total = len(self.binance_fut) + len(self.bybit_linear) + len(self.bitget_usdt_fut)
        slog("universe", binance=len(self.binance_fut), bybit=len(self.bybit_linear), bitget=len(self.bitget_usdt_fut), total=total)

    async def _refresh_binance_futures(self):
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        async with self.sem: data = await http_get_json_cached(self.http, url, ttl=600)
        syms = []
        for s in data.get("symbols", []):
            if s.get("contractType")=="PERPETUAL" and s.get("quoteAsset")=="USDT" and s.get("status")=="TRADING":
                syms.append(s["symbol"])
        self.binance_fut = sorted(set(syms))

    async def _refresh_bybit_linear(self):
        base = "https://api.bybit.com/v5/market/instruments-info"
        params = {"category":"linear"}
        out, cursor = [], None
        while True:
            p = dict(params); 
            if cursor: p["cursor"]=cursor
            async with self.sem: data = await http_get_json_cached(self.http, base, params=p, ttl=600)
            list_ = data.get("result",{}).get("list",[]) or []
            for item in list_:
                if item.get("quoteCoin")=="USDT" and item.get("status")=="Trading": out.append(item["symbol"])
            cursor = data.get("result",{}).get("nextPageCursor")
            if not cursor: break
        self.bybit_linear = sorted(set(out))

    async def _refresh_bitget_usdt_futures(self):
        base = "https://api.bitget.com/api/v3/market/instruments"
        params = {"category":"USDT-FUTURES"}
        async with self.sem: data = await http_get_json_cached(self.http, base, params=params, ttl=600)
        out = []
        for item in (data.get("data",[]) or []):
            if item.get("quoteCoin")=="USDT" and item.get("status")=="online": out.append(item["symbol"])
        self.bitget_usdt_fut = sorted(set(out))

# ------------------------------
# Bars & ATR
# ------------------------------
class BarAgg:
    """1m bar aggregator for ATR and macro gating (OHLC based on trades)."""
    def __init__(self, n=14):
        self.n = n
        self.bars: deque = deque(maxlen=2000)  # (t_minute, o,h,l,c)
        self.prev_close: Optional[float] = None

    def tick(self, ts: float, price: float):
        minute = int(ts // 60)
        if not self.bars or self.bars[-1][0] != minute:
            # start new bar
            self.bars.append([minute, price, price, price, price])
        else:
            b = self.bars[-1]
            b[2] = max(b[2], price); b[3] = min(b[3], price); b[4] = price

    def atr(self) -> Optional[float]:
        if len(self.bars) < self.n+1: return None
        TR = []
        prev_close = None
        for i, b in enumerate(self.bars):
            _, o,h,l,c = b
            if i == 0:
                prev_close = c; continue
            tr = max(h-l, abs(h-prev_close), abs(l-prev_close))
            TR.append(tr); prev_close = c
        if len(TR) < self.n: return None
        arr = np.array(TR[-self.n:], dtype=float)
        return float(arr.mean())

# ------------------------------
# Market Data Structures
# ------------------------------
class MarketData:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.trades: deque = deque(maxlen=Config.TRADES_MAXLEN_HI)
        self.cvd = 0.0
        self.cvd_ring: deque = deque(maxlen=3000)  # (ts, cvd) for MTF deltas
        self.slope_hist: deque = deque(maxlen=200)
        self.notional_window: deque = deque(maxlen=Config.TRADES_MAXLEN_HI)
        self.trade_times: deque = deque(maxlen=1000)
        self.last_retune = time.time()
        self.bars = BarAgg(n=ARGS.atr_len)

    def retune_retention(self):
        now = time.time()
        if now - self.last_retune < 60: return
        usdph = self.usd_per_hour(now)
        maxlen = Config.TRADES_MAXLEN_HI if usdph >= 500_000 else Config.TRADES_MAXLEN_LO
        if self.trades.maxlen != maxlen:
            self.trades = deque(list(self.trades)[-maxlen:], maxlen=maxlen)
            self.notional_window = deque(list(self.notional_window)[-maxlen:], maxlen=maxlen)
        self.last_retune = now

    def add_trade(self, price: float, qty: float, side: str, ts: float):
        signed = qty if side.lower().startswith("b") else -qty
        self.cvd += signed
        self.trades.append({"ts": ts, "price": price, "qty": qty, "side": side.lower()})
        self.notional_window.append((ts, price * qty))
        self.trade_times.append(ts)
        self.cvd_ring.append((ts, self.cvd))
        self.bars.tick(ts, price)
        self.retune_retention()

    def usd_per_hour(self, now_ts: float) -> float:
        cutoff = now_ts - Config.LIQ_WINDOW_SEC
        while self.notional_window and self.notional_window[0][0] < cutoff:
            self.notional_window.popleft()
        if not self.notional_window: return 0.0
        total = sum(n for _, n in self.notional_window)
        window = max(1.0, min(Config.LIQ_WINDOW_SEC,
                              (self.notional_window[-1][0] - self.notional_window[0][0]) if len(self.notional_window) > 1 else 1.0))
        return total * (3600.0 / window)

    def trades_per_sec(self, now_ts: float) -> float:
        cutoff = now_ts - 60.0
        while self.trade_times and self.trade_times[0] < cutoff:
            self.trade_times.popleft()
        if not self.trade_times: return 0.0
        span = max(1.0, self.trade_times[-1] - self.trade_times[0])
        return len(self.trade_times) / span

    def median_trade_size(self) -> float:
        if not self.trades: return 0.0
        sizes = [t["qty"]*t["price"] for t in list(self.trades)[-200:]]
        if not sizes: return 0.0
        arr = np.array(sizes, dtype=float); return float(np.median(arr))

    def cvd_delta(self, window_sec: int) -> Optional[float]:
        now = time.time()
        # find value at now - window
        base = None; last = None
        for ts, v in reversed(self.cvd_ring):
            if last is None: last = v
            if now - ts >= window_sec: base = v; break
        if base is None or last is None:
            return None
        return float(last - base)

# ------------------------------
# Connectors (Trades)
# ------------------------------
class BinanceFuturesConnector:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.data: Dict[str, MarketData] = defaultdict(lambda: MarketData(""))
        self.tasks: List[asyncio.Task] = []
        self._shard_urls: List[str] = []

    def shards(self, symbols: Optional[List[str]] = None):
        syms = symbols if symbols is not None else self.symbols
        total = len(syms)
        max_streams = min(Config.MAX_TOTAL_STREAMS, Config.BINANCE_MAX_CONNS * Config.BINANCE_SHARD_SIZE)
        if total > max_streams:
            slog("stream_guardrail", total=total, capped=max_streams)
        total = min(total, max_streams)
        n = max(1, min(Config.BINANCE_MAX_CONNS, ceil(total / Config.BINANCE_SHARD_SIZE)))
        chunks = [syms[i::n] for i in range(n)]
        for chunk in chunks:
            if not chunk: continue
            streams = "/".join([f"{s.lower()}@aggTrade" for s in chunk])
            url = f"wss://fstream.binance.com/stream?streams={streams}"
            yield url, chunk

    async def listen_shard(self, url):
        name = "BinanceFut"
        while True:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=15) as ws:
                    slog("ws_connected", venue=name, shard=url[-72:])
                    async for raw in ws:
                        msg = json.loads(raw); stream = msg.get("stream",""); data = msg.get("data",{})
                        if not stream or "@aggTrade" not in stream: continue
                        s = data["s"]; p = float(data["p"]); q = float(data["q"]); is_bm = data["m"]
                        ts = float(data.get("T", time.time()))/1000.0
                        md = self.data.setdefault(s, MarketData(s))
                        md.add_trade(p, q, "sell" if is_bm else "buy", ts)
            except Exception as e:
                backoff = 1.5 + random.random()*2.0
                slog("ws_error_reconnect", venue=name, error=str(e), backoff=backoff)
                await asyncio.sleep(backoff)

    async def start(self):
        self._shard_urls = []
        for url, _chunk in self.shards():
            self._shard_urls.append(url)
            self.tasks.append(asyncio.create_task(self.listen_shard(url)))

    async def reload(self, new_symbols: List[str]):
        # Gracefully cancel shards and restart with new symbol set
        self.symbols = new_symbols
        for t in self.tasks:
            try: t.cancel(); await t
            except Exception: pass
        self.tasks = []
        await self.start()

class BybitLinearConnector:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.data: Dict[str, MarketData] = defaultdict(lambda: MarketData(""))
        self.ws = None

    async def start(self):
        name = "BybitLinear"; url = "wss://stream.bybit.com/v5/public/linear"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    self.ws = ws; slog("ws_connected", venue=name)
                    args = [f"publicTrade.{s}" for s in self.symbols]
                    for i in range(0, len(args), Config.BYBIT_BATCH_SUB_SIZE):
                        await ws.send(json.dumps({"op":"subscribe","args":args[i:i+Config.BYBIT_BATCH_SUB_SIZE]})); await asyncio.sleep(0.2)
                    async for raw in ws:
                        msg = json.loads(raw); topic = msg.get("topic","")
                        if not topic.startswith("publicTrade."): continue
                        for tr in msg.get("data", []):
                            s = tr["s"]; p = float(tr["p"]); q = float(tr["v"]); side = tr["S"]
                            ts = float(tr.get("T", time.time()))/1000.0
                            md = self.data.setdefault(s, MarketData(s)); md.add_trade(p, q, side, ts)
            except Exception as e:
                backoff = 2.0 + random.random()*2.5
                slog("ws_error_reconnect", venue=name, error=str(e), backoff=backoff); await asyncio.sleep(backoff)

class BitgetUSDTFutConnector:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.data: Dict[str, MarketData] = defaultdict(lambda: MarketData(""))
        self.ws = None

    async def start(self):
        name = "BitgetUSDTFut"; url = "wss://ws.bitget.com/v2/ws/public"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    self.ws = ws; slog("ws_connected", venue=name)
                    args = [{"instType":"USDT-FUTURES","channel":"trade","instId": s} for s in self.symbols]
                    for i in range(0, len(args), Config.BITGET_BATCH_SUB_SIZE):
                        await ws.send(json.dumps({"op":"subscribe","args": args[i:i+Config.BITGET_BATCH_SUB_SIZE]})); await asyncio.sleep(0.25)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("action") != "update" or not msg.get("data"): continue
                        inst = msg.get("arg",{}).get("instId")
                        for tr in msg["data"]:
                            p = float(tr[1]); q = float(tr[2]); side = tr[3]
                            ts = float(tr[0])/1000.0 if isinstance(tr[0], (int,float)) else time.time()
                            md = self.data.setdefault(inst, MarketData(inst)); md.add_trade(p, q, side, ts)
            except Exception as e:
                backoff = 2.0 + random.random()*2.5
                slog("ws_error_reconnect", venue=name, error=str(e), backoff=backoff); await asyncio.sleep(backoff)

# ------------------------------
# Binance Enrichers (Funding/OI + Depth + Liq bus)
# ------------------------------
class BinanceEnricher:
    def __init__(self, http: aiohttp.ClientSession, sem: asyncio.Semaphore):
        self.http = http; self.sem = sem
        self.funding_live: Dict[str, dict] = {}
        self.depth_cache: Dict[str, dict] = {}
        self.oi_ring: Dict[str, deque] = defaultdict(lambda: deque(maxlen=60))  # (ts, oi) approx 1/min
        self.liq_ring: deque = deque(maxlen=3000)  # (ts, symbol, side, qty_usd)

    async def start_markprice_ws(self):
        if not ConfigEnrichers.ENABLE_FUNDING or not ConfigEnrichers.ENABLE_BINANCE_MARKPRICE_WS: return
        url = "wss://fstream.binance.com/ws/!markPrice@arr"; name = "BinanceMarkPrice"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    slog("ws_connected", venue=name)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if not isinstance(msg, list): continue
                        for it in msg:
                            s = it.get("s"); 
                            if not s: continue
                            fr = it.get("r"); nft = it.get("T"); mp = it.get("p")
                            self.funding_live[s] = {"fundingRate": float(fr) if fr is not None else None,
                                                    "nextFundingTime": int(nft) if nft is not None else None,
                                                    "markPrice": float(mp) if mp is not None else None}
            except Exception as e:
                backoff = 2.0 + random.random()*2.0
                slog("ws_error_reconnect", venue=name, error=str(e), backoff=backoff); await asyncio.sleep(backoff)

    async def start_liq_bus(self):
        url = "wss://fstream.binance.com/ws/!forceOrder@arr"; name = "BinanceLiqBus"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    slog("ws_connected", venue=name)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if not isinstance(msg, list): continue
                        for it in msg:
                            try:
                                s = it.get("o",{}).get("s"); S = it.get("o",{}).get("S"); q = float(it.get("o",{}).get("q",0)); p = float(it.get("o",{}).get("p",0))
                                usd = q*p
                                self.liq_ring.append((time.time(), s, S, usd))
                            except Exception:
                                pass
            except Exception as e:
                backoff = 2.0 + random.random()*2.0
                slog("ws_error_reconnect", venue=name, error=str(e), backoff=backoff); await asyncio.sleep(backoff)

    async def get_oi_point(self, symbol: str, ttl=45) -> Optional[float]:
        if not ConfigEnrichers.ENABLE_OI: return None
        url = "https://fapi.binance.com/fapi/v1/openInterest"; params = {"symbol": symbol}
        try:
            async with self.sem: data = await http_get_json_cached(self.http, url, params=params, ttl=ttl)
            oi = float(data.get("openInterest", 0.0)); self.oi_ring[symbol].append((time.time(), oi)); return oi
        except Exception as e:
            slog("oi_point_fail", symbol=symbol, error=str(e)); return None

    def oi_delta_pct(self, symbol: str, window_sec: int) -> Optional[float]:
        ring = self.oi_ring.get(symbol); 
        if not ring: return None
        now = time.time()
        base = None; last = None
        for ts, v in reversed(ring):
            if last is None: last = v
            if now - ts >= window_sec: base = v; break
        if base is None or last is None or base<=0: return None
        return ((last - base)/base)*100.0

    async def get_depth_snapshot(self, symbol: str, limit=ConfigEnrichers.DEPTH_LIMIT, ttl=ConfigEnrichers.DEPTH_SNAPSHOT_TTL):
        if not ConfigEnrichers.ENABLE_DEPTH: return [], [], None
        url = "https://fapi.binance.com/fapi/v1/depth"; params = {"symbol": symbol, "limit": limit}
        try:
            async with self.sem: data = await http_get_json_cached(self.http, url, params=params, ttl=ttl)
            bids = [(float(p), float(q)) for p,q in data.get("bids",[])]
            asks = [(float(p), float(q)) for p,q in data.get("asks",[])]
            self.depth_cache[symbol] = {"ts": time.time(), "bids": bids, "asks": asks, "lastUpdateId": data.get("lastUpdateId")}
            return bids, asks, data.get("lastUpdateId")
        except Exception as e:
            slog("depth_snapshot_fail", symbol=symbol, error=str(e)); return [], [], None

# ------------------------------
# Depth WS for Top‑N (with OBI)
# ------------------------------
class BinanceDepthWS:
    def __init__(self, enricher: BinanceEnricher):
        self.enricher = enricher
        self.current_syms: List[str] = []
        self.task: Optional[asyncio.Task] = None
        self.books: Dict[str, dict] = {}  # symbol -> {"bids": dict{p:q}, "asks": dict{p:q}, "u": lastUpdateId}
        self.lock = asyncio.Lock()

    async def _build_url(self, syms: List[str]) -> str:
        streams = "/".join([f"{s.lower()}@depth@100ms" for s in syms])
        return f"wss://fstream.binance.com/stream?streams={streams}"

    async def _apply_delta(self, sym: str, bids: List[Tuple[float,float]], asks: List[Tuple[float,float]]):
        book = self.books.get(sym); 
        if not book: return
        b = book["bids"]; a = book["asks"]
        for p, q in bids:
            if q == 0.0: b.pop(p, None)
            else: b[p] = q
        for p, q in asks:
            if q == 0.0: a.pop(p, None)
            else: a[p] = q

    def _book_best(self, sym: str) -> Optional[Tuple[float,float]]:
        book = self.books.get(sym); 
        if not book or not book["bids"] or not book["asks"]: return None
        best_bid = max(book["bids"].keys()); best_ask = min(book["asks"].keys())
        return best_bid, best_ask

    async def _ensure_snapshot(self, sym: str):
        bids, asks, last_id = await self.enricher.get_depth_snapshot(sym)
        if not bids or not asks or last_id is None: return
        self.books[sym] = {"bids": {p:q for p,q in bids}, "asks": {p:q for p,q in asks}, "u": last_id}

    async def orderbook_imbalance(self, sym: str, levels: int = 10) -> Optional[float]:
        async with self.lock:
            book = self.books.get(sym); 
            if not book: return None
            bids = sorted([(p,q) for p,q in book["bids"].items()], key=lambda x:-x[0])[:levels]
            asks = sorted([(p,q) for p,q in book["asks"].items()], key=lambda x:x[0])[:levels]
            bv = sum(q for _,q in bids); av = sum(q for _,q in asks)
            den = bv + av
            if den <= 0: return None
            return (bv - av)/den

    async def slippage_ws(self, sym: str, side: str, notional_usdt: float) -> Optional[float]:
        async with self.lock:
            best = self._book_best(sym); 
            if not best: return None
            best_bid, best_ask = best; mid = (best_bid + best_ask)/2.0
            book = self.books.get(sym); 
            if not book: return None
            bids = sorted([(p,q) for p,q in book["bids"].items()], key=lambda x:-x[0])
            asks = sorted([(p,q) for p,q in book["asks"].items()], key=lambda x:x[0])
            remaining = notional_usdt; spent=0.0; filled=0.0
            if side.lower().startswith("b"):
                for p,q in asks:
                    take = min(q, remaining/p); spent += take*p; filled += take; remaining -= take*p
                    if remaining <= 1e-12: break
            else:
                for p,q in bids:
                    take = min(q, remaining/p); spent += take*p; filled += take; remaining -= take*p
                    if remaining <= 1e-12: break
            if filled <= 0: return None
            vwap = spent/filled
            return ((vwap - mid)/mid)*10_000.0 if side.lower().startswith("b") else ((mid - vwap)/mid)*10_000.0

    async def _run(self, syms: List[str]):
        for s in syms: await self._ensure_snapshot(s)
        url = await self._build_url(syms); name = "BinanceDepthTopN"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    slog("ws_connected", venue=name, streams=len(syms))
                    async for raw in ws:
                        msg = json.loads(raw); data = msg.get("data", {})
                        if not data or "s" not in data or "b" not in data or "a" not in data: continue
                        sym = data["s"]; U=data.get("U"); u=data.get("u"); pu=data.get("pu")
                        book = self.books.get(sym); 
                        if not book: continue
                        last = book.get("u"); 
                        if last is None: continue
                        ok=False
                        if pu is not None and last == pu: ok=True
                        elif U is not None and u is not None and (U <= last + 1 <= u): ok=True
                        if not ok:
                            slog("depth_resync", symbol=sym, last=last, U=U, u=u, pu=pu); await self._ensure_snapshot(sym); continue
                        bids = [(float(p), float(q)) for p,q in data["b"]]; asks = [(float(p), float(q)) for p,q in data["a"]]
                        await self._apply_delta(sym, bids, asks); book["u"]=u
            except Exception as e:
                backoff = 2.0 + random.random()*2.0; slog("ws_error_reconnect", venue=name, error=str(e), backoff=backoff); await asyncio.sleep(backoff)

    async def update_watchlist(self, syms: List[str]):
        syms = sorted(set(syms))
        if syms == self.current_syms: return
        if self.task and not self.task.done():
            self.task.cancel(); 
            try: await self.task
            except Exception: pass
        self.current_syms = syms
        self.task = asyncio.create_task(self._run(syms))

# ------------------------------
# Calibration & Scoring
# ------------------------------
def prob_from_z(z: float, direction_long: bool) -> float:
    # logistic first if provided
    if LOGREG and "w0" in LOGREG and "w1" in LOGREG:
        try:
            p = 1.0/(1.0+math.exp(-(LOGREG["w0"] + LOGREG["w1"]*z)))
            # isotonic post-calibration if provided
            if ISO_LUT and "z" in ISO_LUT and "p" in ISO_LUT and len(ISO_LUT["z"])==len(ISO_LUT["p"])>=2:
                p = float(np.interp(z, ISO_LUT["z"], ISO_LUT["p"]))
            return float(np.clip(p, 0.01, 0.99))
        except Exception:
            pass
    # or pure isotonic
    if ISO_LUT and "z" in ISO_LUT and "p" in ISO_LUT and len(ISO_LUT["z"])==len(ISO_LUT["p"])>=2:
        p = float(np.interp(z, ISO_LUT["z"], ISO_LUT["p"]))
        return float(np.clip(p, 0.01, 0.99))
    # fallback
    base = 0.53 if direction_long else 0.51
    raw = math.tanh(z/2.0)
    return float(np.clip(base + 0.12*raw, 0.05, 0.95))

def rolling_z(v: float, buf: deque, maxlen=200) -> float:
    buf.append(v)
    arr = np.array(buf, dtype=float)
    mu = arr.mean() if len(arr)>=10 else 0.0
    sd = arr.std() if len(arr)>=10 else 1.0
    return (v - mu)/(sd if sd>1e-9 else 1.0)

# Weights for composite score (can be tweaked in future)
W = {"z_cvd":0.5, "z_oi":0.3, "obi":0.2}

# ------------------------------
# Analysis Engine
# ------------------------------
class AnalysisEngine:
    def __init__(self):
        self._z_cvd_buf = defaultdict(lambda: deque(maxlen=200))
        self._z_oi_buf  = defaultdict(lambda: deque(maxlen=200))
        self._z_obi_buf = defaultdict(lambda: deque(maxlen=200))

    def features_from_md(self, md: MarketData):
        if len(md.trades) < 50: return None
        prices = [t["price"] for t in md.trades]; qtys = [t["qty"] for t in md.trades]
        df = pd.DataFrame({"p": prices, "q": qtys})

        ema9 = df["p"].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21= df["p"].ewm(span=21, adjust=False).mean().iloc[-1]

        x = np.arange(len(qtys), dtype=float)
        y = np.cumsum([t["qty"] if t["side"].startswith("b") else -t["qty"] for t in md.trades])
        slope = float(np.polyfit(x, y, 1)[0])

        md.slope_hist.append(slope)
        mu = float(np.mean(md.slope_hist)) if len(md.slope_hist) >= 10 else 0.0
        sd = float(np.std(md.slope_hist))  if len(md.slope_hist) >= 10 else 1.0
        z_slope = (slope - mu) / (sd if sd > 1e-9 else 1.0)

        now = time.time()
        usd_per_hour = md.usd_per_hour(now); tps = md.trades_per_sec(now); med_trade = md.median_trade_size()

        # MTF CVD deltas
        cvd_1m = md.cvd_delta(60) or 0.0
        cvd_5m = md.cvd_delta(300) or 0.0
        cvd_15m= md.cvd_delta(900) or 0.0

        return {
            "symbol": md.symbol,
            "ema9": float(ema9), "ema21": float(ema21),
            "cvd": float(md.cvd), "cvd_slope": slope, "z_cvd_slope": float(z_slope),
            "usd_per_hour": float(usd_per_hour), "tps": float(tps), "median_trade_usd": float(med_trade),
            "price": float(df["p"].iloc[-1]),
            "cvd_1m": float(cvd_1m), "cvd_5m": float(cvd_5m), "cvd_15m": float(cvd_15m),
            "atr": md.bars.atr(),
        }

    def plan_trade(self, f, funding=None, oi_point=None, oi_delta_mtf=None, basis_bps=None, slip_bps=None, obi=None, venue_name=""):
        entry = f["price"]
        # Direction
        long_bias = (f["ema9"] >= f["ema21"]) and (f["z_cvd_slope"] > 0)
        direction = "LONG" if long_bias else "SHORT"

        # SL/TP (ATR mode optional)
        if ARGS.atr-risk and f.get("atr"):
            atr = max(1e-9, f["atr"])
            sl  = entry - ARGS.sl_atr*atr if direction=="LONG" else entry + ARGS.sl_atr*atr
            tp_mults = [float(x) for x in ARGS.tp_atr.split(",")] if ARGS.tp_atr else [0.8,1.5,2.3]
            tp1 = entry + (tp_mults[0]*atr if direction=="LONG" else -tp_mults[0]*atr)
            tp2 = entry + (tp_mults[1]*atr if direction=="LONG" else -tp_mults[1]*atr)
            tp3 = entry + (tp_mults[2]*atr if direction=="LONG" else -tp_mults[2]*atr)
        else:
            sl  = entry * (1-Config.SL_PCT) if direction=="LONG" else entry * (1+Config.SL_PCT)
            tp1 = entry * (1+Config.TP1_PCT) if direction=="LONG" else entry * (1-Config.TP1_PCT)
            tp2 = entry * (1+Config.TP2_PCT) if direction=="LONG" else entry * (1-Config.TP2_PCT)
            tp3 = entry * (1+Config.TP3_PCT) if direction=="LONG" else entry * (1-Config.TP3_PCT)

        # Calibrated probability
        wp = prob_from_z(f["z_cvd_slope"], direction_long=(direction=="LONG"))
        expect = float((wp*1.5) - ((1-wp)*1.0))

        # RR
        if direction=="LONG":
            rr1=(tp1-entry)/max(entry-sl,1e-12); rr2=(tp2-entry)/max(entry-sl,1e-12); rr3=(tp3-entry)/max(entry-sl,1e-12)
        else:
            rr1=(entry-tp1)/max(sl-entry,1e-12); rr2=(entry-tp2)/max(sl-entry,1e-12); rr3=(entry-tp3)/max(sl-entry,1e-12)

        # Funding context
        funding_rate = funding.get("fundingRate") if funding else None
        next_funding = funding.get("nextFundingTime") if funding else None
        funding_soon = None
        if next_funding:
            mins = max(0,(next_funding/1000.0 - time.time())/60.0); funding_soon = mins <= 60
        mark_price = funding.get("markPrice") if funding else None
        basis = None
        if mark_price and f.get("price"):
            mid = f["price"]  # approximated by last trade; WS book mid may be used in orchestrator
            basis = ((mark_price - mid)/mid)*10_000.0

        # Composite (normalized features): z(CVD slope), z(ΔOI%), z(OBI)
        z_cvd = rolling_z(f["z_cvd_slope"], self._z_cvd_buf[f["symbol"]])
        z_oi  = rolling_z(oi_delta_mtf or 0.0,       self._z_oi_buf[f["symbol"]])
        z_obi = rolling_z(obi or 0.0,                self._z_obi_buf[f["symbol"]])
        composite = W["z_cvd"]*z_cvd + W["z_oi"]*z_oi + W["obi"]*z_obi

        return {
            "venue": venue_name, "symbol": f["symbol"], "direction": direction,
            "win_probability": wp, "expectancy": expect, "composite": float(composite),
            "entry": float(entry), "sl": float(sl), "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3),
            "r_tp1": float(rr1), "r_tp2": float(rr2), "r_tp3": float(rr3),
            "usd_per_hour": float(f["usd_per_hour"]), "tps": f["tps"], "median_trade_usd": f["median_trade_usd"],
            "funding_rate": funding_rate, "funding_in_60m": funding_soon,
            "oi_point": oi_point, "oi_delta_pct_5m": oi_delta_mtf, "basis_bps": basis,
            "slip_bps": slip_bps or {}, "obi": obi,
            "atr": f.get("atr"),
        }

# ------------------------------
# Macro regime (BTCUSDT 1m klines → 60m)
# ------------------------------
async def macro_risk_gate(http: aiohttp.ClientSession, sem: asyncio.Semaphore) -> bool:
    if not ARGS.macro: return True
    url = "https://api.binance.com/api/v3/klines"; params = {"symbol":"BTCUSDT","interval":"1m","limit":120}
    async with sem: data = await http_get_json_cached(http, url, params=params, ttl=10)
    if not isinstance(data, list) or len(data)<60: return True
    closes = [float(x[4]) for x in data[-60:]]
    ret = np.diff(np.log(np.array(closes)))
    dd = (max(closes[-60:]) - closes[-1]) / max(1e-9, max(closes[-60:]))
    vol = ret.std()*math.sqrt(60)
    # veto if drawdown > 3% in last hour or vol spike
    return not (dd > 0.03 or vol > 0.05)

# ------------------------------
# Coverage Guard (24h movers) → triggers shard reload
# ------------------------------
async def coverage_guard(http: aiohttp.ClientSession, sem: asyncio.Semaphore, connector: BinanceFuturesConnector):
    base = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    while True:
        try:
            await asyncio.sleep(300)
            async with sem: data = await http_get_json_cached(http, base, ttl=30)
            outliers = [d["symbol"] for d in data if abs(float(d.get("priceChangePercent",0))) >= 10.0]
            if not outliers: continue
            new_syms = sorted(set(connector.symbols) | set(outliers))
            if len(new_syms) != len(connector.symbols):
                slog("coverage_guard_reload", added=list(set(new_syms)-set(connector.symbols))[:10])
                await connector.reload(new_syms)
        except Exception as e:
            slog("coverage_guard_err", error=str(e))

# ------------------------------
# Top‑N Manager (diversity + freeze)
# ------------------------------
class TopNManager:
    def __init__(self, n: int):
        self.n = n; self.picks: List[dict] = []; self.last_update = 0.0

    @staticmethod
    def _corr(a_prices: List[float], b_prices: List[float]) -> float:
        if len(a_prices) < 30 or len(b_prices) < 30: return 0.0
        a = np.array(a_prices[-300:], dtype=float); b = np.array(b_prices[-300:], dtype=float)
        if a.std()<1e-9 or b.std()<1e-9: return 0.0
        return float(np.corrcoef(a, b)[0,1])

    def update(self, candidates: List[dict], md_map: Dict[str, MarketData]) -> List[dict]:
        now = time.time()
        if self.picks and (now - self.last_update) < Config.FREEZE_SEC:
            margin = 0.05
            worst = min(self.picks, key=lambda t:(t["win_probability"], t["expectancy"], t["composite"]))
            best_new = max(candidates, key=lambda t:(t["win_probability"], t["expectancy"], t["composite"]), default=None)
            if best_new and (best_new["win_probability"] - worst["win_probability"]) >= margin:
                self.picks = [t for t in self.picks if t is not worst] + [best_new]
                self.picks = sorted(self.picks, key=lambda t:(t["win_probability"], t["expectancy"], t["composite"]), reverse=True)[:self.n]
                self.last_update = now
            return self.picks
        cand_sorted = sorted(candidates, key=lambda t:(t["win_probability"], t["expectancy"], t["composite"]), reverse=True)
        picks = []
        for t in cand_sorted:
            sym = t["symbol"]; ok=True
            for p in picks:
                md_a = md_map.get(sym); md_b = md_map.get(p["symbol"])
                pa = [tt["price"] for tt in (md_a.trades if md_a else [])]; pb = [tt["price"] for tt in (md_b.trades if md_b else [])]
                if self._corr(pa, pb) >= Config.CORR_MAX: ok=False; break
            if ok: picks.append(t)
            if len(picks) >= self.n: break
        self.picks = picks; self.last_update = now; return self.picks

# ------------------------------
# Persistence helpers
# ------------------------------
def write_jsonl(path: str, obj: dict):
    if not path: return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def ensure_dir(d: str):
    if d and not os.path.isdir(d): os.makedirs(d, exist_ok=True)

# ------------------------------
# Bot Orchestrator
# ------------------------------
class TradingBot:
    def __init__(self):
        self.http: Optional[aiohttp.ClientSession] = None
        self.sem = asyncio.Semaphore(ConfigData.REST_CONCURRENCY)
        self.universe: Optional[Universe] = None
        self.connectors = {}
        self.enrich_binance: Optional[BinanceEnricher] = None
        self.depth_topn: Optional[BinanceDepthWS] = None
        self.topn = TopNManager(Config.TOP_N)
        self.tasks: List[asyncio.Task] = []
        self.running = True

    async def start(self):
        self.http = aiohttp.ClientSession()
        asyncio.create_task(cache_janitor())

        # Universe
        self.universe = Universe(self.http, self.sem); await self.universe.refresh()

        # Connectors
        if self.universe.binance_fut:
            self.connectors["binance"] = BinanceFuturesConnector(self.universe.binance_fut); await self.connectors["binance"].start()
        if self.universe.bybit_linear:
            self.connectors["bybit"] = BybitLinearConnector(self.universe.bybit_linear); self.tasks.append(asyncio.create_task(self.connectors["bybit"].start()))
        if self.universe.bitget_usdt_fut:
            self.connectors["bitget"] = BitgetUSDTFutConnector(self.universe.bitget_usdt_fut); self.tasks.append(asyncio.create_task(self.connectors["bitget"].start()))

        # Enrichers
        self.enrich_binance = BinanceEnricher(self.http, self.sem)
        if ConfigEnrichers.ENABLE_FUNDING and ConfigEnrichers.ENABLE_BINANCE_MARKPRICE_WS:
            self.tasks.append(asyncio.create_task(self.enrich_binance.start_markprice_ws()))
        if ARGS.liq_bus:
            self.tasks.append(asyncio.create_task(self.enrich_binance.start_liq_bus()))

        # Depth WS for Top‑N
        self.depth_topn = BinanceDepthWS(self.enrich_binance)

        # Universe & coverage tasks
        self.tasks.append(asyncio.create_task(self._universe_refresher()))
        if "binance" in self.connectors:
            self.tasks.append(asyncio.create_task(coverage_guard(self.http, self.sem, self.connectors["binance"])))

        # Metrics server (optional)
        if ARGS.metrics:
            self.tasks.append(asyncio.create_task(self._metrics_server()))

        # Health reporter
        self.tasks.append(asyncio.create_task(self._health_reporter()))

    async def _metrics_server(self):
        from aiohttp import web
        async def handle_metrics(request):
            data = {"uptime_sec": time.time() - START_TS,
                    "binance_symbols": len(self.universe.binance_fut) if self.universe else 0,
                    "bybit_symbols": len(self.universe.bybit_linear) if self.universe else 0,
                    "bitget_symbols": len(self.universe.bitget_usdt_fut) if self.universe else 0}
            return web.json_response(data)
        app = web.Application(); app.router.add_get("/metrics", handle_metrics)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 8989); await site.start()

    async def _health_reporter(self):
        while True:
            await asyncio.sleep(300)
            rss = self._rss_mb()
            slog("health", tasks=len(self.tasks), rss_mb=rss, picks=len(self.topn.picks))

    def _rss_mb(self) -> float:
        if not psutil: return -1.0
        try: return float(psutil.Process(os.getpid()).memory_info().rss / (1024*1024))
        except Exception: return -1.0

    async def _universe_refresher(self):
        while True:
            await asyncio.sleep(Config.UNIVERSE_REFRESH_SEC)
            if not self.universe: continue
            before = set(self.universe.binance_fut)
            await self.universe.refresh()
            after = set(self.universe.binance_fut)
            added = sorted(list(after - before)); removed = sorted(list(before - after))
            if added or removed:
                slog("universe_binance_diff", added=len(added), removed=len(removed))
                if "binance" in self.connectors:
                    new_syms = sorted(list(after))
                    await self.connectors["binance"].reload(new_syms)

    def _iter_venues(self):
        for name, conn in self.connectors.items(): yield name, conn

    def gather_features(self):
        feats = []; md_map: Dict[str, MarketData] = {}
        for venue, conn in self._iter_venues():
            md_map.update(getattr(conn, "data", {}))
            for s, md in getattr(conn, "data", {}).items():
                f = analysis.features_from_md(md)
                if f: feats.append((venue, f))
        return feats, md_map

    def _apply_risk_gates(self, plan):
        if plan["usd_per_hour"] < Config.MIN_USD_PER_HOUR: return False
        if max(plan["r_tp1"], plan["r_tp2"]) < Config.MIN_RR_TP1: return False
        if plan["win_probability"] < Config.MIN_WIN_PROB: return False
        # approximate VaR gate (optional): deny extremely wide SL relative to entry if budget set
        if ARGS.var_budget > 0 and plan["atr"]:
            risk_per_unit = abs(plan["entry"] - plan["sl"])
            # naive size that would match budget at 1x exposure
            if risk_per_unit > 0 and (risk_per_unit * 1.0) > ARGS.var_budget:
                return False
        return True

    async def analysis_loop(self):
        jsonl_dir = ARGS.save_jsonl
        if jsonl_dir: ensure_dir(jsonl_dir)
        while self.running:
            macro_ok = await macro_risk_gate(self.http, self.sem)
            if not macro_ok:
                slog("macro_veto", msg="Risk-off: skipping cycle"); await asyncio.sleep(Config.ANALYSIS_EVERY_SEC); continue

            await asyncio.sleep(Config.ANALYSIS_EVERY_SEC)

            feats, md_map = self.gather_features()

            candidates = []
            for venue, f in feats:
                funding = oi_point = None; oi_delta5 = None; basis_bps = None; slip_bps={}; obi=None
                if venue == "binance":
                    funding = self.enrich_binance.funding_live.get(f["symbol"], None) if self.enrich_binance else None
                    if self.enrich_binance:
                        oi_point = await self.enrich_binance.get_oi_point(f["symbol"])
                        oi_delta5 = self.enrich_binance.oi_delta_pct(f["symbol"], 300)
                    # OBI & slippage (WS-first)
                    if self.depth_topn:
                        obi = await self.depth_topn.orderbook_imbalance(f["symbol"], levels=10)
                        side = "buy" if (f["ema9"]>=f["ema21"] and f["z_cvd_slope"]>0) else "sell"
                        for nb in SLIP_BUCKETS:
                            val = await self.depth_topn.slippage_ws(f["symbol"], side, float(nb))
                            if val is None:
                                bids, asks, _ = await self.enrich_binance.get_depth_snapshot(f["symbol"])
                                if bids and asks:
                                    best_bid, best_ask = bids[0][0], asks[0][0]; mid=(best_bid+best_ask)/2.0
                                    remaining=float(nb); spent=0.0; filled=0.0; book=asks if side=="buy" else bids
                                    for p,q in book:
                                        take=min(q, remaining/p); spent+=take*p; filled+=take; remaining-=take*p
                                        if remaining<=1e-9: break
                                    if filled>0:
                                        vwap=spent/filled
                                        slip_bps[str(nb)] = ((vwap-mid)/mid)*10_000.0 if side=="buy" else ((mid-vwap)/mid)*10_000.0
                            else: slip_bps[str(nb)] = val

                plan = analysis.plan_trade(f, funding=funding, oi_point=oi_point, oi_delta_mtf=oi_delta5, basis_bps=basis_bps, slip_bps=slip_bps, obi=obi, venue_name=venue)
                if self._apply_risk_gates(plan): candidates.append(plan)

            picks = self.topn.update(candidates, md_map)

            # Update Top‑N depth watchlist
            if self.depth_topn:
                await self.depth_topn.update_watchlist([p["symbol"] for p in picks if p["venue"]=="binance"])

            # JSONL persist
            ts = time.time()
            if ARGS.save_jsonl:
                for venue, f in feats:
                    write_jsonl(os.path.join(ARGS.save_jsonl, "features.jsonl"), {"ts": ts, "venue": venue, **f})
                for p in picks:
                    write_jsonl(os.path.join(ARGS.save_jsonl, "picks.jsonl"), {"ts": ts, **p})

            # Print
            self.print_topn(picks)

    def print_topn(self, trades: List[dict]):
        if not trades:
            logger.info("No qualifying trades yet."); return
        headers = ["#", "Ex","Symbol","Dir","Win%","Exp","Comp","R1/R2","USD/h","TPS","Med$","Slip 1k/5k/10k","OBI","Fund","F<60m","OI","ΔOI%5m","ATR","Entry","SL","TP1","TP2","TP3"]
        rows=[]
        for i,t in enumerate(trades,1):
            slip_str="/".join(f"{t['slip_bps'].get(str(b), '-') if t.get('slip_bps') else '-'}" for b in SLIP_BUCKETS)
            rows.append([i,t["venue"][:2].upper(),t["symbol"],"L" if t["direction"]=="LONG" else "S",
                         f"{t['win_probability']*100:.1f}%", f"{t['expectancy']:.2f}", f"{t['composite']:.2f}",
                         f"{t['r_tp1']:.2f}/{t['r_tp2']:.2f}",
                         f"{t['usd_per_hour']/1_000:.1f}k", f"{t['tps']:.2f}", f"{t['median_trade_usd']:.0f}",
                         slip_str,
                         f"{t['obi']:.2f}" if t.get("obi") is not None else "-",
                         f"{t['funding_rate']:.5f}" if t.get("funding_rate") is not None else "-",
                         "Y" if t.get("funding_in_60m") else "-",
                         f"{t['oi_point']:.3f}" if t.get("oi_point") is not None else "-",
                         f"{t['oi_delta_pct_5m']:.2f}%" if t.get("oi_delta_pct_5m") is not None else "-",
                         f"{t['atr']:.6f}" if t.get("atr") is not None else "-",
                         f"{t['entry']:.6f}", f"{t['sl']:.6f}", f"{t['tp1']:.6f}", f"{t['tp2']:.6f}", f"{t['tp3']:.6f}"])
        print("\n" + "="*120)
        print(f" TOP-{Config.TOP_N} TRADES — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        print("="*120 + "\n")
        print(tabulate(rows, headers=headers, tablefmt="grid"))
        print("\n" + "="*42 + " END OF SCAN " + "="*42 + "\n")

    async def stop(self):
        self.running=False
        for t in self.tasks:
            try: t.cancel(); await t
            except Exception: pass
        if self.http: await self.http.close()

# ------------------------------
# Entrypoint
# ------------------------------
START_TS = time.time()
analysis = AnalysisEngine()

async def main():
    bot = TradingBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(bot.stop()))
        except NotImplementedError: pass
    await bot.start()
    await bot.analysis_loop()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
