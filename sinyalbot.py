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
SIGNAL_STRENGTH = {symbol: None for symbol in SYMBOLS}
LAST_TP_NOTIFICATION = {symbol: 0 for symbol in SYMBOLS}

# -------------- Telegram Sender --------------
def send_telegram(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Telegram error:", e)

# -------------- Binance Data Fetcher --------------
def get_candles(symbol, interval='1m', limit=50):
    url = f'{BINANCE_API}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    r = requests.get(url)
    r.raise_for_status()
    return r.json()

# -------------- Indicator Calculations --------------
def calculate_ma(data, period):
    closes = [float(c[4]) for c in data]
    return sum(closes[-period:]) / period

def calculate_rsi(data, period=14):
    closes = [float(c[4]) for c in data]
    deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    gains = sum([delta for delta in deltas[-period:] if delta > 0]) / period
    losses = abs(sum([delta for delta in deltas[-period:] if delta < 0]) / period)
    if losses == 0:
        return 100
    rs = gains / losses
    return 100 - (100 / (1 + rs))

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

        data_1m = get_candles(symbol, '1m')
        data_1h = get_candles(symbol, '1h', 50)
        price = float(data_1m[-1][4])

        ma5_1m = calculate_ma(data_1m, 5)
        ma20_1m = calculate_ma(data_1m, 20)
        ma5_1h = calculate_ma(data_1h, 5)
        ma20_1h = calculate_ma(data_1h, 20)
        rsi = calculate_rsi(data_1m)

        breakout_signal = detect_breakout(data_1m)

        signal_strength = 'normal'
        last_volume = float(data_1m[-1][5])
        avg_volume = sum(float(c[5]) for c in data_1m[-6:-1]) / 5
        if last_volume > 2 * avg_volume:
            signal_strength = 'strong'

        if ma5_1m > ma20_1m and ma5_1h > ma20_1h and price > ma5_1m and breakout_signal == 'breakout' and rsi < 70:
            LAST_SIGNAL_TIME[symbol] = now
            if POSITION_STATE[symbol] == 'SELL':
                send_telegram(f"🔁 REVERSAL: {symbol}\nSinyal berubah dari SELL ➜ BUY")
            POSITION_STATE[symbol] = 'BUY'
            SIGNAL_STRENGTH[symbol] = signal_strength
            sl, tp1, tp2 = get_tp_sl(symbol, price, 'BUY')
            return f"📈 BUY SIGNAL ({signal_strength.upper()}): {symbol}\nEntry: {price:.4f}\nSL: {sl:.4f}\nTP1: {tp1:.4f}\nTP2: {tp2:.4f}"

        elif ma5_1m < ma20_1m and ma5_1h < ma20_1h and price < ma5_1m and breakout_signal == 'breakdown' and rsi > 30:
            LAST_SIGNAL_TIME[symbol] = now
            if POSITION_STATE[symbol] == 'BUY':
                send_telegram(f"🔁 REVERSAL: {symbol}\nSinyal berubah dari BUY ➜ SELL")
            POSITION_STATE[symbol] = 'SELL'
            SIGNAL_STRENGTH[symbol] = signal_strength
            sl, tp1, tp2 = get_tp_sl(symbol, price, 'SELL')
            return f"📉 SELL SIGNAL ({signal_strength.upper()}): {symbol}\nEntry: {price:.4f}\nSL: {sl:.4f}\nTP1: {tp1:.4f}\nTP2: {tp2:.4f}"

        # Notifikasi jika sinyal kuat masih aktif
        if SIGNAL_STRENGTH[symbol] == 'strong' and now - LAST_SIGNAL_TIME[symbol] > 1800:
            send_telegram(f"📌 {symbol} sinyal KUAT masih berlaku. Tetap pertahankan posisi: {POSITION_STATE[symbol]}")
            LAST_SIGNAL_TIME[symbol] = now

    except Exception as e:
        print(f"Error on {symbol}: {e}")
    return None

# -------------- Main Runner --------------
print("[RUNNING] Smart Signal Bot with RSI, Breakout, MA, and Strong Signal Alerts")
while True:
    for symbol in SYMBOLS:
        try:
            signal = detect_signal(symbol)
            if signal:
                send_telegram(signal)
        except Exception as err:
            print(f"[ERROR] {symbol}: {err}")
    time.sleep(60)
