import time
import requests
import os
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from ta.trend import ADXIndicator, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

# === CONFIGURATION === #
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MODAL_TOTAL = 20  # modal awal total $20
LEVERAGE = 20

client = Client(API_KEY, API_SECRET)

# Tracking
active_signals = []
cooldowns_strong = {}
cooldowns_early = {}
early_candidates = {}

LOG_FILE = "log_sinyal.txt"


def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
        requests.post(url, data=data)
    except Exception as e:
        print(f"[ERROR] Telegram send: {e}")


def log_signal(message):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{message}\n")
    except Exception as e:
        print(f"[ERROR] Log write: {e}")


def get_all_usdt_futures_symbols():
    info = client.futures_exchange_info()
    return [
        s["symbol"]
        for s in info["symbols"]
        if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"
    ]


def filter_symbols(symbols):
    filtered = []
    for symbol in symbols:
        try:
            ticker = client.futures_ticker(symbol=symbol)
            if float(ticker["quoteVolume"]) > 5_000_000:
                filtered.append(symbol)
        except:
            continue
    return filtered


def fetch_klines(symbol, interval, limit=200):
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(
        klines,
        columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "_", "_", "_", "_", "_", "_"
        ],
    )
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def analyze(df):
    df["ma_fast"] = SMAIndicator(df["close"], 5).sma_indicator()
    df["ma_slow"] = SMAIndicator(df["close"], 20).sma_indicator()
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"]).adx()
    df["atr"] = AverageTrueRange(df["high"], df["low"], df["close"]).average_true_range()
    bb = BollingerBands(df["close"])
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["volume_spike"] = df["volume"] > df["volume"].rolling(20, min_periods=1).mean() * 1.5
    df["trend_up"] = df["ma_fast"] > df["ma_slow"]
    df["breakout_up"] = df["close"] > df["bb_upper"]
    df["breakout_down"] = df["close"] < df["bb_lower"]
    df["strong_adx"] = df["adx"] > 25
    df["volatility_ok"] = df["atr"] > df["atr"].rolling(20, min_periods=1).mean()
    return df


def get_fibo_status(df_4h, price):
    swing_high = df_4h["high"].max()
    swing_low = df_4h["low"].min()
    diff = swing_high - swing_low
    fib_levels = {
        "0.382": swing_high - diff * 0.382,
        "0.5": swing_high - diff * 0.5,
        "0.618": swing_high - diff * 0.618
    }
    closest_level = min(fib_levels, key=lambda k: abs(price - fib_levels[k]))
    level_price = fib_levels[closest_level]
    distance_pct = abs(price - level_price) / price * 100
    if distance_pct <= 0.2:
        return f"✅ Fibo Confirm ({closest_level})"
    else:
        return f"⚠️ Fibo Jauh (Nearest: {closest_level})"


def strength_label(is_long, trend_1h, trend_4h, adx_1h, adx_4h):
    if (trend_1h == is_long) and (trend_4h == is_long) and adx_1h > 25 and adx_4h > 25:
        return "✅ Potensi jadi Strong"
    else:
        return "⚠️ Lemah"


def check_signal(symbol):
    try:
        now = time.time()

        df_1m = analyze(fetch_klines(symbol, "1m"))
        df_5m = analyze(fetch_klines(symbol, "5m"))
        df_15m = analyze(fetch_klines(symbol, "15m"))
        df_1h = analyze(fetch_klines(symbol, "1h"))
        df_4h = analyze(fetch_klines(symbol, "4h"))

        trend_1h = df_1h.iloc[-1]["trend_up"]
        trend_4h = df_4h.iloc[-1]["trend_up"]
        adx_1h = df_1h.iloc[-1]["adx"]
        adx_4h = df_4h.iloc[-1]["adx"]

        # Risk adaptif
        if trend_1h and trend_4h and adx_1h > 30 and adx_4h > 30:
            risk_pct = 0.05
        elif trend_1h or trend_4h:
            risk_pct = 0.03
        else:
            risk_pct = 0.01

        price = df_1m.iloc[-1]["close"]
        atr_4h = df_4h.iloc[-1]["atr"]

        fibo_status = get_fibo_status(df_4h, price)

        # Kondisi STRONG
        cond_up = (
            df_1m.iloc[-1]["trend_up"] and
            df_5m.iloc[-1]["trend_up"] and
            df_15m.iloc[-1]["trend_up"] and
            df_1h.iloc[-1]["trend_up"] and
            df_1m.iloc[-1]["breakout_up"] and
            df_1m.iloc[-1]["volume_spike"] and
            df_1m.iloc[-1]["strong_adx"] and
            df_1m.iloc[-1]["volatility_ok"]
        )
        cond_down = (
            not df_1m.iloc[-1]["trend_up"] and
            not df_5m.iloc[-1]["trend_up"] and
            not df_15m.iloc[-1]["trend_up"] and
            not df_1h.iloc[-1]["trend_up"] and
            df_1m.iloc[-1]["breakout_down"] and
            df_1m.iloc[-1]["volume_spike"] and
            df_1m.iloc[-1]["strong_adx"] and
            df_1m.iloc[-1]["volatility_ok"]
        )

        # Kondisi EARLY
        early_long = (
            df_1m.iloc[-1]["trend_up"] and
            df_5m.iloc[-1]["trend_up"] and
            not df_15m.iloc[-1]["trend_up"] and
            df_1m.iloc[-1]["volume_spike"] and
            df_1m.iloc[-1]["adx"] > 20
        )
        early_short = (
            not df_1m.iloc[-1]["trend_up"] and
            not df_5m.iloc[-1]["trend_up"] and
            df_15m.iloc[-1]["trend_up"] and
            df_1m.iloc[-1]["volume_spike"] and
            df_1m.iloc[-1]["adx"] > 20
        )

        # Size & SL
        risk_dollar = MODAL_TOTAL * risk_pct
        sl_main = price - atr_4h if (cond_up or early_long) else price + atr_4h
        price_sl_diff = abs(price - sl_main)
        if price_sl_diff == 0:
            return
        size = round((risk_dollar / price_sl_diff) * LEVERAGE, 2)
        if size <= 0:
            return

        # === STRONG SIGNAL ===
        if cond_up or cond_down:
            if symbol in cooldowns_strong and now - cooldowns_strong[symbol] < 300:
                return
            cooldowns_strong[symbol] = now
            tp1 = price + atr_4h * 1.5 if cond_up else price - atr_4h * 1.5
            tp2 = price + atr_4h * 2.5 if cond_up else price - atr_4h * 2.5
            tp3 = price + atr_4h * 4 if cond_up else price - atr_4h * 4
            sl = price - atr_4h if cond_up else price + atr_4h
            direction = "LONG" if cond_up else "SHORT"
            emoji = "🚀" if cond_up else "🔻"

            send_telegram(f"{emoji} <b>STRONG {direction}</b> {symbol}\nPrice: {price:.3f}\nTP1: {tp1:.3f} | TP2: {tp2:.3f} | TP3: {tp3:.3f}\nSL: {sl:.3f}\nFibo: {fibo_status}")
            log_signal(f"{datetime.now()} | STRONG {direction} | {symbol} | Price: {price:.3f} | Size: {size} | {fibo_status} | TP1: {tp1:.3f} | TP2: {tp2:.3f} | TP3: {tp3:.3f} | SL: {sl:.3f}")

            active_signals.append({"symbol": symbol, "side": direction, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl, "notified_tp1": False, "notified_tp2": False, "notified_tp3": False})

            if symbol in early_candidates:
                del early_candidates[symbol]

        # === EARLY SIGNAL ===
        if early_long or early_short:
            if symbol in cooldowns_early and now - cooldowns_early[symbol] < 180:
                return
            cooldowns_early[symbol] = now
            direction = "EARLY LONG" if early_long else "EARLY SHORT"
            emoji = "🟡" if early_long else "🔸"
            label = strength_label(early_long, trend_1h, trend_4h, adx_1h, adx_4h)
            tp1 = price + atr_4h * 1.2 if early_long else price - atr_4h * 1.2
            tp2 = price + atr_4h * 2 if early_long else price - atr_4h * 2
            tp3 = price + atr_4h * 3.5 if early_long else price - atr_4h * 3.5
            sl = price - atr_4h if early_long else price + atr_4h

            send_telegram(f"{emoji} <b>{direction}</b> {symbol}\n{label}\nPrice: {price:.3f}\nTP1: {tp1:.3f} | TP2: {tp2:.3f} | TP3: {tp3:.3f}\nSL: {sl:.3f}\nFibo: {fibo_status}")
            log_signal(f"{datetime.now()} | {direction} | {symbol} | Price: {price:.3f} | {label} | {fibo_status} | TP1: {tp1:.3f} | TP2: {tp2:.3f} | TP3: {tp3:.3f} | SL: {sl:.3f}")

            early_candidates[symbol] = {"long": early_long}

    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")


def monitor_active_signals():
    try:
        for signal in active_signals:
            symbol = signal["symbol"]
            klines = client.futures_klines(symbol=symbol, interval="1m", limit=2)
            last_price = float(klines[-1][4])

            # LONG
            if signal["side"] == "LONG":
                if not signal["notified_tp1"] and last_price >= signal["tp1"]:
                    send_telegram(f"✅ {symbol} - TP1 TERCAPAI @ {last_price:.3f}")
                    log_signal(f"{datetime.now()} | {symbol} | TP1 HIT @ {last_price:.3f}")
                    signal["notified_tp1"] = True
                if not signal["notified_tp2"] and last_price >= signal["tp2"]:
                    send_telegram(f"🎯 {symbol} - TP2 TERCAPAI @ {last_price:.3f}")
                    log_signal(f"{datetime.now()} | {symbol} | TP2 HIT @ {last_price:.3f}")
                    signal["notified_tp2"] = True
                if not signal["notified_tp3"] and last_price >= signal["tp3"]:
                    send_telegram(f"🏆 {symbol} - TP3 TERCAPAI @ {last_price:.3f}")
                    log_signal(f"{datetime.now()} | {symbol} | TP3 HIT @ {last_price:.3f}")
                    signal["notified_tp3"] = True

            # SHORT
            elif signal["side"] == "SHORT":
                if not signal["notified_tp1"] and last_price <= signal["tp1"]:
                    send_telegram(f"✅ {symbol} - TP1 TERCAPAI @ {last_price:.3f}")
                    log_signal(f"{datetime.now()} | {symbol} | TP1 HIT @ {last_price:.3f}")
                    signal["notified_tp1"] = True
                if not signal["notified_tp2"] and last_price <= signal["tp2"]:
                    send_telegram(f"🎯 {symbol} - TP2 TERCAPAI @ {last_price:.3f}")
                    log_signal(f"{datetime.now()} | {symbol} | TP2 HIT @ {last_price:.3f}")
                    signal["notified_tp2"] = True
                if not signal["notified_tp3"] and last_price <= signal["tp3"]:
                    send_telegram(f"🏆 {symbol} - TP3 TERCAPAI @ {last_price:.3f}")
                    log_signal(f"{datetime.now()} | {symbol} | TP3 HIT @ {last_price:.3f}")
                    signal["notified_tp3"] = True

            # Stop Loss check
            if (signal["side"] == "LONG" and last_price <= signal["sl"]) or (signal["side"] == "SHORT" and last_price >= signal["sl"]):
                send_telegram(f"⛔ {symbol} - STOP LOSS @ {last_price:.3f}")
                log_signal(f"{datetime.now()} | {symbol} | STOP LOSS @ {last_price:.3f}")
                active_signals.remove(signal)

    except Exception as e:
        print(f"[ERROR] Monitor TP/SL: {e}")


# Main loop
while True:
    try:
        client.futures_ping()
        symbols = filter_symbols(get_all_usdt_futures_symbols())
        for sym in symbols[:5]:
            check_signal(sym)
            time.sleep(0.4)
        monitor_active_signals()
        time.sleep(60)
    except Exception as err:
        print(f"[ERROR] Main loop: {err}")
        time.sleep(60)
