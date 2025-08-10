import time
import math
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
active_signals = []  # Menyimpan sinyal yang masih aktif untuk dipantau TP/SL
cooldowns = {}

client = Client(API_KEY, API_SECRET)

# === UTILS === #
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    requests.post(url, data=data)

def log_event(text):
    with open("log_sinyal.txt", "a") as f:
        f.write(f"{datetime.now()} | {text}\n")

def get_all_usdt_futures_symbols():
    info = client.futures_exchange_info()
    return [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL']

def filter_symbols(symbols):
    filtered = []
    for symbol in symbols:
        try:
            ticker = client.futures_ticker(symbol=symbol)
            if float(ticker['quoteVolume']) > 5_000_000:
                filtered.append(symbol)
        except:
            continue
    return filtered

def fetch_multi_tf(symbol):
    tf_list = ['1m', '5m', '15m', '1h', '4h']
    data = {}
    for tf in tf_list:
        klines = client.futures_klines(symbol=symbol, interval=tf, limit=200)
        df = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            '_', '_', '_', '_', '_', '_'
        ])
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['volume'] = df['volume'].astype(float)
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        df['ma_fast'] = SMAIndicator(df['close'], 5).sma_indicator()
        df['ma_slow'] = SMAIndicator(df['close'], 20).sma_indicator()
        df['rsi'] = RSIIndicator(df['close']).rsi()
        df['adx'] = ADXIndicator(df['high'], df['low'], df['close']).adx()
        df['atr'] = AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
        df['bb_upper'] = BollingerBands(df['close']).bollinger_hband()
        df['bb_lower'] = BollingerBands(df['close']).bollinger_lband()
        df['volume_spike'] = df['volume'] > df['volume'].rolling(20).mean() * 1.5
        df['trend_up'] = df['ma_fast'] > df['ma_slow']
        df['breakout_up'] = df['close'] > df['bb_upper']
        df['breakout_down'] = df['close'] < df['bb_lower']
        df['strong_adx'] = df['adx'] > 25
        df['volatility_ok'] = df['atr'] > df['atr'].rolling(20).mean()

        data[tf] = df
    return data

def check_signal(symbol):
    try:
        now = time.time()
        cooldown_period = 300

        # Cek cooldown
        if symbol in cooldowns and now - cooldowns[symbol] < cooldown_period:
            return

        data = fetch_multi_tf(symbol)
        d1, d5, d15, d1h, d4h = data['1m'], data['5m'], data['15m'], data['1h'], data['4h']

        trend_1h = d1h.iloc[-1]['trend_up']
        trend_4h = d4h.iloc[-1]['trend_up']
        adx_1h = d1h.iloc[-1]['adx']
        adx_4h = d4h.iloc[-1]['adx']

        # Adaptive risk
        if trend_1h and trend_4h and adx_1h > 30 and adx_4h > 30:
            risk_pct = 0.05
        elif trend_1h or trend_4h:
            risk_pct = 0.03
        else:
            risk_pct = 0.01

        cond_up = (
            d1.iloc[-1]['trend_up'] and
            d5.iloc[-1]['trend_up'] and
            d15.iloc[-1]['trend_up'] and
            trend_1h and
            d1.iloc[-1]['breakout_up'] and
            d1.iloc[-1]['volume_spike'] and
            d1.iloc[-1]['strong_adx'] and
            d1.iloc[-1]['volatility_ok']
        )

        cond_down = (
            not d1.iloc[-1]['trend_up'] and
            not d5.iloc[-1]['trend_up'] and
            not d15.iloc[-1]['trend_up'] and
            not trend_1h and
            d1.iloc[-1]['breakout_down'] and
            d1.iloc[-1]['volume_spike'] and
            d1.iloc[-1]['strong_adx'] and
            d1.iloc[-1]['volatility_ok']
        )

        price = d1.iloc[-1]['close']
        atr_4h = d4h.iloc[-1]['atr']
        risk_dollar = MODAL_TOTAL * risk_pct
        sl_main = price - atr_4h if cond_up else price + atr_4h
        price_sl_diff = abs(price - sl_main)
        if price_sl_diff == 0:
            return

        size = round((risk_dollar / price_sl_diff) * LEVERAGE, 2)
        if size == 0:
            return

        # --- CEK JIKA SUDAH ADA POSISI SEARAH ---
        existing_signal = next((sig for sig in active_signals if sig["symbol"] == symbol), None)
        if existing_signal:
            if cond_up and existing_signal["side"] == "LONG":
                # Update TP/SL naikkan
                existing_signal["tp1"] = price + atr_4h * 1.8
                existing_signal["tp2"] = price + atr_4h * 3
                existing_signal["tp3"] = price + atr_4h * 4.5
                existing_signal["sl"] = max(existing_signal["sl"], price - atr_4h * 0.8)

                msg = (
                    f"🟢 <b>HOLD LONG</b> - <b>{symbol}</b>\n"
                    f"📈 Posisi tetap, TP/SL diperbarui:\n"
                    f"🎯 TP1: {existing_signal['tp1']:.3f} | TP2: {existing_signal['tp2']:.3f} | TP3: {existing_signal['tp3']:.3f}\n"
                    f"🛡 SL: {existing_signal['sl']:.3f}"
                )
                send_telegram(msg)
                return

            elif cond_down and existing_signal["side"] == "SHORT":
                # Update TP/SL turunkan
                existing_signal["tp1"] = price - atr_4h * 1.8
                existing_signal["tp2"] = price - atr_4h * 3
                existing_signal["tp3"] = price - atr_4h * 4.5
                existing_signal["sl"] = min(existing_signal["sl"], price + atr_4h * 0.8)

                msg = (
                    f"🔴 <b>HOLD SHORT</b> - <b>{symbol}</b>\n"
                    f"📉 Posisi tetap, TP/SL diperbarui:\n"
                    f"🎯 TP1: {existing_signal['tp1']:.3f} | TP2: {existing_signal['tp2']:.3f} | TP3: {existing_signal['tp3']:.3f}\n"
                    f"🛡 SL: {existing_signal['sl']:.3f}"
                )
                send_telegram(msg)
                return

        # --- SINYAL BARU ---
        if cond_up or cond_down:
            cooldowns[symbol] = now
            tp1 = price + atr_4h * 1.5 if cond_up else price - atr_4h * 1.5
            tp2 = price + atr_4h * 2.5 if cond_up else price - atr_4h * 2.5
            tp3 = price + atr_4h * 4 if cond_up else price - atr_4h * 4
            sl = sl_main
            direction = "LONG" if cond_up else "SHORT"
            emoji = "🚀" if cond_up else "🔻"
            strength_emoji = "🔥🔥🔥" if cond_up else "❄️❄️❄️"

            msg = (
                f"\n{emoji} <b><u>{direction} SIGNAL</u></b> - <b>{symbol}</b>\n"
                f"Price: <b>{price:.3f}</b>\nSL: <b>{sl:.3f}</b>\nSize: <b>{size}</b>\n"
                f"🎯 TP1: {tp1:.3f} | TP2: {tp2:.3f} | TP3: {tp3:.3f}\n"
                f"📊 Sinyal: <b>KUAT</b> {strength_emoji}\n"
                f"🔁 Trailing aktif setelah TP1"
            )
            send_telegram(msg)
            log_event(f"{symbol} | {direction} | {price:.3f} | SL: {sl:.3f} | Size: {size}")
            active_signals.append({
                "symbol": symbol,
                "side": direction,
                "entry": price,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "sl": sl,
                "trailing_active": False,
                "notified_tp1": False,
                "notified_tp2": False,
                "notified_tp3": False
            })

    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")


def monitor_active_signals():
    try:
        for signal in active_signals[:]:
            symbol = signal['symbol']
            entry_price = signal['entry']
            side = signal['side']
            side_emoji = "🚀" if side == "LONG" else "🔻"
            klines = client.futures_klines(symbol=symbol, interval='1m', limit=2)
            last_price = float(klines[-1][4])

            # SL check
            if (side == 'LONG' and last_price <= signal['sl']) or \
               (side == 'SHORT' and last_price >= signal['sl']):
                send_telegram(
                    f"❌ {symbol} | STOP LOSS 💀 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}"
                )
                log_event(f"{symbol} | SL Hit | {last_price:.3f} | Entry {entry_price:.3f} | {side}")
                active_signals.remove(signal)
                continue

            # TP checks
            if side == 'LONG':
                if not signal['notified_tp1'] and last_price >= signal['tp1']:
                    send_telegram(f"✅ {symbol} | TP1 🎯 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp1'] = True
                    signal['trailing_active'] = True
                    signal['sl'] = entry_price
                if not signal['notified_tp2'] and last_price >= signal['tp2']:
                    send_telegram(f"🏅 {symbol} | TP2 🥈 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp2'] = True
                    signal['sl'] = signal['tp1']
                if not signal['notified_tp3'] and last_price >= signal['tp3']:
                    send_telegram(f"🏆 {symbol} | TP3 🥇 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp3'] = True
                    active_signals.remove(signal)

            elif side == 'SHORT':
                if not signal['notified_tp1'] and last_price <= signal['tp1']:
                    send_telegram(f"✅ {symbol} | TP1 🎯 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp1'] = True
                    signal['trailing_active'] = True
                    signal['sl'] = entry_price
                if not signal['notified_tp2'] and last_price <= signal['tp2']:
                    send_telegram(f"🏅 {symbol} | TP2 🥈 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp2'] = True
                    signal['sl'] = signal['tp1']
                if not signal['notified_tp3'] and last_price <= signal['tp3']:
                    send_telegram(f"🏆 {symbol} | TP3 🥇 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp3'] = True
                    active_signals.remove(signal)

    except Exception as e:
        print(f"[ERROR monitor]: {e}")


# === MAIN LOOP === #
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
        print(f"Main loop error: {err}")
        time.sleep(60)
