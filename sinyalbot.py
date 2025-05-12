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

        bullish_candle = close_30m > open_30m and (close_30m - open_30m) / open_30m > 0.002
        volume_ok = volume_now > volume_ma10 * 1.05  # Volume harus 5% lebih besar dari rata-rata

        if ma5_30m > ma20_30m and close_30m > ma5_30m and ma5_1h > ma20_1h and bullish_candle and volume_ok:
            if symbol not in open_positions:
                position_size = calculate_position_size(close_30m, close_30m * SL_PCT)
                open_positions[symbol] = {
                    'entry': close_30m,
                    'tps_hit': [],
                    'last_price': close_30m,
                    'sl_level': SL_PCT,
                    'time': datetime.now(),
                    'position_size': position_size
                }
                daily_signals.append(symbol)
                tp1 = close_30m * TP_LEVELS[0]
                tp2 = close_30m * TP_LEVELS[1]
                tp3 = close_30m * TP_LEVELS[2]
                sl = close_30m * SL_PCT

                message = (
                    f"🔔 BUY SIGNAL: {symbol}\n"
                    f"Entry: ${close_30m:.2f}\n"
                    f"TP1: {tp1:.2f} (+{(TP_LEVELS[0]-1)*100:.1f}%)\n"
                    f"TP2: {tp2:.2f} (+{(TP_LEVELS[1]-1)*100:.1f}%)\n"
                    f"TP3: {tp3:.2f} (+{(TP_LEVELS[2]-1)*100:.1f}%)\n"
                    f"SL: {sl:.2f} ({((SL_PCT-1)*100):.1f}%)\n"
                    f"TF: 30m + 1H Confirm ✅"
                )
                send_telegram(message)
    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")

def check_tp_sl():
    for symbol, pos in list(open_positions.items()):
        price = get_price(symbol)
        if not price:
            continue
        entry = pos['entry']
        pos['last_price'] = price

        # Update trailing stop loss jika harga bergerak
        stop_loss = adjust_stop_loss(symbol, entry, price)
        if price <= stop_loss:
            send_telegram(f"❌ STOP LOSS HIT: {symbol}\nEntry: ${entry:.4f} → SL: ${price:.4f}\nLoss: {((price-entry)/entry)*100:.2f}%")
            performance_log.append(((price-entry)/entry)*100)
            del open_positions[symbol]
            continue

        # Mengecek TP berdasarkan harga saat ini
        for i, level in enumerate(TP_LEVELS):
            if i in pos['tps_hit']:
                continue
            target_price = entry * level
            if price >= target_price:
                send_telegram(f"✅ TP{i+1} HIT: {symbol}\nEntry: ${entry:.4f} → Target: ${target_price:.4f}\nCuan: {((target_price-entry)/entry)*100:.2f}%")
                pos['tps_hit'].append(i)
                if i == len(TP_LEVELS) - 1:
                    performance_log.append(((target_price-entry)/entry)*100)
                    del open_positions[symbol]

        # Menutup posisi jika trend berbalik arah berdasarkan MA crossover
        ma5_30m = calculate_ma(get_candles(symbol, '30m'), 5)
        ma20_30m = calculate_ma(get_candles(symbol, '30m'), 20)
        if ma5_30m < ma20_30m:
            send_telegram(f"🚨 SELL SIGNAL: {symbol} - MA Crossover Detected. Exit position.")
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
