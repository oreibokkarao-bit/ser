
import asyncio
import json
import logging
import os
import time
from math import ceil
from collections import deque, defaultdict

import aiohttp
import websockets
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from tabulate import tabulate

# Optional: uvloop for speed (available on macOS)
try:
    import uvloop
    uvloop.install()
except Exception:
    pass

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[logging.FileHandler("trading_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger("alpha_engine_ws_bot_auto_universe")

# ---------------------------
# Config (tuned for safety)
# ---------------------------
class Config:
    # Universe refresh cadence
    UNIVERSE_REFRESH_SEC = 15 * 60  # refresh symbol lists every 15 minutes

    # Shard sizes: keep well below Binance 1024 streams/conn limit
    BINANCE_SHARD_SIZE = 400   # aggTrade streams per WS
    BYBIT_BATCH_SUB_SIZE = 150 # symbols per subscribe payload (multiple payloads per connection ok)
    BITGET_BATCH_SUB_SIZE = 150

    # Max simultaneous WS connections per venue (stay far below IP limits)
    BINANCE_MAX_CONNS = 4      # 4*400 = ~1600 streams max
    # Bybit/Bitget use one connection; we split subscribes in chunks

    # Analysis cadence
    ANALYSIS_EVERY_SEC = 60

    # Data retention
    TRADES_MAXLEN = 1200

    # Risk model (placeholder, replaced later by magnetic levels engine)
    TP1_PCT = 0.12
    TP2_PCT = 0.22
    TP3_PCT = 0.31
    SL_PCT  = 0.10

# ---------------------------
# Utilities
# ---------------------------
async def http_get_json(session, url, params=None, headers=None, timeout=15):
    for attempt in range(5):
        try:
            async with session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                if resp.status in (429, 418, 451, 403):
                    text = await resp.text()
                    logger.warning(f"HTTP {resp.status} from {url} - backoff: {text[:200]}")
                    await asyncio.sleep(2 ** attempt + np.random.random())
                    continue
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            logger.warning(f"GET fail {url}: {e}; retrying...")
            await asyncio.sleep(2 ** attempt + np.random.random())
    raise RuntimeError(f"GET failed for {url} after retries")

# ---------------------------
# Universe Discovery
# ---------------------------
class Universe:
    def __init__(self):
        self.binance_fut = []  # e.g., BTCUSDT
        self.bybit_linear = []
        self.bitget_usdt_fut = []

    async def refresh(self):
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(
                self._refresh_binance_futures(session),
                self._refresh_bybit_linear(session),
                self._refresh_bitget_usdt_futures(session),
            )
        total = len(self.binance_fut) + len(self.bybit_linear) + len(self.bitget_usdt_fut)
        logger.info(f"[UNIVERSE] BinanceFut={len(self.binance_fut)}, BybitLinear={len(self.bybit_linear)}, BitgetUSDT={len(self.bitget_usdt_fut)} | total={total}")

    async def _refresh_binance_futures(self, session):
        # GET /fapi/v1/exchangeInfo
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        data = await http_get_json(session, url)
        syms = []
        for s in data.get("symbols", []):
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                syms.append(s["symbol"])
        self.binance_fut = sorted(list(set(syms)))

    async def _refresh_bybit_linear(self, session):
        # GET /v5/market/instruments-info?category=linear with pagination
        base = "https://api.bybit.com/v5/market/instruments-info"
        params = {"category": "linear"}
        out = []
        cursor = None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = await http_get_json(session, base, params=p)
            list_ = data.get("result", {}).get("list", []) or []
            for item in list_:
                if item.get("quoteCoin") == "USDT" and item.get("status") == "Trading":
                    out.append(item["symbol"])
            cursor = data.get("result", {}).get("nextPageCursor")
            if not cursor:
                break
        self.bybit_linear = sorted(list(set(out)))

    async def _refresh_bitget_usdt_futures(self, session):
        # GET /api/v3/market/instruments?category=USDT-FUTURES
        base = "https://api.bitget.com/api/v3/market/instruments"
        params = {"category": "USDT-FUTURES"}
        data = await http_get_json(session, base, params=params)
        out = []
        for item in data.get("data", []) or []:
            if item.get("quoteCoin") == "USDT" and item.get("status") == "online":
                out.append(item["symbol"])
        self.bitget_usdt_fut = sorted(list(set(out)))

# ---------------------------
# Market Data Structures
# ---------------------------
class MarketData:
    def __init__(self, symbol):
        self.symbol = symbol
        self.trades = deque(maxlen=Config.TRADES_MAXLEN)
        self.cvd = 0.0

# ---------------------------
# Connectors
# ---------------------------
class BinanceFuturesConnector:
    def __init__(self, symbols):
        self.symbols = symbols
        self.data = defaultdict(lambda: MarketData(""))
        self.tasks = []

    def shards(self):
        # combined stream shards
        n = max(1, min(Config.BINANCE_MAX_CONNS, ceil(len(self.symbols) / Config.BINANCE_SHARD_SIZE)))
        for i in range(n):
            chunk = self.symbols[i::n]  # stripe for even distribution
            if not chunk:
                continue
            streams = "/".join([f"{s.lower()}@aggTrade" for s in chunk])
            url = f"wss://fstream.binance.com/stream?streams={streams}"
            yield url, chunk

    async def listen_shard(self, url):
        name = "BinanceFut"
        while True:
            try:
                async with websockets.connect(url, ping_interval=15, ping_timeout=15) as ws:
                    logger.info(f"[{name}] Connected shard {url[-64:]}")
                    async for raw in ws:
                        msg = json.loads(raw)
                        stream = msg.get("stream", "")
                        data = msg.get("data", {})
                        if not stream or "@aggTrade" not in stream:
                            continue
                        s = data["s"]
                        p = float(data["p"]); q = float(data["q"]); is_bm = data["m"]
                        md = self.data.setdefault(s, MarketData(s))
                        signed = q if not is_bm else -q
                        md.cvd += signed
                        md.trades.append({"price": p, "qty": q, "side": "buy" if not is_bm else "sell"})
            except Exception as e:
                logger.warning(f"[{name}] shard error {e}; reconnecting after backoff")
                await asyncio.sleep(2 + np.random.random())

    async def start(self):
        for url, _ in self.shards():
            self.tasks.append(asyncio.create_task(self.listen_shard(url)))

class BybitLinearConnector:
    def __init__(self, symbols):
        self.symbols = symbols
        self.data = defaultdict(lambda: MarketData(""))
        self.ws = None

    async def start(self):
        name = "BybitLinear"
        url = "wss://stream.bybit.com/v5/public/linear"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    self.ws = ws
                    logger.info(f"[{name}] Connected.")
                    # subscribe in batches to keep payloads small
                    args = [f"publicTrade.{s}" for s in self.symbols]
                    for i in range(0, len(args), Config.BYBIT_BATCH_SUB_SIZE):
                        payload = {"op": "subscribe", "args": args[i:i+Config.BYBIT_BATCH_SUB_SIZE]}
                        await ws.send(json.dumps(payload))
                        await asyncio.sleep(0.2)  # don't exceed message rate limits
                    async for raw in ws:
                        msg = json.loads(raw)
                        topic = msg.get("topic", "")
                        if not topic.startswith("publicTrade."):
                            continue
                        for tr in msg.get("data", []):
                            s = tr["s"]; p = float(tr["p"]); q = float(tr["v"]); side = tr["S"]
                            md = self.data.setdefault(s, MarketData(s))
                            signed = q if side == "Buy" else -q
                            md.cvd += signed
                            md.trades.append({"price": p, "qty": q, "side": side.lower()})
            except Exception as e:
                logger.warning(f"[{name}] error {e}; reconnecting")
                await asyncio.sleep(2 + np.random.random())

class BitgetUSDTFutConnector:
    def __init__(self, symbols):
        self.symbols = symbols
        self.data = defaultdict(lambda: MarketData(""))
        self.ws = None

    async def start(self):
        name = "BitgetUSDT"
        url = "wss://ws.bitget.com/v2/ws/public"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                    self.ws = ws
                    logger.info(f"[{name}] Connected.")
                    # batched subscribe
                    args = [{"instType":"USDT-FUTURES","channel":"trade","instId": s} for s in self.symbols]
                    for i in range(0, len(args), Config.BITGET_BATCH_SUB_SIZE):
                        payload = {"op":"subscribe","args": args[i:i+Config.BITGET_BATCH_SUB_SIZE]}
                        await ws.send(json.dumps(payload))
                        await asyncio.sleep(0.25)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("action") != "update" or not msg.get("data"):
                            continue
                        inst = msg.get("arg",{}).get("instId")
                        for tr in msg["data"]:
                            p = float(tr[1]); q = float(tr[2]); side = tr[3]
                            md = self.data.setdefault(inst, MarketData(inst))
                            signed = q if side == "buy" else -q
                            md.cvd += signed
                            md.trades.append({"price": p, "qty": q, "side": side})
            except Exception as e:
                logger.warning(f"[{name}] error {e}; reconnecting")
                await asyncio.sleep(2 + np.random.random())

# ---------------------------
# Analysis Engine
# ---------------------------
class AnalysisEngine:
    @staticmethod
    def features_from_md(md: MarketData):
        if len(md.trades) < 20:
            return None
        prices = [t["price"] for t in md.trades]
        qtys   = [t["qty"]   for t in md.trades]
        df = pd.DataFrame({"p": prices, "q": qtys})
        ema9  = df["p"].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = df["p"].ewm(span=21, adjust=False).mean().iloc[-1]
        # CVD slope via simple linear fit
        x = np.arange(len(qtys), dtype=float)
        y = np.cumsum([t["qty"] if t["side"]=="buy" else -t["qty"] for t in md.trades])
        slope = float(np.polyfit(x, y, 1)[0])
        return {
            "symbol": md.symbol,
            "ema9": float(ema9),
            "ema21": float(ema21),
            "cvd": float(md.cvd),
            "cvd_slope": slope,
            "price": float(df["p"].iloc[-1]),
        }

    @staticmethod
    def plan_trade(f):
        entry = f["price"]
        sl  = entry * (1-Config.SL_PCT) if f["ema9"]>=f["ema21"] else entry * (1+Config.SL_PCT)
        tp1 = entry * (1+Config.TP1_PCT) if f["ema9"]>=f["ema21"] else entry * (1-Config.TP1_PCT)
        tp2 = entry * (1+Config.TP2_PCT) if f["ema9"]>=f["ema21"] else entry * (1-Config.TP2_PCT)
        tp3 = entry * (1+Config.TP3_PCT) if f["ema9"]>=f["ema21"] else entry * (1-Config.TP3_PCT)
        direction = "LONG" if f["ema9"]>=f["ema21"] and f["cvd_slope"]>0 else "SHORT"
        # toy scoring for demo
        base = 0.55 if direction=="LONG" else 0.52
        wp = max(0.0, min(0.95, base + 0.02*np.tanh(abs(f["cvd_slope"]))))  # clamp
        expect = (wp*1.5) - ((1-wp)*1.0)
        if direction == "LONG":
            r_tp1 = (tp1-entry)/max(entry-sl, 1e-12)
        else:
            r_tp1 = (entry-tp1)/max(sl-entry, 1e-12)
        return {
            "symbol": f["symbol"],
            "direction": direction,
            "win_probability": wp,
            "expectancy": expect,
            "confidence_score": min(1.0, 0.5 + 0.5*np.tanh(abs(f["cvd_slope"]))),
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "r_tp1": r_tp1,
        }

# ---------------------------
# Bot Orchestrator
# ---------------------------
class TradingBot:
    def __init__(self):
        self.universe = Universe()
        self.connectors = {}

    async def start_connectors(self):
        # build connectors from current universe
        tasks = []
        if self.universe.binance_fut:
            self.connectors["binance"] = BinanceFuturesConnector(self.universe.binance_fut)
            await self.connectors["binance"].start()
        if self.universe.bybit_linear:
            self.connectors["bybit"] = BybitLinearConnector(self.universe.bybit_linear)
            tasks.append(asyncio.create_task(self.connectors["bybit"].start()))
        if self.universe.bitget_usdt_fut:
            self.connectors["bitget"] = BitgetUSDTFutConnector(self.universe.bitget_usdt_fut)
            tasks.append(asyncio.create_task(self.connectors["bitget"].start()))
        return tasks

    def gather_features(self):
        feats = []
        for name, conn in self.connectors.items():
            for s, md in getattr(conn, "data", {}).items():
                f = AnalysisEngine.features_from_md(md)
                if f:
                    feats.append((name, f))
        return feats

    def print_top5(self, trades):
        if not trades:
            logger.info("No qualifying trades yet.")
            return
        trades = sorted(trades, key=lambda t: t["win_probability"], reverse=True)[:5]
        headers = ["#", "Exchange", "Symbol", "Dir", "Win %", "Expect.", "R@TP1", "Entry", "SL", "TP1", "TP2", "TP3"]
        rows = []
        for i, t in enumerate(trades, 1):
            rows.append([
                i, t["venue"], t["symbol"], "L" if t["direction"]=="LONG" else "S",
                f"{t['win_probability']*100:.1f}%", f"{t['expectancy']:.2f}", f"{t['r_tp1']:.2f}",
                f"{t['entry']:.6f}", f"{t['sl']:.6f}", f"{t['tp1']:.6f}", f"{t['tp2']:.6f}", f"{t['tp3']:.6f}",
            ])
        print("\n" + "="*72)
        print(f" TOP-5 TRADES — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        print("="*72 + "\n")
        print(tabulate(rows, headers=headers, tablefmt="grid"))
        print("\n" + "="*30 + " END OF SCAN " + "="*30 + "\n")

    async def run(self):
        # 1) initial universe
        await self.universe.refresh()
        connector_tasks = await self.start_connectors()

        # 2) periodic universe refresh
        async def refresher():
            while True:
                await asyncio.sleep(Config.UNIVERSE_REFRESH_SEC)
                await self.universe.refresh()
                # (Simplification) we won't auto-resubscribe mid-run to avoid churn; re-run script to pick up large changes

        asyncio.create_task(refresher())

        # 3) analysis loop
        while True:
            await asyncio.sleep(Config.ANALYSIS_EVERY_SEC)
            feats = self.gather_features()
            signals = []
            for venue, f in feats:
                plan = AnalysisEngine.plan_trade(f)
                plan["venue"] = venue
                signals.append(plan)
            self.print_top5(signals)

async def main():
    bot = TradingBot()
    await bot.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown by user.")
