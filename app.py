import math
import os
import sqlite3
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="BTC 15m Signal Bot", page_icon="₿", layout="wide")

KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"
COINBASE_SPOT = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
DB_PATH = os.getenv("BOT_DB_PATH", "kalshi_btc_bot.db")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "btc-15m-signal-bot/0.1"})


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS prices (
        ts REAL PRIMARY KEY, price REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
        ticker TEXT PRIMARY KEY, created_ts REAL NOT NULL, p_yes REAL NOT NULL,
        spot REAL NOT NULL, strike REAL NOT NULL, seconds_left REAL NOT NULL,
        result TEXT, settled_ts REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS notified (
        ticker TEXT PRIMARY KEY, notified_ts REAL NOT NULL, side TEXT NOT NULL)""")
    conn.commit()
    return conn


def get_json(url, params=None):
    response = HTTP.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def get_btc_spot():
    data = get_json(COINBASE_SPOT)
    return float(data["data"]["amount"])


def parse_ts(market, key_ts, key_iso):
    value = market.get(key_ts)
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    value = market.get(key_iso)
    if value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp()
    return None


def get_active_market():
    data = get_json(
        f"{KALSHI_API}/markets",
        params={"series_ticker": "KXBTC15M", "status": "open", "limit": 100},
    )
    markets = data.get("markets", [])
    now = time.time()
    candidates = []
    for market in markets:
        close_ts = parse_ts(market, "close_ts", "close_time")
        strike = market.get("floor_strike")
        if close_ts and close_ts > now and strike is not None:
            candidates.append((close_ts, market))
    if not candidates:
        return None
    # The closest upcoming close is the active 15-minute contract.
    candidates.sort(key=lambda item: item[0])
    close_ts, market = candidates[0]
    market["_close_ts"] = close_ts
    return market


def save_price(conn, ts, price):
    conn.execute("INSERT OR REPLACE INTO prices(ts, price) VALUES(?, ?)", (ts, price))
    # Keep the latest 30 minutes only; historical forecast outcomes are stored separately.
    conn.execute("DELETE FROM prices WHERE ts < ?", (ts - 1800,))
    conn.commit()


def estimate_probability(conn, spot, strike, seconds_left):
    rows = conn.execute(
        "SELECT ts, price FROM prices WHERE ts >= ? ORDER BY ts ASC",
        (time.time() - 600,),
    ).fetchall()
    if len(rows) < 20:
        return None, len(rows), None

    returns, intervals = [], []
    for previous, current in zip(rows[:-1], rows[1:]):
        dt = current["ts"] - previous["ts"]
        if dt > 0 and previous["price"] > 0 and current["price"] > 0:
            returns.append(math.log(current["price"] / previous["price"]))
            intervals.append(dt)
    if len(returns) < 15:
        return None, len(rows), None

    # Estimate per-second drift and volatility from locally sampled BTC prices.
    total_seconds = sum(intervals)
    drift_per_second = sum(returns) / total_seconds if total_seconds else 0.0
    mean_rate = sum(r / dt for r, dt in zip(returns, intervals)) / len(returns)
    variance_rate = sum(((r / dt) - mean_rate) ** 2 for r, dt in zip(returns, intervals)) / max(1, len(returns) - 1)
    sigma_per_sqrt_second = math.sqrt(max(variance_rate, 0.0))

    # Volatility floor avoids spuriously extreme probabilities during quiet/short samples.
    sigma_per_sqrt_second = max(sigma_per_sqrt_second, 0.000025)
    horizon = max(float(seconds_left), 1.0)
    z = (math.log(spot / strike) + drift_per_second * horizon) / (sigma_per_sqrt_second * math.sqrt(horizon))
    raw_probability = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return min(0.995, max(0.005, raw_probability)), len(rows), sigma_per_sqrt_second


def calibrated_probability(conn, raw_p):
    """Small, regularized Platt-style calibration from previously settled forecasts."""
    rows = conn.execute(
        "SELECT p_yes, result FROM signals WHERE result IN ('yes','no') ORDER BY settled_ts DESC LIMIT 300"
    ).fetchall()
    if len(rows) < 40:
        return raw_p, len(rows), False

    # Gradient descent logistic calibration; strong shrinkage limits overfitting.
    xs = [math.log(max(0.01, min(0.99, r["p_yes"])) / (1 - max(0.01, min(0.99, r["p_yes"])))) for r in rows]
    ys = [1.0 if r["result"] == "yes" else 0.0 for r in rows]
    a, b = 0.7, 0.0
    lr = 0.015
    for _ in range(250):
        ga = sum((1 / (1 + math.exp(-max(-20, min(20, a*x+b)))) - y) * x for x, y in zip(xs, ys)) / len(xs) + 0.08 * (a - 1)
        gb = sum((1 / (1 + math.exp(-max(-20, min(20, a*x+b)))) - y) for x, y in zip(xs, ys)) / len(xs) + 0.08 * b
        a -= lr * ga
        b -= lr * gb
    x = math.log(max(0.01, min(0.99, raw_p)) / (1 - max(0.01, min(0.99, raw_p))))
    p = 1 / (1 + math.exp(-max(-20, min(20, a*x+b))))
    return min(0.995, max(0.005, p)), len(rows), True


def settle_old_signals(conn):
    rows = conn.execute("SELECT ticker FROM signals WHERE result IS NULL ORDER BY created_ts DESC LIMIT 12").fetchall()
    for row in rows:
        try:
            data = get_json(f"{KALSHI_API}/markets/{row['ticker']}")
            market = data.get("market", data)
            result = market.get("result")
            if result in ("yes", "no"):
                conn.execute(
                    "UPDATE signals SET result=?, settled_ts=? WHERE ticker=?",
                    (result, time.time(), row["ticker"]),
                )
        except requests.RequestException:
            continue
    conn.commit()


def market_price(market, field_dollars, field_cents):
    value = market.get(field_dollars)
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    value = market.get(field_cents)
    if value is not None:
        try:
            return float(value) / 100.0
        except (TypeError, ValueError):
            pass
    return None


def send_notification(topic, message):
    if not topic:
        return False, "No ntfy topic configured"
    # ntfy topics are public names; use a long random topic and never include private data.
    response = HTTP.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), timeout=10)
    response.raise_for_status()
    return True, "sent"


def main_panel():
    conn = db()
    now = time.time()
    try:
        spot = get_btc_spot()
        save_price(conn, now, spot)
        market = get_active_market()
        settle_old_signals(conn)
    except Exception as exc:
        st.error(f"Data connection problem: {exc}")
        st.info("The bot will retry on the next refresh. Check your internet connection and API availability.")
        conn.close()
        return

    if not market:
        st.warning("No active KXBTC15M market with a published strike was found. Waiting for the next window.")
        conn.close()
        return

    ticker = market.get("ticker", "unknown")
    strike = float(market["floor_strike"])
    close_ts = float(market["_close_ts"])
    seconds_left = max(0, close_ts - now)
    raw_p, samples, sigma = estimate_probability(conn, spot, strike, seconds_left)

    yes_ask = market_price(market, "yes_ask_dollars", "yes_ask")
    yes_bid = market_price(market, "yes_bid_dollars", "yes_bid")
    no_ask = market_price(market, "no_ask_dollars", "no_ask")
    if no_ask is None and yes_bid is not None:
        no_ask = 1.0 - yes_bid

    st.caption(f"Market: {ticker} · Data checked {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    c1, c2, c3 = st.columns(3)
    c1.metric("BTC spot (Coinbase)", f"${spot:,.2f}")
    c2.metric("Kalshi target / strike", f"${strike:,.2f}", f"{(spot/strike-1)*100:+.3f}% vs target")
    c3.metric("Time remaining", f"{int(seconds_left//60):02d}:{int(seconds_left%60):02d}")

    if raw_p is None:
        st.warning(f"Collecting price history: {samples} samples so far. Need at least 20 samples before producing a signal.")
        conn.close()
        return

    p_yes, calibration_n, calibrated = calibrated_probability(conn, raw_p)
    p_no = 1.0 - p_yes
    yes_edge = p_yes - yes_ask if yes_ask is not None else None
    no_edge = p_no - no_ask if no_ask is not None else None

    side, edge = "WAIT", None
    if yes_edge is not None and yes_edge >= 0.03 and p_yes >= 0.55:
        side, edge = "UP", yes_edge
    elif no_edge is not None and no_edge >= 0.03 and p_no >= 0.55:
        side, edge = "DOWN", no_edge

    a, b = st.columns(2)
    a.metric("Model probability BTC finishes UP", f"{p_yes*100:.1f}%")
    b.metric("Signal", side, f"Estimated edge {edge*100:.1f}¢/contract" if edge is not None else "No clear price advantage")
    st.progress(int(round(p_yes * 100)), text=f"UP {p_yes*100:.1f}% · DOWN {p_no*100:.1f}%")

    with st.expander("Model and market details", expanded=True):
        d1, d2, d3 = st.columns(3)
        d1.write(f"**YES ask:** {yes_ask*100:.1f}¢" if yes_ask is not None else "**YES ask:** unavailable")
        d2.write(f"**NO ask:** {no_ask*100:.1f}¢" if no_ask is not None else "**NO ask:** unavailable")
        d3.write(f"**Volatility estimate:** {sigma:.7f}" if sigma is not None else "**Volatility:** unavailable")
        st.write(f"Historical settled forecasts used for calibration: **{calibration_n}**")
        st.write("Calibration status: **active**" if calibrated else "Calibration status: **warming up; raw model only**")
        st.write("Price samples in rolling window: **{}**".format(samples))
        st.caption("Probability is a statistical estimate, not a verified win rate. Coinbase spot is only a proxy for Kalshi's CF Benchmarks settlement index.")

    # Save one forecast per market. Later outcome is used to calibrate future forecasts.
    conn.execute(
        "INSERT OR IGNORE INTO signals(ticker, created_ts, p_yes, spot, strike, seconds_left) VALUES(?,?,?,?,?,?)",
        (ticker, now, p_yes, spot, strike, seconds_left),
    )
    conn.commit()

    if side in ("UP", "DOWN"):
        already = conn.execute("SELECT ticker FROM notified WHERE ticker=?", (ticker,)).fetchone()
        if not already:
            try:
                topic = st.secrets["NTFY_TOPIC"]
            except Exception:
                topic = os.getenv("NTFY_TOPIC", "")
            message = f"BTC 15m {side} signal | model UP probability {p_yes:.1%} | edge {edge:.1%} | BTC ${spot:,.2f} | target ${strike:,.2f} | {int(seconds_left)}s left | {ticker}"
            try:
                if topic:
                    send_notification(topic, message)
                    conn.execute("INSERT OR REPLACE INTO notified(ticker, notified_ts, side) VALUES(?,?,?)", (ticker, now, side))
                    conn.commit()
                    st.toast("Phone notification sent")
            except requests.RequestException as exc:
                st.warning(f"Could not send phone notification: {exc}")

    history = conn.execute("SELECT ticker, created_ts, p_yes, result FROM signals ORDER BY created_ts DESC LIMIT 20").fetchall()
    if history:
        st.subheader("Recent recorded forecasts")
        df = pd.DataFrame([dict(r) for r in history])
        df["created"] = pd.to_datetime(df.pop("created_ts"), unit="s", utc=True).dt.strftime("%m-%d %H:%M UTC")
        df["UP probability"] = (df.pop("p_yes") * 100).round(1).astype(str) + "%"
        df["Outcome"] = df.pop("result").fillna("pending")
        st.dataframe(df[["created", "ticker", "UP probability", "Outcome"]], use_container_width=True, hide_index=True)

    st.caption("Signals are research alerts only. They do not place orders. Do not treat the model's probability as a guarantee or use it until it has been independently backtested.")
    conn.close()


st.title("₿ Kalshi BTC 15-minute signal bot")
st.write("Read-only market monitor · adaptive forecast calibration · no automated trading")

try:
    refresh_seconds = int(st.sidebar.selectbox("Refresh interval", [5, 10, 15, 30], index=1))
except Exception:
    refresh_seconds = 10

st.sidebar.markdown("### Phone notifications")
st.sidebar.write("Set an ntfy topic in Streamlit Secrets to receive iPhone alerts. See setup instructions in README.")
st.sidebar.markdown("### Safety")
st.sidebar.write("This app uses public read-only market data. Never put Kalshi private keys or account credentials into this starter project.")

@st.fragment(run_every=f"{refresh_seconds}s")
def live_area():
    main_panel()

live_area()
