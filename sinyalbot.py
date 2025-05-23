import os
import requests
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
BINANCE_API = 'https://api.binance.com'

SYMBOLS = ['SOLUSDT', 'OPUSDT', 'DOGEUSDT', 'SUIUSDT', 'WIFUSDT', 'BTCUSDT', 'ETHUSDT']
COOLDOWN = 300  # Waktu jeda antar sinyal per simbol (detik)

POSITION_STATE = {symbol: None for symbol in SYMBOLS}
LAST_SIGNAL_TIME = {symbol: 0 for symbol in SYMBOLS}

# -------------- Telegram Sender --------------
def send_telegram(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Telegram error:", e)

# -------------- Binance Data Fetcher --------------
def get_candles(symbol, interval='5m', limit=50):
    url = f'{BINANCE_API}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    r = requests.get(url)
    r.raise_for_status()
    return r.json()

# -------------- Indicator Calculations --------------
def calculate_ma(data, period):
    if len(data) < period:
        raise ValueError("Data tidak cukup untuk MA")
    closes = [float(c[4]) for c in data]
    return sum(closes[-period:]) / period

def is_marubozu(candle):
    open_price = float(candle[1])
    close_price = float(candle[4])
    high = float(candle[2])
    low = float(candle[3])
    body = abs(close_price - open_price)
    shadow = (high - low) - body
    return shadow <= body * 0.3

def detect_breakout(data):
    last = data[-1]
    prev = data[-2]
    volume_now = float(last[5])
    volume_prev = float(prev[5])
    price = float(last[4])
    resistance = max(float(c[2]) for c in data[-6:-1])
    support = min(float(c[3]) for c in data[-6:-1])

    if volume_now > 1.5 * volume_prev:
        if price > resistance and is_marubozu(last):
            return 'breakout'
        elif price < support and is_marubozu(last):
            return 'breakdown'
    return None

# -------------- TP/SL Strategy by Symbol --------------
def get_tp_sl(symbol, entry_price, direction):
    if symbol in ['BTCUSDT', 'ETHUSDT']:
        sl_pct, tp1_pct, tp2_pct = 0.01, 0.015, 0.03
    elif symbol in ['SOLUSDT', 'OPUSDT']:
        sl_pct, tp1_pct, tp2_pct = 0.015, 0.02, 0.04
    else:
        sl_pct, tp1_pct, tp2_pct = 0.02, 0.03, 0.05

    if direction == 'BUY':
        sl = entry_price * (1 - sl_pct)
        tp1 = entry_price * (1 + tp1_pct)
        tp2 = entry_price * (1 + tp2_pct)
    else:
        sl = entry_price * (1 + sl_pct)
        tp1 = entry_price * (1 - tp1_pct)
        tp2 = entry_price * (1 - tp2_pct)

    return sl, tp1, tp2

# -------------- Signal Detection Core --------------
def detect_signal(symbol):
    try:
        now = time.time()
        if now - LAST_SIGNAL_TIME[symbol] < COOLDOWN:
            return None

        data_5m = get_candles(symbol, '5m')
        data_15m = get_candles(symbol, '15m')
        price = float(data_5m[-1][4])
        ma5_5m = calculate_ma(data_5m, 5)
        ma20_5m = calculate_ma(data_5m, 20)
        ma5_15m = calculate_ma(data_15m, 5)
        ma20_15m = calculate_ma(data_15m, 20)

        breakout_signal = detect_breakout(data_5m)

        if ma5_5m > ma20_5m and ma5_15m > ma20_15m and price > ma5_5m and breakout_signal == 'breakout':
            LAST_SIGNAL_TIME[symbol] = now
            if POSITION_STATE[symbol] == 'SELL':
                send_telegram(f"❗️ SUGGEST EXIT: {symbol}\nPosisi sebelumnya: SELL\nSinyal berlawanan: BUY")
            POSITION_STATE[symbol] = 'BUY'
            sl, tp1, tp2 = get_tp_sl(symbol, price, 'BUY')
            return f"🔔 BUY SIGNAL: {symbol}\nEntry: {price:.4f}\nSL: {sl:.4f}\nTP1: {tp1:.4f}\nTP2: {tp2:.4f}"

        elif ma5_5m < ma20_5m and ma5_15m < ma20_15m and price < ma5_5m and breakout_signal == 'breakdown':
            LAST_SIGNAL_TIME[symbol] = now
            if POSITION_STATE[symbol] == 'BUY':
                send_telegram(f"❗️ SUGGEST EXIT: {symbol}\nPosisi sebelumnya: BUY\nSinyal berlawanan: SELL")
            POSITION_STATE[symbol] = 'SELL'
            sl, tp1, tp2 = get_tp_sl(symbol, price, 'SELL')
            return f"🔻 SELL SIGNAL: {symbol}\nEntry: {price:.4f}\nSL: {sl:.4f}\nTP1: {tp1:.4f}\nTP2: {tp2:.4f}"

    except Exception as e:
        print(f"Error on {symbol}: {e}")
    return None

# -------------- Main Runner --------------
print("[RUNNING] Smart Signal Bot with Confirmation & Adaptive TP/SL")
while True:
    for symbol in SYMBOLS:
        try:
            signal = detect_signal(symbol)
            if signal:
                send_telegram(signal)
        except Exception as err:
            print(f"[ERROR] {symbol}: {err}")
    time.sleep(60)
