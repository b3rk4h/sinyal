import os
import requests
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

SYMBOLS = [
    'SOLUSDT', 'OPUSDT', 'DOGEUSDT', 'SUIUSDT', 'WIFUSDT', 'ETHUSDT',
    'BTCUSDT', 'AVAXUSDT', 'ARBUSDT', 'PEPEUSDT', 'LINKUSDT', 'SEIUSDT', 'XRPUSDT'
]

TP_LEVELS = [1.03, 1.06, 1.10]
SL_PCT = 0.975
TRAILING_SL_PCTS = [0.985, 0.99, 1.00]

open_positions = {}
daily_signals = []
performance_log = []
last_report_date = None
MODE = "agresif"  # agresif atau defensif


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Telegram error:", e)


def get_candles(symbol, interval='30m', limit=50):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
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
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
        return float(r.json()['price'])
    except:
        return None


def detect_signal(symbol):
    try:
        if symbol in open_positions:
            return

        data_30m = get_candles(symbol, '30m')
        data_1h = get_candles(symbol, '1h')

        ma5_30m = calculate_ma(data_30m, 5)
        ma20_30m = calculate_ma(data_30m, 20)
        ma5_1h = calculate_ma(data_1h, 5)
        ma20_1h = calculate_ma(data_1h, 20)

        close_30m = float(data_30m[-1][4])
        open_30m = float(data_30m[-1][1])
        volume_now = float(data_30m[-1][5])
        volume_ma10 = calculate_volume_ma(data_30m, 10)

        candle_body_pct = abs(close_30m - open_30m) / open_30m
        bullish = close_30m > open_30m and candle_body_pct > 0.002
        bearish = close_30m < open_30m and candle_body_pct > 0.002
        volume_ok = volume_now > volume_ma10 * (1.05 if MODE == "agresif" else 1.2)

        price = get_price(symbol)
        if not price:
            return

        qty = 1  # dummy qty untuk simulasi

        if ma5_30m > ma20_30m and ma5_1h > ma20_1h and bullish and volume_ok:
            open_positions[symbol] = {
                'entry': price,
                'tps_hit': [],
                'last_price': price,
                'sl_level': SL_PCT,
                'qty': qty,
                'side': 'LONG',
                'time': datetime.now()
            }
            daily_signals.append(symbol)
            send_telegram(f"🔔 LONG BUY {symbol}\nEntry: ${price:.2f}\nTP1-3: {[round(price * x, 2) for x in TP_LEVELS]}\nSL: {price * SL_PCT:.2f}")

        elif ma5_30m < ma20_30m and ma5_1h < ma20_1h and bearish and volume_ok:
            open_positions[symbol] = {
                'entry': price,
                'tps_hit': [],
                'last_price': price,
                'sl_level': 2 - SL_PCT,
                'qty': qty,
                'side': 'SHORT',
                'time': datetime.now()
            }
            daily_signals.append(symbol)
            send_telegram(f"🔔 SHORT SELL {symbol}\nEntry: ${price:.2f}\nTP1-3: {[round(price * (2-x), 2) for x in TP_LEVELS]}\nSL: {price * (2 - SL_PCT):.2f}")

    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")


def check_tp_sl():
    for symbol, pos in list(open_positions.items()):
        price = get_price(symbol)
        if not price:
            continue

        entry = pos['entry']
        pos['last_price'] = price

        if len(pos['tps_hit']) < len(TRAILING_SL_PCTS):
            pos['sl_level'] = TRAILING_SL_PCTS[len(pos['tps_hit'])] if pos['side'] == 'LONG' else 2 - TRAILING_SL_PCTS[len(pos['tps_hit'])]

        sl_hit = price <= entry * pos['sl_level'] if pos['side'] == 'LONG' else price >= entry * pos['sl_level']
        if sl_hit:
            send_telegram(f"❌ STOP LOSS HIT {symbol} ({pos['side']})\nEntry: ${entry:.2f}, Exit: ${price:.2f}\nLoss: {((price-entry)/entry)*100:.2f}%")
            performance_log.append(((price-entry)/entry)*100 * (1 if pos['side'] == 'LONG' else -1))
            del open_positions[symbol]
            continue

        for i, level in enumerate(TP_LEVELS):
            target = entry * level if pos['side'] == 'LONG' else entry * (2 - level)
            if i not in pos['tps_hit'] and ((price >= target and pos['side'] == 'LONG') or (price <= target and pos['side'] == 'SHORT')):
                send_telegram(f"✅ TP{i+1} HIT {symbol} ({pos['side']})\nTarget: ${target:.2f}")
                pos['tps_hit'].append(i)
                if i == len(TP_LEVELS) - 1:
                    performance_log.append(((target-entry)/entry)*100 * (1 if pos['side'] == 'LONG' else -1))
                    del open_positions[symbol]

        try:
            data_30m = get_candles(symbol, '30m')
            ma5_now = calculate_ma(data_30m, 5)
            ma20_now = calculate_ma(data_30m, 20)
            trend_reversal = (ma5_now < ma20_now if pos['side'] == 'LONG' else ma5_now > ma20_now)
            if trend_reversal:
                send_telegram(f"🔁 AUTO CLOSE {symbol} ({pos['side']}) Trend reversal\nExit: ${price:.2f}")
                performance_log.append(((price-entry)/entry)*100 * (1 if pos['side'] == 'LONG' else -1))
                del open_positions[symbol]
        except Exception as e:
            print(f"Trend reversal check error: {e}")


print("🚀 Bot sinyal (LONG & SHORT) dengan mode agresif/defensif siap jalan...")
while True:
    now = datetime.utcnow() + timedelta(hours=7)
    if now.minute % 30 == 0:
        for sym in SYMBOLS:
            detect_signal(sym)
        check_tp_sl()

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
