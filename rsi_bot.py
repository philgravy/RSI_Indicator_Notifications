# BTC multi-timeframe RSI alerts → Telegram
# Runs in GitHub Actions. On each run, loops ~6 minutes with 1m checks.
# Uses public endpoints and auto-falls back across exchanges (no Binance needed).

import os, time, requests
import ccxt
import pandas as pd
import pytz
from datetime import datetime
from ta.momentum import RSIIndicator

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")

SYMBOL_CANDIDATES = [
    ("bybit",     ["BTC/USDT"]),
    ("okx",       ["BTC/USDT", "BTC-USDT"]),
    ("kraken",    ["BTC/USDT", "XBT/USDT", "BTC/USD", "XBT/USD"]),
    ("binanceus", ["BTC/USDT", "BTC/USD"]),
    ("coinbase",  ["BTC/USD", "BTC/USDT"]),
]

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]
RSI_LEN = 14
LOW_THRESH  = 20.0
HIGH_THRESH = 80.0
CANDLES_LIMIT = 200

# Cooldowns are per timeframe & side (oversold/overbought)
COOLDOWN_SECONDS = 300  # 5 minutes

# This run’s overall time budget (GitHub step timeout safety)
RUN_SECONDS = 4 * 60  # ~6 minutes per workflow run
LOOP_SLEEP_SECONDS = 30  # sleep between TF sweeps

def now_utc():
    return datetime.now(pytz.UTC)

def now_utc_str():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S %Z")

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[{now_utc_str()}] (TELEGRAM DISABLED) {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        print(f"[{now_utc_str()}] Telegram send error: {e}")

def make_exchange(exchange_id: str):
    klass = getattr(ccxt, exchange_id)
    opts = {"enableRateLimit": True}
    if exchange_id == "okx":
        opts["options"] = {"defaultType": "spot"}
    return klass(opts)

def pick_working_market():
    last_err = None
    for ex_id, syms in SYMBOL_CANDIDATES:
        try:
            ex = make_exchange(ex_id)
        except Exception as e:
            last_err = e
            print(f"[{now_utc_str()}] Could not init {ex_id}: {e}")
            continue
        try:
            ex.load_markets()
        except Exception as e:
            print(f"[{now_utc_str()}] {ex_id} load_markets warn: {e}")
        for sym in syms:
            try:
                candles = ex.fetch_ohlcv(sym, timeframe="1m", limit=10)
                if candles and len(candles[0]) >= 5:
                    print(f"[{now_utc_str()}] Using {ex_id} / {sym}")
                    return ex, sym
            except Exception as e:
                last_err = e
                print(f"[{now_utc_str()}] {ex_id} {sym} failed: {e}")
                time.sleep(1)
        time.sleep(1)
    raise RuntimeError(f"No working exchange/symbol. Last error: {last_err}")

def fetch_rsi(ex, sym, timeframe: str):
    bars = ex.fetch_ohlcv(sym, timeframe=timeframe, limit=CANDLES_LIMIT)
    df = pd.DataFrame(bars, columns=["ts","open","high","low","close","vol"])
    rsi = RSIIndicator(close=df["close"], window=RSI_LEN).rsi()
    return float(rsi.iloc[-1]), int(df["ts"].iloc[-1])

def classify_zone(rsi: float):
    if rsi < LOW_THRESH:  return "below"
    if rsi > HIGH_THRESH: return "above"
    return "normal"

STARTUP_DM = os.environ.get("STARTUP_DM", "true").lower() == "true"

def main():
    start = time.time()
    if STARTUP_DM:
        send_telegram("✅ BTC RSI watcher (GitHub Actions) starting… selecting exchange.")
    exchange, symbol = pick_working_market()
    if STARTUP_DM:
        send_telegram(f"✅ Live on {exchange.id} • {symbol}\nTFs: {', '.join(TIMEFRAMES)}")

    # Per-run state (resets each GA invocation)
    state = {
        tf: {
            "last_zone": "normal",
            "last_ts": 0,
            "last_alert_low":  0.0,
            "last_alert_high": 0.0,
        } for tf in TIMEFRAMES
    }

    while (time.time() - start) < RUN_SECONDS:
        try:
            for tf in TIMEFRAMES:
                try:
                    rsi, ts = fetch_rsi(exchange, symbol, tf)
                except ccxt.BaseError as e:
                    print(f"[{now_utc_str()}] {exchange.id} {symbol} {tf} error: {e}")
                    time.sleep(10)
                    try:
                        exchange, symbol = pick_working_market()
                        send_telegram(f"🔁 Switched to {exchange.id} / {symbol}")
                    except Exception as ee:
                        print(f"[{now_utc_str()}] Switch failed: {ee}")
                    continue
                except Exception as e:
                    print(f"[{now_utc_str()}] Fetch error {tf}: {e}")
                    continue

                zone = classify_zone(rsi)
                tf_state = state[tf]
                new_candle = (ts != tf_state["last_ts"])
                crossed_below = new_candle and (tf_state["last_zone"] != "below" and zone == "below")
                crossed_above = new_candle and (tf_state["last_zone"] != "above" and zone == "above")

                now_t = time.time()
                can_low  = (now_t - tf_state["last_alert_low"])  >= COOLDOWN_SECONDS
                can_high = (now_t - tf_state["last_alert_high"]) >= COOLDOWN_SECONDS

                alert_text = None
                if zone == "below" and (crossed_below or can_low):
                    alert_text = (
                        f"⚠️ OVERSOLD on {tf}\n"
                        f"{symbol} RSI(14) = {rsi:.2f} (< {LOW_THRESH:.0f})\n"
                        f"Exchange: {exchange.id}\nUTC: {now_utc_str()}"
                    )
                    tf_state["last_alert_low"] = now_t
                elif zone == "above" and (crossed_above or can_high):
                    alert_text = (
                        f"🚀 OVERBOUGHT on {tf}\n"
                        f"{symbol} RSI(14) = {rsi:.2f} (> {HIGH_THRESH:.0f})\n"
                        f"Exchange: {exchange.id}\nUTC: {now_utc_str()}"
                    )
                    tf_state["last_alert_high"] = now_t

                print(f"[{now_utc_str()}] {exchange.id} {symbol} {tf} RSI={rsi:.2f} zone={zone} new={new_candle}")
                if alert_text:
                    send_telegram(alert_text)

                tf_state["last_zone"] = zone
                tf_state["last_ts"] = ts

            time.sleep(30 if (time.time() - start) < RUN_SECONDS else 1)

        except ccxt.BaseError as e:
            print(f"[{now_utc_str()}] Exchange error: {e}. Re-choosing in 20s…")
            time.sleep(20)
            try:
                exchange, symbol = pick_working_market()
              #  send_telegram(f"🔁 Switched to {exchange.id} / {symbol}")
            except Exception as ee:
                print(f"[{now_utc_str()}] Still no endpoint: {ee}. Waiting 30s…")
                time.sleep(30)
        except Exception as e:
            print(f"[{now_utc_str()}] Unexpected error: {e}. Retrying in 30s…")
            time.sleep(30)

if __name__ == "__main__":
    main()
