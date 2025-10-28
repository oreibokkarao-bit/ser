#!/usr/bin/env python3
# zyncas_scanner_top5.py
# Full "main script" that scans Binance + Bybit USDT perpetuals and prints ONLY Top-5 trades
# by expectancy (and ties by probability and R:R). Designed for Thonny-friendly Python 3.
#
# No API keys required. Uses public REST endpoints. Concurrency via ThreadPool for speed.
#
# Outputs columns similar to your previous runs: venue, symbol, last, risk_floor, tp1,tp2,tp3, score
# and adds side, p_success, rr, expectancy. Prints only Top-5 overall (changeable with --topn/--per_venue).
#
# IMPORTANT: This approximates fixed-percent “Zyncas” levels:
#   risk = 10% from last; TP1=+12%, TP2=+22%, TP3=+31% for longs (mirror for shorts).
# Funding and OI are fetched and used in scoring; OI trend is not used (one-shot public endpoints).

import os
import sys
import math
import argparse
import datetime as dt
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np

# ---------- Config ----------
BINANCE_FAPI = "https://fapi.binance.com"
BYBIT_API = "https://api.bybit.com"

# Fixed-percent model (can be tuned via CLI)
RISK_PCT = 0.10    # 10% stop
TP1_PCT  = 0.12    # 12% target
TP2_PCT  = 0.22    # 22% target
TP3_PCT  = 0.31    # 31% target

# Prob mapping (score 0..5 -> probability via logistic)
PROB_A = 1.2
PROB_B = 3.5

# Networking
TIMEOUT = 8
RETRIES = 2
MAX_WORKERS = 40  # overall concurrency cap

# Liquidity gates for scoring (quote-volume USDT 24h)
VOL1 = 2.0e7   # 20M -> +1
VOL2 = 2.0e8   # 200M -> +2

# ---------- http helpers ----------
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=RETRIES, pool_connections=100, pool_maxsize=200)
session.mount("http://", adapter)
session.mount("https://", adapter)

def _get(url: str, params: Optional[Dict]=None) -> Optional[requests.Response]:
    try:
        r = session.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            return r
        else:
            return None
    except requests.RequestException:
        return None

# ---------- Binance data ----------
def binance_futures_symbols() -> List[str]:
    url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    r = _get(url)
    if not r:
        return []
    data = r.json()
    out = []
    for s in data.get("symbols", []):
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL":
            out.append(s["symbol"])
    return out

def binance_24h_map() -> Dict[str, Dict]:
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    r = _get(url)
    m = {}
    if not r:
        return m
    try:
        arr = r.json()
        for it in arr:
            sym = it.get("symbol")
            if not sym:
                continue
            m[sym] = it
    except Exception:
        pass
    return m

def binance_premium_index(symbol: str) -> Tuple[Optional[float], Optional[int]]:
    """Returns (fundingRate, nextFundingTime_ms) from /premiumIndex. fundingRate is decimal per 8h."""
    url = f"{BINANCE_FAPI}/fapi/v1/premiumIndex"
    r = _get(url, {"symbol": symbol})
    if not r:
        return (None, None)
    try:
        j = r.json()
        fr = j.get("lastFundingRate")
        nft = j.get("nextFundingTime")
        return (float(fr) if fr is not None else None, int(nft) if nft is not None else None)
    except Exception:
        return (None, None)

def binance_open_interest(symbol: str) -> Optional[float]:
    url = f"{BINANCE_FAPI}/fapi/v1/openInterest"
    r = _get(url, {"symbol": symbol})
    if not r:
        return None
    try:
        j = r.json()
        oi = j.get("openInterest")
        return float(oi) if oi is not None else None
    except Exception:
        return None

# ---------- Bybit data ----------
def bybit_linear_symbols() -> List[str]:
    url = f"{BYBIT_API}/v5/market/instruments-info"
    r = _get(url, {"category": "linear"})
    out = []
    if not r:
        return out
    try:
        j = r.json()
        for it in j.get("result", {}).get("list", []):
            if it.get("quoteCoin") == "USDT" and it.get("status") in ("Trading", "Listed"):
                out.append(it["symbol"])
    except Exception:
        pass
    return out

def bybit_tickers_map() -> Dict[str, Dict]:
    url = f"{BYBIT_API}/v5/market/tickers"
    r = _get(url, {"category": "linear"})
    m = {}
    if not r:
        return m
    try:
        j = r.json()
        for it in j.get("result", {}).get("list", []):
            m[it["symbol"]] = it
    except Exception:
        pass
    return m

def bybit_open_interest(symbol: str) -> Optional[float]:
    """Try v5 OI endpoint; return latest OI as float contracts."""
    url = f"{BYBIT_API}/v5/market/open-interest"
    r = _get(url, {"category": "linear", "symbol": symbol, "interval": "5min", "limit": 1})
    if not r:
        return None
    try:
        j = r.json()
        arr = j.get("result", {}).get("list", [])
        if arr:
            oi = arr[0].get("openInterest")
            return float(oi) if oi is not None else None
    except Exception:
        return None
    return None

# ---------- Model / scoring ----------
def fixed_levels(last: float, side: str) -> Tuple[float, float, float, float]:
    if side == "long":
        risk = last * (1 - RISK_PCT)
        tp1  = last * (1 + TP1_PCT)
        tp2  = last * (1 + TP2_PCT)
        tp3  = last * (1 + TP3_PCT)
    else:
        risk = last * (1 + RISK_PCT)  # for short, risk is above price
        tp1  = last * (1 - TP1_PCT)
        tp2  = last * (1 - TP2_PCT)
        tp3  = last * (1 - TP3_PCT)
    return risk, tp1, tp2, tp3

def prob_from_score(score: float, a: float=PROB_A, b: float=PROB_B) -> float:
    p = 1.0/(1.0 + math.exp(-a*(score - b)))
    return max(0.05, min(0.98, p))

def expectancy_and_rr(last: float, risk: float, tp1: float, tp2: float, tp3: float, side: str, p_success: float) -> Tuple[float,float]:
    if side == "long":
        win1 = max((tp1 - last)/last, 0.0)
        win2 = max((tp2 - last)/last, 0.0)
        win3 = max((tp3 - last)/last, 0.0)
        loss = max((last - risk)/last, 1e-6)
    else:
        win1 = max((last - tp1)/last, 0.0)
        win2 = max((last - tp2)/last, 0.0)
        win3 = max((last - tp3)/last, 0.0)
        loss = max((risk - last)/last, 1e-6)
    avg_win = 0.5*win1 + 0.3*win2 + 0.2*win3
    rr = (avg_win / loss) if loss > 0 else float("inf")
    ev = p_success*avg_win - (1 - p_success)*loss
    return ev, rr

def score_components(side: str, funding: Optional[float], quote_vol_usd: Optional[float], day_change_pct: Optional[float]) -> float:
    score = 0.0
    # Liquidity
    if quote_vol_usd is not None:
        if quote_vol_usd >= VOL2:
            score += 2.0
        elif quote_vol_usd >= VOL1:
            score += 1.0
    # Funding tilt
    if funding is not None:
        if side == "long":
            if funding <= -0.01:
                score += 2.0
            elif funding <= 0.0:
                score += 1.0
        else:
            if funding >= 0.01:
                score += 2.0
            elif funding >= 0.0:
                score += 1.0
    # Trend align (light)
    if day_change_pct is not None:
        if side == "long" and day_change_pct >= 0.0:
            score += 1.0
        if side == "short" and day_change_pct <= 0.0:
            score += 1.0
    return min(score, 5.0)

# ---------- Build rows per venue ----------
def rows_from_binance(symbols: List[str]) -> List[Dict]:
    rows = []
    tick = binance_24h_map()
    if not tick:
        return rows
    # precompute per-symbol funding + OI concurrently
    def work(sym):
        fr, nft = binance_premium_index(sym)
        oi = binance_open_interest(sym)
        return sym, fr, nft, oi
    syms = [s for s in symbols if s in tick]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(work, s): s for s in syms}
        fr_map = {}; oi_map = {}; nft_map = {}
        for f in as_completed(futs):
            s = futs[f]
            try:
                sym, fr, nft, oi = f.result()
                fr_map[sym] = fr
                oi_map[sym] = oi
                nft_map[sym] = nft
            except Exception:
                fr_map[s] = None; oi_map[s] = None; nft_map[s] = None
    now_utc = dt.datetime.now(dt.timezone.utc)
    for s in syms:
        t = tick[s]
        try:
            last = float(t.get("lastPrice", "nan"))
            if not np.isfinite(last) or last <= 0:
                continue
            # Daily change percent
            chg = t.get("priceChangePercent")
            day_chg = float(chg) if chg is not None else None
            quote_vol = float(t.get("quoteVolume")) if t.get("quoteVolume") is not None else None
            # Evaluate both sides, keep better expectancy
            best = None
            best_side = None
            best_pack = None
            for side in ("long", "short"):
                risk, tp1, tp2, tp3 = fixed_levels(last, side)
                sc = score_components(side, fr_map.get(s), quote_vol, day_chg)
                p = prob_from_score(sc)
                ev, rr = expectancy_and_rr(last, risk, tp1, tp2, tp3, side, p)
                if (best is None) or (ev > best):
                    best = ev; best_side = side; best_pack = (risk, tp1, tp2, tp3, sc, p, rr)
            risk, tp1, tp2, tp3, sc, p, rr = best_pack
            rows.append({
                "venue": "binance",
                "symbol": s,
                "side": best_side,
                "last": last,
                "risk_floor": risk,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "score": sc,
                "p_success": p,
                "rr": rr,
                "expectancy": best,
                "funding": fr_map.get(s),
                "oi": oi_map.get(s),
                "t_utc": now_utc.isoformat(timespec="seconds").replace("+00:00","Z"),
                "next_funding_time": nft_map.get(s)
            })
        except Exception:
            continue
    return rows

def rows_from_bybit(symbols: List[str]) -> List[Dict]:
    rows = []
    tick = bybit_tickers_map()
    if not tick:
        return rows
    # OI concurrently (funding is inside tickers)
    def work(sym):
        oi = bybit_open_interest(sym)
        return sym, oi
    syms = [s for s in symbols if s in tick]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(work, s): s for s in syms}
        oi_map = {}
        for f in as_completed(futs):
            s = futs[f]
            try:
                sym, oi = f.result()
                oi_map[sym] = oi
            except Exception:
                oi_map[s] = None
    now_utc = dt.datetime.now(dt.timezone.utc)
    for s in syms:
        t = tick[s]
        try:
            last = float(t.get("lastPrice"))
            if not np.isfinite(last) or last <= 0:
                continue
            day_chg = float(t.get("price24hPcnt"))*100.0 if t.get("price24hPcnt") is not None else None
            quote_vol = float(t.get("turnover24h")) if t.get("turnover24h") is not None else None
            fr = float(t.get("fundingRate")) if t.get("fundingRate") is not None else None
            best = None; best_side=None; best_pack=None
            for side in ("long","short"):
                risk, tp1, tp2, tp3 = fixed_levels(last, side)
                sc = score_components(side, fr, quote_vol, day_chg)
                p = prob_from_score(sc)
                ev, rr = expectancy_and_rr(last, risk, tp1, tp2, tp3, side, p)
                if (best is None) or (ev > best):
                    best=ev; best_side=side; best_pack=(risk,tp1,tp2,tp3,sc,p,rr)
            risk, tp1, tp2, tp3, sc, p, rr = best_pack
            rows.append({
                "venue": "bybit",
                "symbol": s,
                "side": best_side,
                "last": last,
                "risk_floor": risk,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "score": sc,
                "p_success": p,
                "rr": rr,
                "expectancy": best,
                "funding": fr,
                "oi": oi_map.get(s),
                "t_utc": now_utc.isoformat(timespec="seconds").replace("+00:00","Z"),
                "next_funding_time": None
            })
        except Exception:
            continue
    return rows

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Zyncas scanner (Binance+Bybit) that prints only Top-5 trades.")
    ap.add_argument("--topn", type=int, default=5, help="How many total rows to show.")
    ap.add_argument("--per_venue", action="store_true", help="Show Top-N per venue separately instead of overall.")
    ap.add_argument("--include", type=str, default="binance,bybit", help="Comma list of venues to include: binance,bybit")
    ap.add_argument("--save_csv", action="store_true", help="Also save raw rows to ./logs/scan_latest.csv and top to ./logs/topN_latest.csv")
    ap.add_argument("--risk", type=float, default=RISK_PCT, help="Risk percent (default 0.10)")
    ap.add_argument("--tp1", type=float, default=TP1_PCT, help="TP1 percent (default 0.12)")
    ap.add_argument("--tp2", type=float, default=TP2_PCT, help="TP2 percent (default 0.22)")
    ap.add_argument("--tp3", type=float, default=TP3_PCT, help="TP3 percent (default 0.31)")
    ap.add_argument("--min_score", type=float, default=3.5, help="Minimum score gate before ranking (default 3.5)")
    args = ap.parse_args()

    global RISK_PCT, TP1_PCT, TP2_PCT, TP3_PCT
    RISK_PCT, TP1_PCT, TP2_PCT, TP3_PCT = args.risk, args.tp1, args.tp2, args.tp3

    include = set(x.strip().lower() for x in args.include.split(",") if x.strip())
    rows_all: List[Dict] = []

    if "binance" in include:
        syms = binance_futures_symbols()
        if syms:
            rows_all.extend(rows_from_binance(syms))
    if "bybit" in include:
        syms = bybit_linear_symbols()
        if syms:
            rows_all.extend(rows_from_bybit(syms))

    if not rows_all:
        print("[Zyncas] No data pulled. Check your internet or reduce venues.")
        sys.exit(1)

    df = pd.DataFrame(rows_all)
    # Gate score
    df = df[df["score"] >= args.min_score].copy()
    if df.empty:
        print("[Zyncas] No rows passed the score gate. Try lowering --min_score.")
        sys.exit(0)

    if args.per_venue:
        out_frames = []
        for v, g in df.groupby("venue"):
            topv = g.sort_values(["expectancy","p_success","rr"], ascending=[False,False,False]).head(args.topn)
            out_frames.append(topv)
        top = pd.concat(out_frames, ignore_index=True)
    else:
        top = df.sort_values(["expectancy","p_success","rr"], ascending=[False,False,False]).head(args.topn).reset_index(drop=True)

    view_cols = ["venue","symbol","side","last","risk_floor","tp1","tp2","tp3","score","p_success","rr","expectancy","funding","oi","t_utc"]
    view = top[view_cols].copy()
    for c in ["last","risk_floor","tp1","tp2","tp3","p_success","rr","expectancy","funding","oi"]:
        if c in view.columns:
            view[c] = view[c].apply(lambda z: float(z) if z is not None and str(z) != "nan" else np.nan)
    # Pretty print
    view["p_success"]  = view["p_success"].map(lambda z: f"{z:.3f}")
    view["rr"]         = view["rr"].map(lambda z: f"{z:.3f}")
    view["expectancy"] = view["expectancy"].map(lambda z: f"{z:.3f}")
    view["last"]       = view["last"].map(lambda z: f"{z:.6f}")
    view["risk_floor"] = view["risk_floor"].map(lambda z: f"{z:.6f}")
    view["tp1"]        = view["tp1"].map(lambda z: f"{z:.6f}")
    view["tp2"]        = view["tp2"].map(lambda z: f"{z:.6f}")
    view["tp3"]        = view["tp3"].map(lambda z: f"{z:.6f}")
    if "funding" in view.columns:
        view["funding"] = view["funding"].map(lambda z: "—" if (z!=z) else f"{z:.6f}")
    if "oi" in view.columns:
        view["oi"] = view["oi"].map(lambda z: "—" if (z!=z) else f"{z:.3f}")

    print("\n[Zyncas Top] Highest Expectancy picks:\n")
    print(view.to_string(index=False))

    if args.save_csv:
        os.makedirs("logs", exist_ok=True)
        pd.DataFrame(rows_all).to_csv("logs/scan_latest.csv", index=False)
        top.to_csv("logs/topN_latest_raw.csv", index=False)
        view.to_csv("logs/topN_latest_pretty.csv", index=False)
        print("\n[Zyncas] Saved logs/scan_latest.csv, logs/topN_latest_raw.csv, logs/topN_latest_pretty.csv")

if __name__ == "__main__":
    main()
