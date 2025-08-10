# bot_sinyal_retrace_v2.py
# Anti-FOMO retrace signal bot (Binance Futures + Telegram)
# Features:
# - Trend filter on 1H (EMA50/EMA200)
# - Entry on 15m retrace: EMA20 touch + EMA50 hold + RSI/StochRSI confirmation
# - Entry BAND using EMA20 + Fibonacci(0.5/0.618); pending limit (midpoint) optional
# - Validity window + slip tolerance (late entries still safe)
# - Dynamic SL/TP (percent-based) suitable for high leverage (e.g., 75x)
# - Optional: Only LONG in uptrend, or include SHORT in downtrend
# - Optional: Set leverage and margin type (ISOLATED) via API
# - Re-alert on retest after expiry
#
# Dependencies:
#   pip install python-binance pandas numpy ta python-dotenv requests
#
# DISCLAIMER: This script sends SIGNALS only. Trading is risky; use at your own discretion.

import os
import time
import math
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import HistoricalKlinesType
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator, StochRSIIndicator

# ===================== ENV & SETUP =====================
load_dotenv()
API_KEY = os.getenv("API_KEY", "")
API_SECRET = os.getenv("API_SECRET", "")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

if not API_KEY or not API_SECRET:
    print("[WARN] Missing BINANCE API keys in .env")
if not TG_TOKEN or not TG_CHAT:
    print("[WARN] Telegram not configured; messages will print to console only.")

# ===================== CONFIG =====================
PAIRS = [
    "LTCUSDT","BTCUSDT","ETHUSDT","LINKUSDT","ETCUSDT",
    "SUIUSDT","IOTAUSDT","BCHUSDT","XLMUSDT","DASHUSDT","BLURUSDT"
]

# Timeframes
TF_TREND = "1h"    # trend filter timeframe
TF_ENTRY = "15m"   # entry timing timeframe

SCAN_EVERY_SEC = 30            # loop pacing
CANDLES_FETCH_TREND = 300
CANDLES_FETCH_ENTRY = 400

# Modes
CONFIRM_ON_CLOSE = True        # True: signal after 15m candle CLOSE; False: early ping (more risk)
SIDE_LONG_ONLY = False          # True: LONG only (with uptrend); False: also SHORT in downtrend

# Trend (1H)
EMA_FAST_TREND = 50
EMA_SLOW_TREND = 200

# Entry (15m)
EMA_TOUCH = 20                 # must touch/near EMA20
EMA_CONFIRM = 50               # must hold above/below EMA50
RSI_MIN_LONG, RSI_MAX_LONG = 45, 60
RSI_MIN_SHORT, RSI_MAX_SHORT = 40, 55
STOCHK_LOW_LONG, STOCHK_HIGH_LONG = 0.25, 0.65
STOCHK_LOW_SHORT, STOCHK_HIGH_SHORT = 0.35, 0.75

# Entry Band + Tolerances
USE_FIB = True
FIB_LOOKBACK = 20              # bars to compute swing high/low
FIB_LEVELS = (0.5, 0.618)
ENTRY_WINDOW_MIN = 30          # signal validity window
SLIP_TOL_PCT = 0.0025          # 0.25% ok-to-chase outside band
PENDING_LIMIT = True           # suggest midpoint limit
LIMIT_BUFFER_PCT = 0.0008      # +/- buffer around band for limit

# SL/TP (suited for high leverage)
SL_PCT  = 0.006   # 0.6%
TP1_PCT = 0.012   # 1.2%
TP2_PCT = 0.020   # 2.0%

# Account controls (optional)
SET_ISOLATED = True            # set isolated margin type
SET_LEVERAGE = 75              # set leverage per symbol (use with caution; must be supported by symbol)
LEVERAGE_ON_STARTUP = True     # apply on bot start

# Re-alert control
REALERT_COOLDOWN_MIN = 20      # after sending a signal, avoid spamming within N minutes
# ==================================================

def tg(msg: str):
    """Send Telegram message (fallback to print)."""
    if not TG_TOKEN or not TG_CHAT:
        print("[TG]", msg)
        return
    try:
        requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            params={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("TG error:", e, "\n", msg)

def binance_client():
    return Client(API_KEY, API_SECRET)

def get_klines_df(client, symbol, interval, limit):
    """Fetch closed candles for given symbol/timeframe from Binance Futures."""
    # Use the dedicated futures endpoint to avoid extra params errors (-1104)
    kl = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    cols = ["t","o","h","l","c","v","ct","qv","ntr","tbbav","tbqv","ig"]
    df = pd.DataFrame(kl, columns=cols)
    for col in ["o","h","l","c","v"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df[["t","o","h","l","c","v"]]

def add_indicators(df, ema_fast=20, ema_slow=50):
    df = df.copy()
    df["ema_fast"] = EMAIndicator(close=df["c"], window=ema_fast).ema_indicator()
    df["ema_slow"] = EMAIndicator(close=df["c"], window=ema_slow).ema_indicator()
    df["rsi"] = RSIIndicator(close=df["c"], window=14).rsi()
    st = StochRSIIndicator(close=df["c"], window=14, smooth1=3, smooth2=3)
    df["stoch_k"] = st.stochrsi_k() / 100.0  # 0..1
    return df

def ema_uptrend(df_1h):
    ema_fast = EMAIndicator(close=df_1h["c"], window=EMA_FAST_TREND).ema_indicator()
    ema_slow = EMAIndicator(close=df_1h["c"], window=EMA_SLOW_TREND).ema_indicator()
    return float(ema_fast.iloc[-1]) > float(ema_slow.iloc[-1])

def ema_downtrend(df_1h):
    ema_fast = EMAIndicator(close=df_1h["c"], window=EMA_FAST_TREND).ema_indicator()
    ema_slow = EMAIndicator(close=df_1h["c"], window=EMA_SLOW_TREND).ema_indicator()
    return float(ema_fast.iloc[-1]) < float(ema_slow.iloc[-1])

def last_candle(df):
    c = df.iloc[-1]
    return float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])

def fmt(n):
    return f"{n:.6f}".rstrip("0").rstrip(".")

def build_sl_tp(side, price):
    if side == "LONG":
        sl  = price * (1 - SL_PCT)
        tp1 = price * (1 + TP1_PCT)
        tp2 = price * (1 + TP2_PCT)
    else:
        sl  = price * (1 + SL_PCT)
        tp1 = price * (1 - TP1_PCT)
        tp2 = price * (1 - TP2_PCT)
    return sl, tp1, tp2

def compute_fib_band(df15, ema20_val):
    """Compute an entry band using EMA20 and Fibo 0.5/0.618 of recent swing (lookback FIB_LOOKBACK)."""
    if not USE_FIB:
        return ema20_val * (1 - LIMIT_BUFFER_PCT), ema20_val * (1 + LIMIT_BUFFER_PCT)

    hh = df15["h"].iloc[-FIB_LOOKBACK-1:-1].max()
    ll = df15["l"].iloc[-FIB_LOOKBACK-1:-1].min()
    fib50  = ll + (hh - ll) * FIB_LEVELS[0]
    fib618 = ll + (hh - ll) * FIB_LEVELS[1]

    # Use medians to avoid extremes; create a tight band around EMA20 & fibs
    low  = np.median([ema20_val*0.999, fib50, fib618]) * (1 - LIMIT_BUFFER_PCT)
    high = np.median([ema20_val*1.001, fib50, fib618]) * (1 + LIMIT_BUFFER_PCT)
    if low > high:  # safety
        low, high = high, low
    return float(low), float(high)

def apply_account_settings_once(client):
    """Optionally set ISOLATED margin & leverage on startup for all PAIRS."""
    if not LEVERAGE_ON_STARTUP:
        return
    for sym in PAIRS:
        try:
            if SET_ISOLATED:
                #  ISOLATED or CROSSED
                client.futures_change_margin_type(symbol=sym, marginType="ISOLATED")
            if SET_LEVERAGE:
                client.futures_change_leverage(symbol=sym, leverage=SET_LEVERAGE)
            print(f"[ACC] {sym}: ISOLATED={SET_ISOLATED} LEV={SET_LEVERAGE}")
        except Exception as e:
            print(f"[ACC] Skip {sym}: {e}")

# Memory for anti-spam & retest
last_signal_time = {}     # {symbol: timestamp}
last_band = {}            # {symbol: (low, high, expiry_ts)}
last_side = {}            # {symbol: "LONG"/"SHORT"}

def should_realert(sym):
    """Check cooldown to avoid spamming signals."""
    now = time.time()
    ts = last_signal_time.get(sym, 0)
    return (now - ts) > (REALERT_COOLDOWN_MIN * 60)

def mark_signal(sym, band_low, band_high, expiry_ts, side):
    last_signal_time[sym] = time.time()
    last_band[sym] = (band_low, band_high, expiry_ts)
    last_side[sym] = side

def maybe_realert(sym, current_price):
    """If band expired, re-alert when price revisits band after cooldown."""
    if sym not in last_band:
        return False
    band_low, band_high, expiry_ts = last_band[sym]
    now = time.time()
    if now <= expiry_ts:
        return False  # still valid; no need
    if not should_realert(sym):
        return False
    if band_low <= current_price <= band_high:
        return True
    return False

def signal_text(sym, side, entry_low, entry_high, price_ref, rsi, k):
    now_utc = datetime.utcnow()
    expiry = now_utc + pd.Timedelta(minutes=ENTRY_WINDOW_MIN)
    sl, tp1, tp2 = build_sl_tp(side, price_ref)
    mode = "Confirm on close" if CONFIRM_ON_CLOSE else "Early ping"
    return (
        f"{'🟢' if side=='LONG' else '🔴'} <b>RETRACE {side}</b> {sym}\n"
        f"Entry band: {fmt(entry_low)} – {fmt(entry_high)} "
        f"(valid s/d {expiry.strftime('%H:%M UTC')})\n"
        f"Slip OK ≤ {SLIP_TOL_PCT*100:.2f}% di luar band\n"
        f"RSI {rsi:.1f} | StochK {k:.2f}\n"
        f"SL {fmt(sl)} | TP1 {fmt(tp1)} | TP2 {fmt(tp2)}\n"
        f"Mode: {mode}\n"
        f"Tips: hitung SL/TP dari <i>harga fill</i> kamu."
    ), (time.time() + ENTRY_WINDOW_MIN * 60)

def check_symbol(client, sym):
    # 1) Trend filter (1H)
    df1h = get_klines_df(client, sym, TF_TREND, CANDLES_FETCH_TREND)
    up = ema_uptrend(df1h)
    down = ema_downtrend(df1h)

    if SIDE_LONG_ONLY:
        if not up:
            return
    else:
        if not (up or down):
            return

    # 2) Entry timing (15m)
    df15 = get_klines_df(client, sym, TF_ENTRY, CANDLES_FETCH_ENTRY)
    df15 = add_indicators(df15, ema_fast=EMA_TOUCH, ema_slow=EMA_CONFIRM)
    o,h,l,c = last_candle(df15)

    ema_t = float(df15["ema_fast"].iloc[-1])
    ema_c = float(df15["ema_slow"].iloc[-1])
    rsi = float(df15["rsi"].iloc[-1])
    k = float(df15["stoch_k"].iloc[-1])

    # Compute entry band
    band_low, band_high = compute_fib_band(df15, ema_t)
    price_ref = c if CONFIRM_ON_CLOSE else o

    # Conditions
    if up:
        # Need touch EMA20 & hold above EMA50
        touched = (l <= ema_t <= h) or (abs(c-ema_t)/ema_t < 0.0015)
        above50 = c > ema_c
        ok_rsi = (RSI_MIN_LONG <= rsi <= RSI_MAX_LONG)
        ok_k = (STOCHK_LOW_LONG <= k <= STOCHK_HIGH_LONG)

        if touched and above50 and ok_rsi and ok_k:
            # Late entry tolerance
            slip_ok = (price_ref <= band_high*(1+SLIP_TOL_PCT))
            if (band_low <= price_ref <= band_high) or slip_ok:
                txt, expiry_ts = signal_text(sym, "LONG", band_low, band_high, price_ref, rsi, k)
                if should_realert(sym):
                    tg(txt)
                    print(txt)
                    mark_signal(sym, band_low, band_high, expiry_ts, "LONG")
                return
            # maybe re-alert on retest after expiry
            if maybe_realert(sym, price_ref):
                txt, expiry_ts = signal_text(sym, "LONG", band_low, band_high, price_ref, rsi, k)
                tg(txt)
                print(txt)
                mark_signal(sym, band_low, band_high, expiry_ts, "LONG")
                return

    if (not SIDE_LONG_ONLY) and down:
        touched = (l <= ema_t <= h) or (abs(c-ema_t)/ema_t < 0.0015)
        below50 = c < ema_c
        ok_rsi = (RSI_MIN_SHORT <= rsi <= RSI_MAX_SHORT)
        ok_k = (STOCHK_LOW_SHORT <= k <= STOCHK_HIGH_SHORT)

        if touched and below50 and ok_rsi and ok_k:
            slip_ok = (price_ref >= band_low*(1-SLIP_TOL_PCT))
            if (band_low <= price_ref <= band_high) or slip_ok:
                txt, expiry_ts = signal_text(sym, "SHORT", band_low, band_high, price_ref, rsi, k)
                if should_realert(sym):
                    tg(txt)
                    print(txt)
                    mark_signal(sym, band_low, band_high, expiry_ts, "SHORT")
                return
            if maybe_realert(sym, price_ref):
                txt, expiry_ts = signal_text(sym, "SHORT", band_low, band_high, price_ref, rsi, k)
                tg(txt)
                print(txt)
                mark_signal(sym, band_low, band_high, expiry_ts, "SHORT")
                return

def main():
    client = binance_client()
    print("Bot sinyal retrace anti-FOMO berjalan…")
    if LEVERAGE_ON_STARTUP:
        apply_account_settings_once(client)

    while True:
        loop_start = time.time()
        try:
            for sym in PAIRS:
                try:
                    check_symbol(client, sym)
                except Exception as e:
                    print(f"[{sym}] Error:", e)
            # pacing
            dt = time.time() - loop_start
            time.sleep(max(5, SCAN_EVERY_SEC - dt))
        except KeyboardInterrupt:
            print("Stop.")
            break
        except Exception as e:
            print("[LOOP] Error:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
