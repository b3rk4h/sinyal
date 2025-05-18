import os
import requests
import time
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
BINANCE_API = 'https://api.binance.com'

SYMBOLS = [
    'SOLUSDT', 'OPUSDT', 'DOGEUSDT', 'SUIUSDT', 'WIFUSDT',
    'ETHUSDT', 'BTCUSDT', 'AVAXUSDT', 'ARBUSDT', 'PEPEUSDT',
    'LINKUSDT', 'SEIUSDT', 'XRPUSDT'
]

TP_LEVELS = [1.03, 1.06, 1.10]
SL_PCT = 0.975
TRAILING_SL_PCTS = [0.99, 1.00, 1.03]
EXIT_PROFIT_PCT = 1.015
RISK_PCT_PER_TRADE = 0.01  # Risiko 1% per trade

open_positions = {}
daily_signals = []
performance_log = []
last_report_date = None
account_balance = 10  # Saldo akun untuk contoh, sesuaikan dengan nilai nyata

def send_telegram(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Telegram error:", e)

def get_candles(symbol, interval='30m', limit=50):
    url = f'{BINANCE_API}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    r = requests.get(url)
    return r.json()

def calculate_ma(data, period):
    closes = [float(c[4]) for c in data]
    return sum(closes[-period:]) / period

def calculate_volume_ma(data, period):
    volumes = [float(c[5]) for c in data]
    return sum(volumes[-period:]) / period

def get_price(symbol):
    try:
        r = requests.get(f'{BINANCE_API}/api/v3/ticker/price?symbol={symbol}')
        return float(r.json()['price'])
    except:
        return None

def calculate_position_size(entry_price, stop_loss_price):
    """Menghitung ukuran posisi berdasarkan saldo dan risiko"""
    risk_amount = account_balance * RISK_PCT_PER_TRADE
    position_size = risk_amount / (entry_price - stop_loss_price)
    return position_size

def adjust_stop_loss(symbol, entry_price, current_price, trailing_stop_pct=0.02):
    """Mengatur trailing stop loss berdasarkan harga saat ini dan persentase"""
    stop_loss = entry_price * (1 - trailing_stop_pct)
    if current_price > stop_loss:
        stop_loss = current_price * (1 - trailing_stop_pct)
    return stop_loss

def take_profit(symbol, entry_price, current_price, tp_pct=0.05):
    """Mengambil keuntungan jika harga mencapai target tertentu"""
    tp_price = entry_price * (1 + tp_pct)
    if current_price >= tp_price:
        send_telegram(f"✅ Take Profit tercapai untuk {symbol}: ${current_price:.2f}")
        close_position(symbol)

def close_position(symbol):
    """Menutup posisi"""
    if symbol in open_positions:
        del open_positions[symbol]
        send_telegram(f"Posisi {symbol} telah ditutup")

def detect_signal(symbol):
    try:
        data_30m = get_candles(symbol, '30m')
        data_1h = get_candles(symbol, '1h')

        ma5_30m = calculate_ma(data_30m, 5)
        ma20_30m = calculate_ma(data_30m, 20)
        close_30m = float(data_30m[-1][4])
        open_30m = float(data_30m[-1][1])

        ma5_1h = calculate_ma(data_1h, 5)
        ma20_1h = calculate_ma(data_1h, 20)

        volume_now = float(data_30m[-1][5])
        volume_ma10 = calculate_volume_ma(data_30m, 10)

        bullish = close_30m > open_30m and (close_30m - open_30m) / open_30m > 0.002
        bearish = close_30m < open_30m and (open_30m - close_30m) / open_30m > 0.002
        volume_ok = volume_now > volume_ma10 * 1.05

        entry_price = close_30m
        sl_price = entry_price * SL_PCT
        position_size = calculate_position_size(entry_price, sl_price)

        # LONG SIGNAL
        if ma5_30m > ma20_30m and ma5_1h > ma20_1h and bullish and volume_ok:
            if symbol not in open_positions:
                open_positions[symbol] = {
                    'entry': entry_price,
                    'tps_hit': [],
                    'last_price': entry_price,
                    'sl_level': SL_PCT,
                    'time': datetime.now(),
                    'position_size': position_size,
                    'direction': 'long'
                }
                daily_signals.append(symbol)
                send_telegram(
                    f"🟢 LONG SIGNAL: {symbol}\n"
                    f"Entry: ${entry_price:.2f}\n"
                    f"TP1: {entry_price * TP_LEVELS[0]:.2f}\n"
                    f"TP2: {entry_price * TP_LEVELS[1]:.2f}\n"
                    f"TP3: {entry_price * TP_LEVELS[2]:.2f}\n"
                    f"SL: {sl_price:.2f}\nTF: 30m + 1H Confirm ✅"
                )

        # SHORT SIGNAL
        elif ma5_30m < ma20_30m and ma5_1h < ma20_1h and bearish and volume_ok:
            if symbol not in open_positions:
                open_positions[symbol] = {
                    'entry': entry_price,
                    'tps_hit': [],
                    'last_price': entry_price,
                    'sl_level': SL_PCT,
                    'time': datetime.now(),
                    'position_size': position_size,
                    'direction': 'short'
                }
                daily_signals.append(symbol)
                send_telegram(
                    f"🔻 SHORT SIGNAL: {symbol}\n"
                    f"Entry: ${entry_price:.2f}\n"
                    f"TP1: {entry_price * (2 - TP_LEVELS[0]):.2f}\n"
                    f"TP2: {entry_price * (2 - TP_LEVELS[1]):.2f}\n"
                    f"TP3: {entry_price * (2 - TP_LEVELS[2]):.2f}\n"
                    f"SL: {entry_price / SL_PCT:.2f}\nTF: 30m + 1H Confirm ✅"
                )
    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")

def check_tp_sl():
    for symbol, pos in list(open_positions.items()):
        price = get_price(symbol)
        if not price:
            continue

        entry = pos['entry']
        pos['last_price'] = price
        direction = pos.get('direction', 'long')
        sl_price = 0

        if direction == 'long':
            # Hit SL?
            sl_price = entry * SL_PCT
            if price <= sl_price:
                send_telegram(f"❌ SL HIT: {symbol} (LONG)\nEntry: ${entry:.2f} → SL: ${price:.2f}")
                performance_log.append(((price-entry)/entry)*100)
                del open_positions[symbol]
                continue
            # TP check
            for i, level in enumerate(TP_LEVELS):
                if i in pos['tps_hit']:
                    continue
                target = entry * level
                if price >= target:
                    send_telegram(f"✅ TP{i+1} HIT: {symbol} (LONG)\nCuan: {(level-1)*100:.2f}%")
                    pos['tps_hit'].append(i)
                    if i == len(TP_LEVELS) - 1:
                        performance_log.append(((price-entry)/entry)*100)
                        del open_positions[symbol]

        elif direction == 'short':
            sl_price = entry / SL_PCT
            if price >= sl_price:
                send_telegram(f"❌ SL HIT: {symbol} (SHORT)\nEntry: ${entry:.2f} → SL: ${price:.2f}")
                performance_log.append(((entry-price)/entry)*100)
                del open_positions[symbol]
                continue
            # TP check (kebalikan)
            for i, level in enumerate(TP_LEVELS):
                if i in pos['tps_hit']:
                    continue
                target = entry * (2 - level)
                if price <= target:
                    send_telegram(f"✅ TP{i+1} HIT: {symbol} (SHORT)\nCuan: {(1 - (target/entry))*100:.2f}%")
                    pos['tps_hit'].append(i)
                    if i == len(TP_LEVELS) - 1:
                        performance_log.append(((entry-price)/entry)*100)
                        del open_positions[symbol]

        # Exit kalau MA cross berbalik
        ma5_30m = calculate_ma(get_candles(symbol, '30m'), 5)
        ma20_30m = calculate_ma(get_candles(symbol, '30m'), 20)

        if (direction == 'long' and ma5_30m < ma20_30m) or (direction == 'short' and ma5_30m > ma20_30m):
            send_telegram(f"🚨 TREND REVERSE: {symbol} ({direction.upper()})\nExit posisi karena MA cross.")
            close_position(symbol)

print("🚀 Bot sinyal siap jalan...")
while True:
    now = datetime.utcnow() + timedelta(hours=7)  # WIB timezone

    if now.minute % 30 == 0:
        for sym in SYMBOLS:
            detect_signal(sym)

    check_tp_sl()

    # Kirim laporan harian hanya 1x per hari pukul 00:00 WIB
    if now.hour == 0 and now.minute == 0 and (last_report_date != now.date()):
        total = len(performance_log)
        win = len([x for x in performance_log if x > 0])
        loss = total - win
        avg_gain = sum(performance_log) / total if total else 0

        summary = f"📊 Laporan Harian ({now.strftime('%Y-%m-%d')})\n"
        summary += f"Sinyal muncul: {len(daily_signals)}\n"
        summary += f"Open Posisi: {len(open_positions)}\n"
        summary += f"TP/SL Tercapai: {total}\n"
        summary += f"Win: {win}, Loss: {loss}\n"
        summary += f"Avg Gain/Loss: {avg_gain:.2f}%"

        if not daily_signals:
            summary += "\n⚠️ Tidak ada sinyal valid hari ini."

        send_telegram(summary)
        last_report_date = now.date()
        daily_signals.clear()
        performance_log.clear()

    time.sleep(60)
