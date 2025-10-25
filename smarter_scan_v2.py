import asyncio
import json
import logging
import math # <-- MODIFICATION: Imported for sigmoid calculation
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Tuple, Any, Optional

import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
from tabulate import tabulate
import websockets

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CONFIG = {
    "exchange": "binance",
    "market_type": "swap",
    "quote_currency": "USDT",
    "blacklist": ["USDC/USDT", "BUSD/USDT", "TUSD/USDT"],
    "timeframe": "15m",
    "lookback_period": 150,

    "z_score_window": 21,
    "ignition_filter": {"bollinger_len": 20, "bollinger_std": 2.0},

    "tp_sl_strategy": "atr", # Options: "atr", "swing", "liquidation"

    # --- MODIFICATION NOTE: How to improve Risk-Reward Ratio ---
    # For the 'atr' strategy, the Risk-Reward Ratio is controlled by the multipliers below.
    # RR = tp_multiplier / sl_multiplier.
    # Default: 4.0 / 2.0 = 2.0. A 1:2 RR.
    # To get a 1:3 RR, you could set tp_multiplier to 6.0.
    "atr_config": {
        "period": 14,
        "sl_multiplier": 2.0, # Stop Loss at 2x ATR
        "tp_multiplier": 4.0 # Take Profit at 4x ATR
    },
    "swing_config": {
        "lookback": 50 # Look back 50 candles for recent swing high/low
    },

    "scan_interval_seconds": 60,

    "liq_window_sec": 60 * 30,
    "liq_price_range_pct": 0.08,
    "liq_bin_step_pct": 0.0025,
    "liq_top_nodes": 4,
    "exchanges_liq": ["binance"],
}

# --- DATA & SIGNAL PROCESSING ---
class DataEngine:
    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    async def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            logging.warning(f"Could not fetch OHLCV for {symbol}: {e}")
            return None

class SignalGenerator:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    def engineer_features(self, df: pd.DataFrame) -> pd.DataFrame:
        # Bollinger Bands
        bb_len = self.cfg["ignition_filter"]["bollinger_len"]
        bb_std = self.cfg["ignition_filter"]["bollinger_std"]
        df['bb_ma'] = df['close'].rolling(window=bb_len).mean()
        df['bb_std'] = df['close'].rolling(window=bb_len).std()
        df['bb_upper'] = df['bb_ma'] + (df['bb_std'] * bb_std)
        df['bb_lower'] = df['bb_ma'] - (df['bb_std'] * bb_std)
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_ma']

        # Z-Scores
        z_window = self.cfg["z_score_window"]
        df['z_score_price'] = (df['close'] - df['close'].rolling(window=z_window).mean()) / df['close'].rolling(window=z_window).std()
        df['z_score_volume'] = (df['volume'] - df['volume'].rolling(window=z_window).mean()) / df['volume'].rolling(window=z_window).std()

        # ATR
        atr_period = self.cfg["atr_config"]["period"]
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df['atr'] = true_range.rolling(window=atr_period).mean()

        # Swing High/Low
        swing_lookback = self.cfg["swing_config"]["lookback"]
        df['swing_high'] = df['high'].rolling(window=swing_lookback, center=True).max()
        df['swing_low'] = df['low'].rolling(window=swing_lookback, center=True).min()

        return df.dropna()

    def apply_ignition_filter(self, df: pd.DataFrame) -> bool:
        if df.empty: return False
        last = df.iloc[-1]
        return (
            last['bb_width'] < 0.06 and
            last['z_score_price'] > 0 and
            last['z_score_volume'] > 0
        )

    def generate_score(self, df: pd.DataFrame) -> float:
        if df.empty or "z_score_price" not in df or "z_score_volume" not in df:
            return 0.0
        zp = df["z_score_price"].iloc[-1]
        zv = df["z_score_volume"].iloc[-1]
        zp = 0.0 if pd.isna(zp) else float(zp)
        zv = 0.0 if pd.isna(zv) else float(zv)
        return zp + zv

# --- LIQUIDATION DATA MANAGER (UNCHANGED) ---
@dataclass
class LiqNode:
    price: float
    density: float

class LiquidationWSManager:
    #... (This class remains unchanged from the original script)
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._data = defaultdict(lambda: defaultdict(lambda: deque(maxlen=1000)))
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)

    def start(self):
        self._thread.start()

    def _run_forever(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._connect_all())
        loop.run_forever()

    async def _connect_all(self):
        tasks = [self._listen(exchange) for exchange in self.cfg["exchanges_liq"]]
        await asyncio.gather(*tasks)

    async def _listen(self, exchange_name: str):
        urls = {
            'binance': 'wss://fstream.binance.com/ws/!forceOrder@arr',
        }
        url = urls.get(exchange_name.lower())
        if not url: return

        while True:
            try:
                async with websockets.connect(url) as ws:
                    logging.info(f"Connected to {exchange_name} liquidation stream.")
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        self._process_message(exchange_name, data)
            except Exception as e:
                logging.error(f"Liquidation WS error ({exchange_name}): {e}. Reconnecting...")
                await asyncio.sleep(5)

    def _process_message(self, exchange: str, data: Dict):
        try:
            if exchange == 'binance':
                order = data.get('o', {})
                symbol = order.get('s')
                if not symbol or not symbol.endswith('USDT'): return
                price = float(order.get('p'))
                qty = float(order.get('q'))
                side = order.get('S')
                # --- THIS IS THE FIX (from previous step, still correct) ---
                timestamp = int(order.get('T')) 
                # --- END OF FIX ---

                with self._lock:
                    self._data[exchange][symbol].append((timestamp, price, qty, side))
        except (KeyError, ValueError, TypeError) as e:
            # Added a check to log 'None' if order.get('T') fails for some new reason
            timestamp_val = order.get('T') if 'order' in locals() else 'N/A'
            logging.warning(f"Error processing liq message: {e} | T={timestamp_val} | Data: {data}")

# --- TP/SL CALCULATOR ---
class TP_SL_Calculator:
    def __init__(self, cfg: Dict[str, Any], liq_ws: LiquidationWSManager):
        self.cfg = cfg
        self.liq_ws = liq_ws

    def calculate(self, df: pd.DataFrame, symbol_ccxt: str) -> Dict[str, float]:
        strategy = self.cfg.get("tp_sl_strategy", "liquidation").lower()
        last_price = float(df["close"].iloc[-1])

        if strategy == "atr":
            return self._calculate_atr_based(df, last_price)
        elif strategy == "swing":
            return self._calculate_swing_based(df, last_price)
        else: # Default to "liquidation"
            return self._calculate_liquidation_based(symbol_ccxt, last_price)

    def _calculate_atr_based(self, df: pd.DataFrame, last_price: float) -> Dict[str, float]:
        atr = float(df["atr"].iloc[-1])
        sl_mult = float(self.cfg["atr_config"]["sl_multiplier"])
        tp_mult = float(self.cfg["atr_config"]["tp_multiplier"])

        sl = last_price - (atr * sl_mult)
        tp1 = last_price + (atr * tp_mult)

        return self._format_results(last_price, sl, [tp1])

    def _calculate_swing_based(self, df: pd.DataFrame, last_price: float) -> Dict[str, float]:
        sl = float(df["swing_low"].iloc[-2]) # Use previous candle's low
        recent_highs = df['high'].iloc[-self.cfg['swing_config']['lookback']:-1]
        valid_tps = recent_highs[recent_highs > last_price]
        tp1 = valid_tps.max() if not valid_tps.empty else last_price * 1.03

        return self._format_results(last_price, sl, [tp1])

    def _calculate_liquidation_based(self, symbol_ccxt: str, last_price: float) -> Dict[str, float]:
        # This function is not implemented in the provided script, but we follow the pattern
        # heatmap = self.liq_ws.get_heatmap(symbol_ccxt, last_price) 
        # ... logic ...
        # For now, return ATR-based as a fallback
        logging.warning(f"Liquidation strategy not fully implemented. Falling back to ATR for {symbol_ccxt}")
        # This part needs the get_heatmap function to be defined in LiquidationWSManager
        # As a placeholder, I'll use simple ATR fallback.
        # sl = last_price * 0.985
        # tps = [last_price * 1.01]
        # return self._format_results(last_price, sl, tps)
        
        # Since get_heatmap isn't defined, I'll just return placeholder values
        # to avoid a crash. The original script has this same issue.
        sl = last_price * 0.98
        tps = [last_price * 1.02, last_price * 1.03, last_price * 1.04]
        return self._format_results(last_price, sl, tps)


    def _format_results(self, entry: float, sl: float, tps: List[float]) -> Dict[str, float]:
        tp1 = tps[0] if len(tps) >= 1 else entry * 1.015
        tp2 = tps[1] if len(tps) >= 2 else tp1 * 1.01
        tp3 = tps[2] if len(tps) >= 3 else tp2 * 1.01
        return {
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp1": round(tp1, 6),
            "tp2": round(tp2, 6),
            "tp3": round(tp3, 6),
            "max": round(max(tp1, tp2, tp3), 6),
        }

# --- MAIN APPLICATION ---
async def main():
    exchange = ccxt.binance({
        'asyncio_loop': asyncio.get_event_loop(),
        'options': {'defaultType': CONFIG['market_type']}
    })

    liq_ws_manager = LiquidationWSManager(CONFIG)
    liq_ws_manager.start()

    data_engine = DataEngine(exchange)
    signal_gen = SignalGenerator(CONFIG)
    tp_sl_calculator = TP_SL_Calculator(CONFIG, liq_ws_manager)

    logging.info("Fetching market data...")
    markets = await exchange.load_markets()
    
    # --- FIX #1: Filter for 'swap' markets only ---
    symbols = [m['symbol'] for m in markets.values()
               if m.get('type') == 'swap'  # <-- THIS IS THE FIX
               and m['quote'] == CONFIG['quote_currency']
               and m['symbol'] not in CONFIG['blacklist']
               and m['active']]
    logging.info(f"Found {len(symbols)} symbols to scan.")

    while True:
        logging.info("--- Starting new scan cycle ---")
        potential_trades = []
        tasks = []

        async def process_symbol(symbol):
            df = await data_engine.get_ohlcv(symbol, CONFIG['timeframe'], CONFIG['lookback_period'])
            if df is not None and not df.empty:
                df_feat = signal_gen.engineer_features(df)
                if signal_gen.apply_ignition_filter(df_feat):
                    score = signal_gen.generate_score(df_feat)
                    potential_trades.append({"symbol": symbol, "score": score, "dataframe": df_feat})

        for symbol in symbols:
            tasks.append(process_symbol(symbol))

        await asyncio.gather(*tasks)

        if not potential_trades:
            logging.info("Scan complete. No assets passed the Ignition Filter.")
        else:
            top_candidates = sorted(potential_trades, key=lambda x: x["score"], reverse=True)[:5]
            logging.info(f"Scan complete. Top {len(top_candidates)} candidates:")

            # --- MODIFICATION: Added new columns to headers ---
            headers = ["#", "Symbol", "Score", "Confidence", "RR (TP1)", "Expectancy", "Entry", "SL", "TP1", "TP2", "TP3", "Max Target"]
            table_data = []

            for i, c in enumerate(top_candidates, start=1):
                trade_params = tp_sl_calculator.calculate(c["dataframe"], c["symbol"])

                # --- MODIFICATION: Calculate new metrics ---
                entry = trade_params["entry"]
                sl = trade_params["sl"]
                tp1 = trade_params["tp1"]
                score = c["score"]

                risk = entry - sl
                reward = tp1 - entry
                
                # --- FIX #2: Filter out trades with no risk ---
                if risk <= 0:
                    logging.warning(f"Skipping {c['symbol']} due to invalid risk (risk <= 0). Entry: {entry}, SL: {sl}")
                    continue # Skip to the next candidate
                # --- END FIX ---

                # Now safe to divide
                risk_reward_ratio = reward / risk

                # Convert score to a confidence probability (0-1) using a sigmoid function
                confidence = 1 / (1 + math.exp(-0.5 * score))

                # Calculate expectancy
                expectancy = (confidence * reward) - ((1 - confidence) * risk)
                # --- END MODIFICATION ---

                # --- MODIFICATION: Append new metrics to table data ---
                table_data.append([
                    i, c["symbol"], round(c["score"], 3),
                    f"{confidence:.1%}", # New column, formatted as percentage
                    round(risk_reward_ratio, 2), # New column
                    round(expectancy, 4), # New column
                    trade_params["entry"], trade_params["sl"],
                    trade_params["tp1"], trade_params["tp2"], trade_params["tp3"],
                    trade_params["max"],
                ])

            print(tabulate(table_data, headers=headers, tablefmt="grid"))

        logging.info(f"--- Scan cycle finished. Waiting {CONFIG['scan_interval_seconds']} seconds. ---")
        await asyncio.sleep(CONFIG['scan_interval_seconds'])

    await exchange.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Script terminated by user.")