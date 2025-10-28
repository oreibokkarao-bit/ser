from __future__ import annotations
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import random
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

try:
    import httpx
    import numpy as np
    import orjson
    import pandas as pd
except Exception as e:
    print("[SETUP] Please install required packages first:")
    print("pip install -U httpx pandas numpy orjson")
    raise

APP_ROOT = Path(os.getcwd())
CACHE_DIR = APP_ROOT / ".cache"
LOGS_DIR = APP_ROOT / "logs"
OUT_DIR = APP_ROOT / "out"
for d in (CACHE_DIR, LOGS_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

ACQ_LOG = LOGS_DIR / "acquisition.jsonl"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# ---------- Token Bucket (thread-safe per host) ----------
@dataclass
class TokenBucket:
    rate_per_sec: float
    capacity: int
    tokens: float = 0.0
    last_ts: float = dataclasses.field(default_factory=time.time)
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)

    def take(self, amount: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.time()
                elapsed = now - self.last_ts
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
                self.last_ts = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                need = amount - self.tokens
                sleep_for = max(need / self.rate_per_sec, 0.01)
            time.sleep(min(sleep_for, 1.0))

PER_HOST_BUCKETS = {
    "fapi.binance.com": TokenBucket(rate_per_sec=9.0, capacity=45),  # conservative
    "api.binance.com":  TokenBucket(rate_per_sec=9.0, capacity=45),
    "api.bybit.com":    TokenBucket(rate_per_sec=6.0, capacity=30),
}

def _host_from_url(url: str) -> str:
    return url.split("//",1)[-1].split("/",1)[0]

def _cache_key(url: str, params: Optional[Dict[str, Any]]) -> str:
    src = url + "?" + (json.dumps(params, sort_keys=True) if params else "")
    return hashlib.sha256(src.encode()).hexdigest()

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"

def _log(entry: Dict[str, Any]) -> None:
    entry["t_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    ACQ_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ACQ_LOG, "a", encoding="utf-8") as f:
        f.write(orjson.dumps(entry).decode() + "\n")

def http_get_json(url: str, params: Optional[Dict[str, Any]] = None, ttl: int = 30, max_retries: int = 3) -> Any:
    host = _host_from_url(url)
    bucket = PER_HOST_BUCKETS.get(host, TokenBucket(rate_per_sec=5.0, capacity=20))
    key = _cache_key(url, params)
    path = _cache_path(key)
    now = time.time()

    if path.exists():
        age = now - path.stat().st_mtime
        if age <= ttl:
            try:
                data = orjson.loads(path.read_bytes())
                _log({"event":"cache_hit","host":host,"url":url,"params":params,"age":round(age,1)})
                return data
            except Exception:
                pass

    backoff = 1.0
    for attempt in range(1, max_retries+1):
        bucket.take(1.0)
        t0 = time.time()
        status = None
        try:
            with httpx.Client(http2=True, timeout=25.0, headers={"User-Agent":"zyncas-multi/1.1"}) as c:
                r = c.get(url, params=params)
                status = r.status_code
                if status in (403, 451):
                    _log({"event":"geoblock","host":host,"url":url,"params":params,"status":status})
                    raise RuntimeError(f"Geo/Legal block {status} {url}")
                r.raise_for_status()
                data = r.json()
                path.write_bytes(orjson.dumps(data))
                _log({"event":"http_ok","host":host,"url":url,"status":status,"ms":int((time.time()-t0)*1000)})
                return data
        except Exception as e:
            _log({"event":"http_err","host":host,"url":url,"params":params,"status":status,"attempt":attempt,"err":str(e)})
            if attempt >= max_retries:
                raise
            sleep_s = backoff + random.random()*0.5*backoff
            time.sleep(min(sleep_s, 5.0))
            backoff *= 2.0

# ---------- Venue adapters ----------
BINANCE_FAPI = "https://fapi.binance.com"
class BinanceUSDTMFutures:
    name = "binance"
    host = "fapi.binance.com"

    @staticmethod
    def discover_symbols() -> List[str]:
        url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
        j = http_get_json(url, ttl=3600)
        out = []
        for s in j.get("symbols", []):
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                out.append(s["symbol"])
        return sorted(out)

    @staticmethod
    def klines_1m(symbol: str, limit: int = 400) -> pd.DataFrame:
        url = f"{BINANCE_FAPI}/fapi/v1/klines"
        params = {"symbol":symbol, "interval":"1m", "limit":limit}
        data = http_get_json(url, params=params, ttl=8)
        cols = ["open_time","open","high","low","close","volume","close_time","qv","trades","tbb","tbq","ignore"]
        df = pd.DataFrame(data, columns=cols)
        for c in ("open","high","low","close","volume","tbb"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df = df.rename(columns={"tbb":"taker_buy_base"})
        return df[["open_time","close_time","open","high","low","close","volume","taker_buy_base"]]

    @staticmethod
    def oi_5m(symbol: str, limit: int = 150) -> pd.DataFrame:
        url = f"{BINANCE_FAPI}/futures/data/openInterestHist"
        params = {"symbol":symbol, "period":"5m", "limit":limit}
        data = http_get_json(url, params=params, ttl=60)
        df = pd.DataFrame(data)
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["oi"] = pd.to_numeric(df["sumOpenInterest"], errors="coerce")
        return df[["timestamp","oi"]]

    @staticmethod
    def funding_hist(symbol: str, limit: int = 500) -> pd.DataFrame:
        url = f"{BINANCE_FAPI}/fapi/v1/fundingRate"
        params = {"symbol":symbol, "limit":limit}
        data = http_get_json(url, params=params, ttl=60)
        df = pd.DataFrame(data)
        if df.empty:
            return df
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
        df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
        return df[["fundingTime","fundingRate"]]

BYBIT_API = "https://api.bybit.com"
class BybitLinearUSDT:
    name = "bybit"
    host = "api.bybit.com"

    @staticmethod
    def discover_symbols() -> List[str]:
        url = f"{BYBIT_API}/v5/market/instruments-info"
        params = {"category":"linear","limit": "1000"}
        j = http_get_json(url, params=params, ttl=3600)
        rows = j.get("result", {}).get("list", []) or j.get("result", {}).get("category", [])
        out = []
        for r in rows:
            sym = r.get("symbol")
            qc = r.get("quoteCoin") or r.get("quoteCurrency")
            st = r.get("status") or (r.get("lotSizeFilter", {}) or {}).get("status")
            if sym and qc == "USDT" and (st == "Trading" or st == "trading"):
                out.append(sym)
        return sorted(list(set(out)))

    @staticmethod
    def klines_1m(symbol: str, limit: int = 400) -> pd.DataFrame:
        url = f"{BYBIT_API}/v5/market/kline"
        params = {"category":"linear","symbol":symbol,"interval":"1","limit":str(limit)}
        j = http_get_json(url, params=params, ttl=8)
        result = j.get("result", {})
        lst = result.get("list", []) or []
        if not lst:
            return pd.DataFrame(columns=["open_time","close_time","open","high","low","close","volume","taker_buy_base"])
        recs = []
        for it in lst:
            ts = int(it[0])
            open_, high, low, close, vol = map(float, (it[1], it[2], it[3], it[4], it[5]))
            open_t = pd.to_datetime(ts, unit="ms", utc=True)
            close_t = open_t + pd.Timedelta(minutes=1)
            recs.append((open_t, close_t, open_, high, low, close, vol, math.nan))
        df = pd.DataFrame(recs, columns=["open_time","close_time","open","high","low","close","volume","taker_buy_base"])
        return df

    @staticmethod
    def oi_5m(symbol: str, limit: int = 150) -> pd.DataFrame:
        url = f"{BYBIT_API}/v5/market/open-interest"
        params = {"category":"linear","symbol":symbol,"interval":"5min","limit":str(limit)}
        j = http_get_json(url, params=params, ttl=60)
        lst = j.get("result", {}).get("list", [])
        if not lst:
            return pd.DataFrame(columns=["timestamp","oi"])
        recs = []
        for it in lst:
            ts = int(it["timestamp"]) if isinstance(it, dict) else int(it[0])
            oi = float(it["openInterest"]) if isinstance(it, dict) else float(it[1])
            recs.append((pd.to_datetime(ts, unit="ms", utc=True), oi))
        return pd.DataFrame(recs, columns=["timestamp","oi"])

    @staticmethod
    def funding_hist(symbol: str, limit: int = 150) -> pd.DataFrame:
        url = f"{BYBIT_API}/v5/market/funding/history"
        params = {"category":"linear","symbol":symbol,"limit":str(limit)}
        j = http_get_json(url, params=params, ttl=60)
        lst = j.get("result", {}).get("list", [])
        if not lst:
            return pd.DataFrame(columns=["fundingTime","fundingRate"])
        recs = []
        for it in lst:
            ts = int(it["fundingRateTimestamp"]) if isinstance(it, dict) else int(it[0])
            fr = float(it["fundingRate"]) if isinstance(it, dict) else float(it[1])
            recs.append((pd.to_datetime(ts, unit="ms", utc=True), fr))
        return pd.DataFrame(recs, columns=["fundingTime","fundingRate"])

# ---------- Feature engineering ----------
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()

def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=series.index).ewm(alpha=1/length, adjust=False).mean()
    roll_down = pd.Series(down, index=series.index).ewm(alpha=1/length, adjust=False).mean()
    rs = roll_up / (roll_down.replace(0, np.nan))
    return 100.0 - (100.0 / (1.0 + rs))

def bollinger(series: pd.Series, length: int = 20, num_sd: float = 2.0):
    ma = series.rolling(length).mean()
    sd = series.rolling(length).std(ddof=0)
    upper = ma + num_sd * sd
    lower = ma - num_sd * sd
    return ma, upper, lower

@dataclass
class SignalConfig:
    cvd_slope_z_min: float = 1.5
    bb_expansion_min: float = 0.10
    ema_align_required: bool = True
    vol_surge_min: float = 1.3
    taker_buy_ratio_min: float = 0.55
    oi_delta_rate_min: float = 0.00
    require_funding_flip: bool = False
    score_threshold: float = 4.0
    fast_mode: bool = False

def compute_features(df: pd.DataFrame, oi: Optional[pd.DataFrame], fr: Optional[pd.DataFrame]) -> pd.DataFrame:
    k = df.copy()
    if k.empty:
        return k
    k = k.set_index("close_time")
    price = k["close"].astype(float)
    vol = k["volume"].astype(float)
    if "taker_buy_base" in k.columns and k["taker_buy_base"].notna().sum() > 0:
        taker_buy = k["taker_buy_base"].astype(float).fillna(np.nan)
        taker_sell = (vol - taker_buy).clip(lower=0)
        cvd = (taker_buy - taker_sell).cumsum()
        cvd_diff = cvd.diff()
        cvd_slope = (cvd_diff - cvd_diff.rolling(50).mean()) / (cvd_diff.rolling(50).std(ddof=0).replace(0, np.nan))
        taker_buy_ratio = taker_buy / (taker_buy + taker_sell).replace(0, np.nan)
    else:
        cvd = pd.Series(np.nan, index=price.index)
        cvd_slope = pd.Series(np.nan, index=price.index)
        taker_buy_ratio = pd.Series(np.nan, index=price.index)

    ema9 = ema(price, 9)
    ema21 = ema(price, 21)
    ma, bb_u, bb_l = bollinger(price, 20, 2.0)
    bb_width = (bb_u - bb_l) / (ma.replace(0, np.nan))
    bb_expansion = bb_width.pct_change(3, fill_method=None)
    vol_sma20 = vol.rolling(20).mean()
    vol_surge = (vol / vol_sma20.replace(0, np.nan))
    r = rsi(price, 14)

    feats = pd.DataFrame({
        "close": price,
        "ema9": ema9,
        "ema21": ema21,
        "ema_align": (ema9 > ema21).astype(int),
        "rsi14": r,
        "bb_expansion3": bb_expansion,
        "vol_surge": vol_surge,
        "taker_buy_ratio": taker_buy_ratio,
        "cvd": cvd,
        "cvd_slope_z": cvd_slope,
    })

    if oi is not None and not oi.empty:
        oi2 = oi.copy().set_index("timestamp").sort_index()
        feats = feats.join(oi2["oi"].rename("oi"), how="left")
        oi_delta = feats["oi"].pct_change(fill_method=None)
        feats["oi_delta_rate"] = oi_delta.replace([np.inf, -np.inf], np.nan)
    else:
        feats["oi"] = np.nan
        feats["oi_delta_rate"] = np.nan

    if fr is not None and not fr.empty:
        fr2 = fr.copy().set_index("fundingTime").sort_index().reindex(feats.index, method="ffill")
        feats["funding"] = fr2["fundingRate"]
        f_prev = feats["funding"].shift(1)
        feats["funding_flip_up"] = ((f_prev < 0) & (feats["funding"] > 0)).astype(int)
    else:
        feats["funding"] = np.nan
        feats["funding_flip_up"] = 0

    feats = feats.dropna(thresh=5)
    return feats

def score_row(row: pd.Series, cfg: SignalConfig) -> float:
    score = 0.0
    if pd.notna(row.get("cvd_slope_z")) and row["cvd_slope_z"] >= cfg.cvd_slope_z_min:
        score += 1.0
    if pd.notna(row.get("bb_expansion3")) and row["bb_expansion3"] >= cfg.bb_expansion_min:
        score += 1.0
    if (row.get("ema9", 0) > row.get("ema21", 0)) if cfg.ema_align_required else True:
        score += 1.0
    if pd.notna(row.get("vol_surge")) and row["vol_surge"] >= cfg.vol_surge_min:
        score += 1.0
    if pd.notna(row.get("taker_buy_ratio")) and row["taker_buy_ratio"] >= cfg.taker_buy_ratio_min:
        score += 1.0
    if pd.notna(row.get("oi_delta_rate")) and row["oi_delta_rate"] >= cfg.oi_delta_rate_min:
        score += 0.5
    if cfg.require_funding_flip and row.get("funding_flip_up", 0) == 1:
        score += 0.5
    return score

def zyncas_ladder(entry: float) -> Dict[str, float]:
    return {
        "sl": round(entry*(1-0.10), 10),
        "tp1": round(entry*(1+0.12), 10),
        "tp2": round(entry*(1+0.22), 10),
        "tp3": round(entry*(1+0.31), 10),
    }

def analyze_symbol(venue: str, symbol: str, cfg: SignalConfig, k_limit: int = 400) -> pd.DataFrame:
    if venue == "binance":
        kl = BinanceUSDTMFutures.klines_1m(symbol, limit=k_limit)
        oi = None if cfg.fast_mode else BinanceUSDTMFutures.oi_5m(symbol, limit=150)
        fr = None if cfg.fast_mode else BinanceUSDTMFutures.funding_hist(symbol, limit=500)
    elif venue == "bybit":
        kl = BybitLinearUSDT.klines_1m(symbol, limit=k_limit)
        oi = None if cfg.fast_mode else BybitLinearUSDT.oi_5m(symbol, limit=150)
        fr = None if cfg.fast_mode else BybitLinearUSDT.funding_hist(symbol, limit=150)
    else:
        raise ValueError("unknown venue")

    feats = compute_features(kl, oi, fr)
    if feats.empty:
        return pd.DataFrame(columns=["venue","symbol","entry","sl","tp1","tp2","tp3","utc","ist","score"])

    feats["score"] = feats.apply(lambda r: score_row(r, cfg), axis=1)
    cand = feats[feats["score"] >= cfg.score_threshold].copy()
    if cand.empty:
        return pd.DataFrame(columns=["venue","symbol","entry","sl","tp1","tp2","tp3","utc","ist","score"])
    cand["entry"] = cand["close"]
    lad = cand["entry"].apply(zyncas_ladder).apply(pd.Series)
    out = pd.concat([cand[["entry","score"]], lad], axis=1)
    out["venue"] = venue
    out["symbol"] = symbol
    out["utc"] = out.index.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S")
    out["ist"] = out.index.tz_convert(IST).strftime("%Y-%m-%d %H:%M:%S")
    return out[["venue","symbol","entry","sl","tp1","tp2","tp3","utc","ist","score"]]

def print_table(df: pd.DataFrame) -> None:
    if df.empty:
        print("No eligible signals right now.")
        return
    cols = ["venue","symbol","entry","sl","tp1","tp2","tp3","utc","ist","score"]
    print(df[cols].to_string(index=False))

def main(argv: Optional[List[str]] = None) -> None:
    import argparse, time, math
    p = argparse.ArgumentParser(description="Multi-venue Zyncas Decoder — ALL Binance & Bybit USDT perpetuals. Fast/parallel & ToS-safe.")
    p.add_argument("--venues", type=str, default="binance,bybit", help="Comma-list among {binance,bybit}")
    p.add_argument("--symbols", type=str, default="all", help="'all' or comma-list per venue scope")
    p.add_argument("--k_limit", type=int, default=400, help="1m bars per symbol")
    p.add_argument("--score_threshold", type=float, default=4.0)
    p.add_argument("--require_funding_flip", action="store_true")
    p.add_argument("--fast", action="store_true", help="Skip OI & funding for speed")
    p.add_argument("--max_per_venue", type=int, default=0, help="0 = no cap; else cap symbols per venue")
    p.add_argument("--shard_size", type=int, default=60, help="symbols per batch to keep request rate safe")
    p.add_argument("--sleep_between_shards", type=float, default=5.0, help="seconds to sleep between batches")
    p.add_argument("--workers", type=int, default=6, help="threads per shard (rate-limited)")
    p.add_argument("--save_csv", action="store_true")
    args = p.parse_args(argv)

    cfg = SignalConfig(require_funding_flip=args.require_funding_flip,
                       score_threshold=args.score_threshold,
                       fast_mode=args.fast)

    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    all_rows: List[pd.DataFrame] = []

    for v in venues:
        if v == "binance":
            syms = BinanceUSDTMFutures.discover_symbols()
        elif v == "bybit":
            try:
                syms = BybitLinearUSDT.discover_symbols()
            except Exception as e:
                print(f"[WARN] bybit discovery failed: {e}. Skipping Bybit.")
                syms = []
        else:
            print(f"[WARN] unknown venue {v}, skipping.")
            continue

        if args.symbols != "all":
            wanted = set([s.strip().upper() for s in args.symbols.split(",") if s.strip()])
            syms = [s for s in syms if s in wanted]

        if args.max_per_venue and args.max_per_venue > 0:
            syms = syms[:args.max_per_venue]

        total = len(syms)
        print(f"[BOOT] {v}: scanning {total} symbols (fast={cfg.fast_mode})…")

        shard = max(1, args.shard_size)
        for i in range(0, total, shard):
            batch = syms[i:i+shard]
            print(f"  [PROGRESS] {v} batch {i//shard+1}/{math.ceil(total/shard)}: {batch[0]}..{batch[-1]} ({len(batch)} syms)")
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futs = {pool.submit(analyze_symbol, v, sym, cfg, args.k_limit): sym for sym in batch}
                for fut in as_completed(futs):
                    sym = futs[fut]
                    try:
                        out = fut.result()
                        if not out.empty:
                            all_rows.append(out)
                        print(f"    ✓ {v}:{sym} done", flush=True)
                    except Exception as e:
                        print(f"    [WARN] {v}:{sym} -> {e}", flush=True)
            if i + shard < total:
                time.sleep(max(args.sleep_between_shards, 0.0))

    if not all_rows:
        print("No signals.")
        return

    res = pd.concat(all_rows, ignore_index=False).sort_values("utc")
    print_table(res)

    if args.save_csv:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = OUT_DIR / "latest_signals_all.csv"
        res.to_csv(dest, index=False)
        print(f"[SAVED] {dest}")

if __name__ == "__main__":
    main()
