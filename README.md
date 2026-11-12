# Kalshi BTC 15-minute signal bot (starter)

A free-first, read-only Streamlit dashboard for the Kalshi `KXBTC15M` series. It reads Coinbase BTC/USD spot and Kalshi public market data, estimates the probability that BTC finishes above the current market strike, compares that estimate with the displayed YES/NO asks, records one forecast per market, and gradually calibrates future forecasts using settled outcomes.

**It does not place trades.** It is a starter/research tool, not a proven profitable strategy.

## Important model limitations

- Kalshi BTC 15-minute settlement uses the CF Benchmarks Bitcoin Real-Time Index (BRTI), including a 60-second averaging rule. Coinbase spot is only a proxy and can disagree near the strike.
- The first model is a short-horizon lognormal/normal approximation using recent locally sampled Coinbase prices. It is not a trained high-quality forecasting model.
- The adaptive calibration requires at least 40 previously settled forecasts and is deliberately disabled before then. The first 40 outcomes are a warm-up period, not evidence of an edge.
- A single forecast is stored per market, when the app first sees it. Keep the app running through multiple market windows to build a record.
- Streamlit Community Cloud may sleep or restart apps and its local filesystem is not durable. The SQLite history can reset. Free hosting is not a guaranteed 24/7 alert service.
- The ntfy public service uses public topic names. Use a long random topic; anyone who knows it may be able to publish to or subscribe to it. Do not put personal information in alerts.

## Run locally

1. Install Python 3.10 or newer.
2. Unzip the project and open a terminal in this folder.
3. Create and activate a virtual environment (optional but recommended):
   - macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate`
   - Windows: `py -m venv .venv && .venv\\Scripts\\activate`
4. Install packages: `pip install -r requirements.txt`
5. Start the dashboard: `streamlit run app.py`
6. Open the local URL printed by Streamlit.

## iPhone notifications with ntfy

1. Install **ntfy** from the iOS App Store.
2. In the app, subscribe to a long, randomly generated topic name. Do not use a simple name like `btc-alerts`.
3. In Streamlit, open **App settings → Secrets** and add:

   ```toml
   NTFY_TOPIC = "replace-with-a-long-random-topic-name"
   ```

4. Save/restart the app. The bot sends one push alert per market when it first detects a qualifying UP or DOWN signal.

If running locally instead of Streamlit Cloud, set the environment variable `NTFY_TOPIC` before starting Streamlit.

## Free phone-accessible hosting (optional)

1. Create a GitHub repository and upload `app.py`, `requirements.txt`, and `README.md`.
2. Sign in to Streamlit Community Cloud and deploy the repository, selecting `app.py` as the entry point.
3. Add `NTFY_TOPIC` under the app's Secrets settings.
4. Open the deployed app URL in Safari on iPhone and use **Share → Add to Home Screen**.

Free cloud apps may sleep, be restarted, or lose local SQLite data. For continuous alerts, use an always-on machine/server; that may cost money.

## How signals are generated

- `P(UP)` is estimated from the log distance between Coinbase spot and Kalshi's `floor_strike`, remaining time, and recent realized volatility/drift.
- If at least 40 previous forecasts have settled, a small regularized logistic calibration is fitted to past forecast/outcome pairs.
- The app only displays UP/DOWN when the model probability is at least 55% and the model-vs-ask difference is at least 3 percentage points. Otherwise it says WAIT.
- The edge is a simple probability-minus-ask comparison and does not fully model fees, spread/slippage, settlement-index mismatch, or execution quality. It is not a trade recommendation.

## Troubleshooting

- **No market found:** wait for the next `KXBTC15M` window or check whether Kalshi's public API is responding.
- **Collecting price history:** leave the app running until it has at least 20 price samples.
- **No phone alert:** check ntfy subscription, iPhone notification permissions, and the topic secret. Alerts are only sent when a signal is first detected for a market.
- **Data resets:** local SQLite storage on free cloud hosting is not persistent; download/export records or move to persistent storage before relying on long-term calibration.
