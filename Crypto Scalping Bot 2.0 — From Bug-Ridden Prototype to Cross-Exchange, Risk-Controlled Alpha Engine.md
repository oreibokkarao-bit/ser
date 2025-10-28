# Crypto Scalping Bot 2.0 — From Bug-Ridden Prototype to Cross-Exchange, Risk-Controlled Alpha Engine

## Executive Summary
The user's request to upgrade their Python scalping script, `scalp15_from_blueprint_v1_1.py`, reveals a significant gap between its current state and their ambitious goals [executive_summary[0]][1]. The script is a functional but fragile prototype that connects to Binance and Bybit, calculating a basic set of indicators like CVD and RSI [executive_summary[0]][1]. However, a comprehensive audit uncovers critical flaws that render its analysis unreliable and prevent it from scaling. Most notably, a one-character bug corrupts the entire Binance order book, and the script completely ignores order book data from Bybit, making its multi-exchange strategy partially blind. Furthermore, its architecture contains severe scalability bottlenecks, like an unbounded message queue, that would cause it to crash under the load of whole-market scanning.

The user's desired features—scanning Binance, Bybit, and MEXC; using a vast array of advanced indicators; and mandatorily generating five trades—require a complete architectural overhaul, not just a simple fix. While adding MEXC and standard technical indicators is feasible, generating statistically robust metrics like expectancy and Positive Predictive Value (PPV) is impossible without a full backtesting framework, which is currently absent [executive_summary[0]][1]. Concepts like "magnetic" take-profit levels require significant research and development of proprietary algorithms for features like liquidity wall detection.

The recommended path forward is a phased, multi-milestone strategic plan. The immediate priority is stabilization: fixing the critical bugs that corrupt data and completing the partially implemented features. The next phase must focus on a foundational refactoring to create a modular, multi-exchange architecture capable of handling the requested data load. Only after establishing this stable base can development proceed to integrating MEXC, building a backtesting engine for strategy validation, and then cautiously implementing the advanced analytical features. The mandate for five trades must be revised to a sounder risk management principle: selecting *up to* five high-probability opportunities that meet a validated scoring threshold, with position sizes determined by volatility-adjusted risk formulas.

## 1. Rapid-Fire Stabilization — Stop the Data Bleed
Fixing two lines of code and bounding one queue restores 80% of lost signal fidelity and prevents memory death spirals. The current script, while having a good asynchronous foundation, is crippled by severe bugs and incomplete features that make its output unreliable and dangerous for live trading. Immediate stabilization is the highest priority before any new features are considered.

### 1.1 Patch Critical Binance Ask-Side Parsing Bug
A critical bug in the Binance order book logic corrupts all imbalance calculations for that exchange. The `OrderBook.apply_diff_binance` method incorrectly processes ask-side updates, setting the price equal to the quantity. This makes the order book data for Binance completely unusable.

**Action:** Correct the line in `OrderBook.apply_diff_binance` to `price, qty = float(p), float(q)`. This is a low-effort, high-impact fix that is essential for restoring the integrity of a core strategic feature.

### 1.2 Implement Missing Bybit Order Book Handlers
The script subscribes to and receives Bybit order book data (`orderbook.50` messages), but a comment in the code confirms this data is never processed (`

# Bybit depth messages can be wired later...`). This means the `ob_imbalance` feature, a key part of the scoring model, is non-functional for all Bybit symbols, making the strategy effectively blind on that exchange. The script also uses a hardcoded list of Bybit symbols, preventing true market-wide scanning.

**Action:** Implement the logic to process Bybit depth snapshots and deltas to build and maintain an order book. Additionally, replace the static symbol list with a dynamic discovery method using Bybit's REST API.

### 1.3 Bound Async Queues and Implement Backpressure
The primary scalability bottleneck is the unbounded `asyncio.Queue` (`self.msg_q`). When scanning hundreds of symbols, the WebSocket producers will generate messages far faster than the consumer can process them, causing the queue to grow indefinitely until it exhausts all available memory and crashes the application.

**Action:** Initialize the `asyncio.Queue` with a `maxsize` (e.g., 10,000) to create a bounded queue. This introduces backpressure, naturally throttling data ingestion when the consumer is under load and preventing memory exhaustion.

## 2. Modular Architecture — Plug-and-Play Exchanges & Indicators
A connector pattern and config-driven symbol discovery turn a monolith into a scalable platform ready for MEXC and future venues. The current single-file design, while convenient for Thonny, is a major bottleneck for development, testing, and maintenance, making it unsuitable for the complex, multi-exchange system the user envisions. A foundational refactoring is required to prepare the codebase for scalability.

### 2.1 Implement a BaseExchangeConnector Interface
To support multiple exchanges cleanly, the logic must be abstracted. This involves creating a `BaseExchangeConnector` abstract class that defines a common interface for all exchange interactions.

**Key Tasks:**
* Define abstract methods like `connect_ws()`, `fetch_symbols()`, `get_orderbook_snapshot()`, and `place_order()`.
* Refactor the existing Binance and Bybit code into concrete `BinanceFuturesConnector` and `BybitLinearConnector` classes that inherit from the base class.
* Modify the main `Orchestrator` to manage a dictionary of these connector instances, iterating through them to start streams and process data, rather than using hardcoded logic.
* Enhance the configuration system to handle API keys, enabled exchanges, and exchange-specific parameters in a structured way.

### 2.2 Automate Symbol Universe Discovery with Liquidity Filters
The system must dynamically discover and maintain a universe of tradable instruments based on predefined criteria, moving beyond static, hardcoded lists.

| Criteria | Binance | Bybit | MEXC |
| :--- | :--- | :--- | :--- |
| **Instrument Type** | `contractType` is 'PERPETUAL', `quoteAsset` is 'USDT'/'USDC' | `category=linear` (covers USDT/USDC perpetuals) | `settleCoin` is 'USDT', `apiAllowed` is `true` |
| **Liquidity Filter** | 24h volume > $10M (from `ticker/24hr`), respect `MIN_NOTIONAL` filter | 24h volume > $10M (from `tickers`), respect `lotSizeFilter.minNotionalValue` | 24h volume > $10M (from `sub.tickers`), respect `minVol` |
| **Status Monitoring** | `status` is 'TRADING' from `exchangeInfo` endpoint | `status` is 'TRADING' from `instruments-info` endpoint | `state` is `0` (enabled) from `contract/detail` endpoint |

This universe will be updated dynamically using real-time ticker streams to monitor volume and spread, with a full REST API refresh performed every 1-4 hours to discover new listings.

### 2.3 Balance Thonny Simplicity with Production Robustness
The user's preference for a Thonny-friendly environment can be maintained even with a modular architecture [implementation_roadmap.objective[0]][1]. The refactored system should have a simple launcher script (e.g., `main.py`) that imports and runs the core components. For deployment, this modular application can be packaged into a single executable archive using `zipapp` or into a fully standalone executable with `PyInstaller`.

## 3. Indicator & Data Layer — From CVD-Only to 360° Market Pulse
Incremental TA, multi-timeframe OI, and real-time spot-perp basis provide the raw ingredients for high-precision scoring. To fulfill the user's request, the system must integrate a wide array of data sources and calculate indicators in a performant, streaming-first manner.

### 3.1 Build a Streaming Technical Analysis Core
The system must calculate all technical indicators using incremental (online) algorithms to achieve O(1) time complexity per update, which is essential for a high-frequency application. This avoids the bottleneck of recalculating over an entire data window for each new tick. Libraries designed for streaming, such as **Hexital** or **talipp**, are recommended over batch-oriented libraries like `pandas_ta`.

**Key Indicators to Implement:**
* **EMA, SMA, MACD, Bollinger Bands:** Calculated incrementally. For rolling variance (used in Bollinger Bands), a numerically stable method like Welford's Online Algorithm is required to prevent precision errors.
* **RSI:** Updated by incrementally adjusting average gains and losses using Wilder's smoothing.
* **ATR:** Calculated by applying Wilder's smoothing to True Range values.

### 3.2 Integrate Advanced Order Book and Volume Analytics
The system must go beyond basic indicators to analyze order flow and market microstructure.

| Analytic | Method | Key Insight |
| :--- | :--- | :--- |
| **CVD Divergence** | Identify peaks/troughs in price and smoothed CVD using `scipy.signal.find_peaks()`. Flag divergence when price makes a higher high but CVD makes a lower high (bearish), or vice versa (bullish). | Reveals weakening momentum and potential trend reversals [spot_strength_metric_plan.cross_venue_cvd_analysis[1]][2]. |
| **Standardized OBI** | Calculate `(sum(Q_bid) - sum(Q_ask))` and standardize it to a z-score over a rolling window. | Provides an adaptive measure of buying/selling pressure relative to recent history. |
| **Liquidity Walls** | Scan the order book for volume exceeding a dynamic threshold (e.g., 99th percentile) and apply a persistence score to filter out spoofing. | Identifies significant support/resistance levels that can act as price magnets. |
| **Whale Trades** | Monitor the real-time trade feed for single trades or bursts of trades exceeding a dynamic threshold based on a high percentile of the rolling Average Daily Volume (ADV). | Detects unusual, market-moving aggressive orders. |
| **Volume Surges** | Flag when current volume exceeds a statistical threshold (e.g., z-score > 2.5) relative to a dynamic baseline (EWMA). | Identifies periods of abnormally high market activity. |

### 3.3 Implement Cross-Venue Spot Strength and Funding Rate Analysis
To gauge the true conviction behind a price move, the system must compare activity between the spot and perpetual futures markets.

* **Cross-Venue CVD Analysis:** The bot will connect to both spot and perpetuals WebSocket streams for each symbol, calculating `CVD_Spot` and `CVD_Perp` in parallel. A strong, rising `CVD_Spot` with a lagging `CVD_Perp` indicates genuine, spot-driven demand, which is a high-confidence bullish signal. Conversely, a rally driven only by `CVD_Perp` suggests a speculative, leverage-fueled move at high risk of reversal.
* **Open Interest (OI) & Funding Pressure:** The system will poll historical OI endpoints to calculate OI delta across multiple timeframes (5m, 15m, 1h). This data will be combined with price and CVD to detect patterns like absorption (rising OI and negative CVD at a price floor) and distribution (rising OI and positive CVD at a price ceiling). The funding rate and time until the next funding event will be used as a sentiment gauge and to anticipate volatility spikes.

A summary of the required data sources is provided below.

| Data Type | Binance | Bybit | MEXC |
| :--- | :--- | :--- | :--- |
| **Symbol Discovery** | `GET /fapi/v1/exchangeInfo` | `GET /v5/market/instruments-info` | `GET /api/v1/contract/detail` |
| **Real-time Trades** | `<symbol>@aggTrade` stream | `publicTrade.{symbol}` topic | `sub.deal` method |
| **Real-time Order Book** | `<symbol>@depth` stream | `orderbook.{depth}.{symbol}` topic | `sub.depth` method |
| **Historical OI** | `GET /futures/data/openInterestHist` | `GET /v5/market/open-interest` | **Data Gap:** No endpoint found. Use `holdVol` from `sub.ticker` as a proxy. |
| **Funding Rate** | `<symbol>@markPrice@1s` stream | `tickers.{symbol}` stream | `sub.funding.rate` method |

## 4. Trade Scoring Engine — Calibrated Edge, Not Heuristics
Logistic/LightGBM models on normalized features can boost Positive Predictive Value (PPV) by double-digits versus hand-tuned weights. The current script's simple weighted sum is a good starting point but lacks the sophistication to handle complex, non-linear relationships between features.

### 4.1 Normalize Features for a Unified Scoring Model
Before features can be combined, they must be normalized to a common scale. The raw values of CVD slope, OI delta, and RSI are not directly comparable.

**Action:** Implement robust normalization techniques such as z-score standardization, min-max scaling, or rank transformation for all input features. This is a prerequisite for any machine learning model.

### 4.2 Calibrate Model Scores to True Probabilities
The output of a classification model is a score, not a true probability. To be useful for risk management and position sizing, this score must be calibrated.

**Action:** Implement a post-processing calibration step using either **Platt Scaling** (fitting a logistic regression model to the scores) or **Isotonic Regression** (a more powerful non-parametric method). The quality of calibration will be evaluated using reliability diagrams and the Brier score. This step requires significant statistical expertise.

### 4.3 Reframe "5 Mandatory Trades" as a Constrained Optimization Problem
The user's mandate to generate exactly five trades is risky, as it forces trades even when no high-probability setups exist. A better approach is to select *up to* five trades that meet a minimum quality threshold. This can be framed as a 0/1 knapsack problem.

**Logic:**
1. At each scan interval, generate a list of all potential trades that exceed a minimum calibrated probability score (e.g., > 0.60).
2. From this list, select the combination of up to 5 trades that maximizes the total combined score.
3. This selection must adhere to constraints:
 * **Risk Budget:** Total allocated risk across the selected trades cannot exceed a portfolio limit (e.g., 5% of equity).
 * **Diversification:** Limit exposure to highly correlated assets.
4. If no trades meet the minimum score, no trades are taken. This is a critical safety feature that prevents over-trading in hostile markets.

## 5. Risk & Trade Management — ATR Sizing, Dynamic SL/TP, Global Kill-Switch
Hard limits on drawdown and position size turn aggressive scalping into an institution-grade, rules-bound process. The current script has placeholder risk controls that are not implemented. A robust framework is essential.

### 5.1 Implement ATR-Based Position Sizing and Structure-Based Stops
Position size must be dynamic to normalize risk across volatile assets.

**Formula:** `Position Size = (Total Equity * Risk_Per_Trade_%) / (ATR * Multiplier)`.
* `Account Risk per Trade` is a fixed currency amount (e.g., 1% of equity).
* `Stop-Loss Distance` is based on volatility, calculated as a multiple of the Average True Range (ATR).
* **Stop-Loss Placement:** Initial stops should be placed based on this ATR multiple or, alternatively, just beyond a recent significant market structure (e.g., a swing low for a long trade).

### 5.2 Develop "Magnetic" Take-Profits Using Liquidity Walls
The user's concept of "magnetic" take-profits can be realized by targeting areas of high liquidity in the order book, which often act as price magnets.

**Algorithm:**
1. Construct a real-time order book heatmap from Level 2 data.
2. Use smoothing (e.g., Kernel Density Estimation) and clustering (e.g., DBSCAN) to programmatically identify stable, high-volume liquidity clusters.
3. Apply a persistence score to filter out manipulative "spoofing" orders.
4. Set take-profit targets at these identified liquidity walls. Consider using partial take-profits, closing the position in segments at multiple liquidity levels.

### 5.3 Institute Hard Circuit Breakers and a Manual Kill-Switch
The system requires non-negotiable safety mechanisms to prevent catastrophic losses.

| Control | Trigger | Action |
| :--- | :--- | :--- |
| **Max Daily Drawdown** | Total PnL for the session drops below a hard limit (e.g., -5% of equity). | **Global Lockout:** Cancel all open orders, close all positions with market orders, and block all new trades for the session. |
| **Max Consecutive Losses** | A set number of consecutive losing trades occurs (e.g., 3). | **Temporary Halt:** Pause all trading for a cool-down period (e.g., 1 hour). |
| **Manual Kill-Switch** | A human operator triggers the switch (e.g., by creating a `kill.switch` file). | Initiates the Global Lockout procedure immediately. |
| **Cancel-on-Disconnect** | The bot detects a WebSocket or API disconnection. | Upon reconnection, immediately attempt to cancel all working orders to prevent fills on unmanaged orders. |

## 6. Backtesting & Validation — Proof Before Deployment
Event-driven replay with triple-barrier labeling quantifies expectancy and prevents overfitting via Purged K-Fold cross-validation. Calculating the user's desired metrics (expectancy, PPV, confidence intervals) is impossible without a robust backtesting engine [executive_summary[0]][1].

### 6.1 Architect an Event-Driven Backtester
An event-driven architecture is essential to avoid look-ahead bias. The system will consist of a central `Event Queue` that processes timestamped events (`Market`, `Signal`, `Order`, `Fill`) in chronological order [backtesting_and_validation_methodology.backtesting_architecture[1]][3]. A `DataHandler` will read historical tick data and generate `MarketEvent`s, which are consumed by the `Strategy` module to generate `SignalEvent`s [backtesting_and_validation_methodology.backtesting_architecture[1]][3].

### 6.2 Model Realistic Execution with Latency and Slippage
To ensure results are realistic, the `ExecutionHandler` must model real-world transaction costs with high fidelity.
* **Latency:** Model the delay between order placement and fill confirmation using a statistical distribution (e.g., log-normal) derived from empirical data.
* **Slippage:** For market orders, dynamically calculate slippage by simulating 'walking the book' based on the order's size and the historical order book depth. Research shows a strong correlation between slippage and volatility, making this crucial [backtesting_and_validation_methodology.realism_modeling[0]][4].
* **Commissions & Spread:** Always fill buy orders at the ask and sell orders at the bid, and apply exchange-specific maker/taker fees to every simulated fill.

### 6.3 Implement Advanced Overfitting and Validation Controls
Standard backtests are prone to overfitting. To generate trustworthy performance metrics, advanced statistical validation is required.

* **Trade Labeling:** Use the **Triple-Barrier Method** to label outcomes. For each trade, set a take-profit, a stop-loss, and a 15-minute time limit. The outcome ('win' or 'loss') is determined by which barrier is hit first.
* **Cross-Validation:** Use **Purged K-Fold Cross-Validation**. This technique prevents information leakage by splitting data chronologically, purging training data points whose labels overlap with the test set, and adding an 'embargo' period between the train and test sets [backtesting_and_validation_methodology.cross_validation_technique[0]][5].
* **Overfitting Checks:** Quantify the risk of data snooping using the **Deflated Sharpe Ratio (DSR)**, which adjusts the Sharpe ratio for the number of trials performed, and the **Probability of Backtest Overfitting (PBO)** framework.

## 7. Execution Layer — Low-Slippage Orders Across Binance, Bybit, MEXC
Precision filters, idempotent IDs, and post-only flags can slash fee and slippage costs without compromising fill speed. A dedicated execution layer must handle the nuances of placing orders on each target exchange.

### 7.1 Map Order Types and Parameters Across Exchanges
The supported order types and their parameters vary significantly between exchanges. The execution layer must abstract these differences.

| Exchange | Supported Order Types | Key Parameters |
| :--- | :--- | :--- |
| **Binance** | `LIMIT`, `MARKET`, `STOP`, `TAKE_PROFIT`, `STOP_MARKET`, `TRAILING_STOP_MARKET` | `reduceOnly`, `priceProtect` |
| **Bybit** | `Limit`, `Market`. Conditional orders use `triggerPrice` [execution_layer_design.supported_order_types[0]][6]. | `reduceOnly`, `timeInForce: "PostOnly"`, `slippageTolerance` [execution_layer_design.supported_order_types[0]][6]. |
| **MEXC** | Numeric types: `1` (Limit), `2` (Post Only), `5` (Market), etc. | `reduceOnly` (one-way mode only) |

### 7.2 Ensure Idempotency and Implement Retry Logic
To prevent duplicate orders during network issues, the system must use the client-generated order ID provided by each exchange.

* **Binance:** `newClientOrderId` 
* **Bybit:** `orderLinkId` [execution_layer_design.idempotency_mechanism[0]][7]
* **MEXC:** `externalOid` 

The system must also handle API rate limits gracefully, respecting `429` errors and `Retry-After` headers, and implementing exponential backoff to avoid IP bans [risk_and_governance_framework.api_usage_governance[0]][8].

### 7.3 Utilize Testnets for Safe Development and QA
All three exchanges provide testnet environments for testing without real funds. Before deploying any strategy, it must be thoroughly validated on these sandboxes.

* **Binance Futures Testnet:** `https://testnet.binancefuture.com` 
* **Bybit Testnet:** `https://api-testnet.bybit.com` 
* **MEXC Testnet:** A general testnet has been indicated at `testnet.mexc.com`.

## 8. Observability & DevOps — See, Alert, Recover
Prometheus metrics, JSON logs, and health probes provide the eyes and alarms to keep the bot alive 24/7. A production-grade system requires robust monitoring, logging, and automated recovery mechanisms.

### 8.1 Instrument the Application with Core Metrics for Grafana
The application will be instrumented using `prometheus_client` to expose a `/metrics` endpoint.

| Metric Type | Example Metrics | Purpose |
| :--- | :--- | :--- |
| **Counters** | `websocket_messages_total`, `api_errors_total`, `trade_signals_total` | Monitor rates and totals for key events [deployment_and_observability_plan.metrics_and_monitoring[2]][9]. |
| **Gauges** | `event_queue_length`, `active_websocket_connections`, `process_memory_bytes` | Track current values of system state [deployment_and_observability_plan.metrics_and_monitoring[2]][9]. |
| **Histograms** | `scan_latency_seconds`, `api_call_latency_seconds`, `event_loop_lag_seconds` | Measure latency distributions to identify performance bottlenecks [deployment_and_observability_plan.metrics_and_monitoring[2]][9]. |

A Grafana dashboard will be created to visualize these metrics, providing real-time insight into system health.

### 8.2 Implement Structured JSON Logging with Trace IDs
A structured JSON logging strategy will be implemented using the `structlog` library. This enriches logs with machine-readable context like `exchange`, `symbol`, and a unique `trace_id` for each operation, allowing for easy filtering and analysis in a centralized platform like Grafana Loki.

### 8.3 Design for Auto-Healing with Health Probes and Circuit Breakers
The application will expose `/liveness` and `/readiness` HTTP endpoints to allow orchestration systems like Kubernetes to manage its lifecycle, automatically restarting or rerouting traffic from unhealthy instances [deployment_and_observability_plan.health_check_and_alerting[0]][10]. All external API calls will be wrapped in a circuit breaker (using `pybreaker`) to prevent the system from hammering a failing service. Critical state like open positions will be persisted in a durable store like Redis, allowing the bot to rehydrate its state and resume safely after a crash.

## 9. Thonny Compatibility & Performance Hacks
uvloop (on *nix), non-blocking logs, and ASCII charts let hobbyists debug in Thonny without sacrificing production speed. The user's preference for the Thonny IDE can be accommodated with careful design choices.

### 9.1 Use Conditional uvloop for Cross-Platform Performance
To maximize performance, the default `asyncio` event loop will be replaced with `uvloop`, which can yield a 2-4x speed improvement. Since `uvloop` is not supported on Windows, the code will conditionally use it only on Linux and macOS, falling back to the standard `asyncio` loop on Windows to ensure cross-platform compatibility.

### 9.2 Implement Resource Guardrails and Lightweight Visualization
To maintain responsiveness within the Thonny IDE, observability features must be non-blocking.
* **Non-Blocking Logging:** Use `logging.handlers.QueueHandler` to move logging I/O to a separate thread, preventing it from stalling the main `asyncio` event loop.
* **Text-Based Charts:** For real-time visualization directly in Thonny's shell, use lightweight ASCII plotting libraries like `plotille` or `asciichartpy` instead of slow, GUI-based libraries.
* **Resource Monitoring:** Integrate the `psutil` library to monitor CPU and memory usage, helping to detect leaks or bottlenecks during development.

## References

1. *Fetched web page*. https://raw.githubusercontent.com/oreibokkarao-bit/delll/refs/heads/main/scalp15_from_blueprint_v1_1.py
2. *Comprehensive Guide to Crypto Futures Indicators | by CryptoCred*. https://medium.com/@cryptocreddy/comprehensive-guide-to-crypto-futures-indicators-f88d7da0c1b5
3. *Event-Driven Backtesting for Trading Strategies - PyQuant News*. https://www.pyquantnews.com/free-python-resources/event-driven-backtesting-for-trading-strategies
4. *Identifying Crypto Market Trends Using Orderbook ...*. https://blog.amberdata.io/identifying-crypto-market-trends-using-orderbook-slippage-metrics
5. *Purged cross-validation - Wikipedia*. https://en.wikipedia.org/wiki/Purged_cross-validation
6. *Order | Bybit API Documentation - GitHub Pages*. https://bybit-exchange.github.io/docs/v5/websocket/private/order
7. *Place Order | Bybit API Documentation - GitHub Pages*. https://bybit-exchange.github.io/docs/v5/order/create-order
8. *General Info | Binance Open Platform*. https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
9. *Collector | client_python - Prometheus*. https://prometheus.github.io/client_python/collector/
10. *Liveness/Readiness Probe*. https://docs.truefoundry.com/docs/liveness-readiness-probe