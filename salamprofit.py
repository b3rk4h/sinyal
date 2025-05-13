import os
import requests
import time
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

SYMBOLS = [
    'SOLUSDT', 'OPUSDT', 'DOGEUSDT', 'SUIUSDT', 'WIFUSDT', 'ETHUSDT',
    'BTCUSDT', 'AVAXUSDT', 'ARBUSDT', 'PEPEUSDT', 'LINKUSDT', 'SEIUSDT', 'XRPUSDT'
]

TP_LEVELS = [1.03, 1.06, 1.10]
SL_PCT = 0.975
TRAILING_SL_PCTS = [0.99, 1.00, 1.03]

open_positions = {}
daily_signals = []
performance_log = []
last_report_date = None


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


def get_usdt_balance():
    try:
        balance = client.futures_account_balance()
        for b in balance:
            if b['asset'] == 'USDT':
                return float(b['balance'])
        return 0
    except:
        return 0


def place_order(symbol, quantity):
    try:
        client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=quantity
        )
        return True
    except Exception as e:
        print("Order error:", e)
        return False


def detect_signal(symbol):
    try:
        if open_positions:
            return

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
        volume_ok = volume_now > volume_ma10 * 1.05

        if ma5_30m > ma20_30m and close_30m > ma5_30m and ma5_1h > ma20_1h and bullish_candle and volume_ok:
            if symbol not in open_positions:
                balance = get_usdt_balance()
                if balance < 5:
                    return
                price = get_price(symbol)
                if not price:
                    return
                qty = round(balance / price, 3)
                if not place_order(symbol, qty):
                    return

                open_positions[symbol] = {
                    'entry': price,
                    'tps_hit': [],
                    'last_price': price,
                    'sl_level': SL_PCT,
                    'qty': qty,
                    'time': datetime.now()
                }
                daily_signals.append(symbol)

                tp1 = price * TP_LEVELS[0]
                tp2 = price * TP_LEVELS[1]
                tp3 = price * TP_LEVELS[2]
                sl = price * SL_PCT

                message = (
                    f"🔔 BUY EXECUTED: {symbol}\n"
                    f"Entry: ${price:.2f}\n"
                    f"TP1: {tp1:.2f}, TP2: {tp2:.2f}, TP3: {tp3:.2f}\n"
                    f"SL: {sl:.2f}\n"
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
        qty = pos['qty']

        if len(pos['tps_hit']) < len(TRAILING_SL_PCTS):
            pos['sl_level'] = TRAILING_SL_PCTS[len(pos['tps_hit'])]

        # SL Check
        if price <= entry * pos['sl_level']:
            send_telegram(f"❌ STOP LOSS HIT: {symbol}\nEntry: ${entry:.4f} → SL: ${price:.4f}\nLoss: {((price-entry)/entry)*100:.2f}%")
            performance_log.append(((price-entry)/entry)*100)
            client.futures_create_order(
                symbol=symbol,
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=qty,
                reduceOnly=True
            )
            del open_positions[symbol]
            continue

        # TP check
        for i, level in enumerate(TP_LEVELS):
            if i in pos['tps_hit']:
                continue
            target_price = entry * level
            if price >= target_price:
                send_telegram(f"✅ TP{i+1} HIT: {symbol}\nEntry: ${entry:.4f} → Target: ${target_price:.4f}\nCuan: {((target_price-entry)/entry)*100:.2f}%")
                pos['tps_hit'].append(i)
                if i == len(TP_LEVELS) - 1:
                    performance_log.append(((target_price-entry)/entry)*100)
                    client.futures_create_order(
                        symbol=symbol,
                        side=SIDE_SELL,
                        type=ORDER_TYPE_MARKET,
                        quantity=qty,
                        reduceOnly=True
                    )
                    del open_positions[symbol]

        # Manual early exit suggestion
        if 0 in pos['tps_hit'] and price < entry * 1.01 and price > entry * 1.005:
            send_telegram(f"⚠️ EXIT MANUAL DISARANKAN: {symbol}\nEntry: ${entry:.4f} → Now: ${price:.4f}\nMasih cuan tipis, hindari SL ulang.")

        # 🔁 Auto-close on trend reversal
        try:
            data_30m = get_candles(symbol, '30m')
            ma5_now = calculate_ma(data_30m, 5)
            ma20_now = calculate_ma(data_30m, 20)
            if ma5_now < ma20_now:
                send_telegram(f"🔁 AUTO CLOSE: Trend reversal detected on {symbol}\nExit price: ${price:.4f}")
                performance_log.append(((price - entry) / entry) * 100)
                client.futures_create_order(
                    symbol=symbol,
                    side=SIDE_SELL,
                    type=ORDER_TYPE_MARKET,
                    quantity=qty,
                    reduceOnly=True
                )
                del open_positions[symbol]
        except Exception as e:
            print(f"Trend reversal check error: {e}")


print("🚀 Bot sinyal & live trading siap jalan...")
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
