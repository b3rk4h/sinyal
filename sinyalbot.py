import time
import math
import requests
import os
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import FuturesType
from ta.trend import ADXIndicator, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import OnBalanceVolumeIndicator

# === CONFIGURATION === #
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODAL_TOTAL = 20  # modal awal total $20
LEVERAGE = 20
active_signals = []  # Menyimpan sinyal yang masih aktif untuk dipantau TP-nya

client = Client(API_KEY, API_SECRET)
cooldowns = {}

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    requests.post(url, data=data)

def get_all_usdt_futures_symbols():
    info = client.futures_exchange_info()
    return [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL']

def filter_symbols(symbols):
    filtered = []
    for symbol in symbols:
        try:
            ticker = client.futures_ticker(symbol=symbol)
            if float(ticker['quoteVolume']) > 5000000:  # filter min volume
                filtered.append(symbol)
        except:
            continue
    return filtered

def fetch_klines(symbol, interval, limit=200):
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume', '_', '_', '_', '_', '_', '_'
    ])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def analyze(df):
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
    return df

def check_signal(symbol):
    try:
        now = time.time()
        cooldown_period = 300  # 5 menit
        if symbol in cooldowns and now - cooldowns[symbol] < cooldown_period:
            return

        df_1m = analyze(fetch_klines(symbol, '1m'))
        df_5m = analyze(fetch_klines(symbol, '5m'))
        df_15m = analyze(fetch_klines(symbol, '15m'))
        df_1h = analyze(fetch_klines(symbol, '1h'))
        df_4h = analyze(fetch_klines(symbol, '4h'))

        trend_1h = df_1h.iloc[-1]['trend_up']
        trend_4h = df_4h.iloc[-1]['trend_up']
        adx_1h = df_1h.iloc[-1]['adx']
        adx_4h = df_4h.iloc[-1]['adx']

        # Adaptive risk
        if trend_1h and trend_4h and adx_1h > 30 and adx_4h > 30:
            risk_pct = 0.05
        elif trend_1h or trend_4h:
            risk_pct = 0.03
        else:
            risk_pct = 0.01

        cond_up = (
            df_1m.iloc[-1]['trend_up'] and
            df_5m.iloc[-1]['trend_up'] and
            df_15m.iloc[-1]['trend_up'] and
            df_1h.iloc[-1]['trend_up'] and
            df_1m.iloc[-1]['breakout_up'] and
            df_1m.iloc[-1]['volume_spike'] and
            df_1m.iloc[-1]['strong_adx'] and
            df_1m.iloc[-1]['volatility_ok']
        )

        cond_down = (
            not df_1m.iloc[-1]['trend_up'] and
            not df_5m.iloc[-1]['trend_up'] and
            not df_15m.iloc[-1]['trend_up'] and
            not df_1h.iloc[-1]['trend_up'] and
            df_1m.iloc[-1]['breakout_down'] and
            df_1m.iloc[-1]['volume_spike'] and
            df_1m.iloc[-1]['strong_adx'] and
            df_1m.iloc[-1]['volatility_ok']
        )

        early_long = (
            df_1m.iloc[-1]['trend_up'] and
            df_5m.iloc[-1]['trend_up'] and
            not df_15m.iloc[-1]['trend_up'] and
            df_1m.iloc[-1]['volume_spike'] and
            df_1m.iloc[-1]['strong_adx'] and
            df_1m.iloc[-1]['volatility_ok']
        )

        early_short = (
            not df_1m.iloc[-1]['trend_up'] and
            not df_5m.iloc[-1]['trend_up'] and
            df_15m.iloc[-1]['trend_up'] and
            df_1m.iloc[-1]['volume_spike'] and
            df_1m.iloc[-1]['strong_adx'] and
            df_1m.iloc[-1]['volatility_ok']
        )

        price = df_1m.iloc[-1]['close']
        atr_4h = df_4h.iloc[-1]['atr']  # ATR dari 4H sebagai acuan SL/TP
        risk_dollar = MODAL_TOTAL * risk_pct
        sl_main = price - atr_4h if cond_up else price + atr_4h
        price_sl_diff = abs(price - sl_main)

        if price_sl_diff == 0:
            print(f"[WARNING] {symbol} - SL = Entry price. Size diset ke 0.")
            return

        size = round((risk_dollar / price_sl_diff) * LEVERAGE, 2)
        if size == 0:
            print(f"[INFO] {symbol} dilewati karena size 0 akibat SL terlalu dekat.")
            return

        # === SINYAL KUAT ===
        if cond_up or cond_down:
            cooldowns[symbol] = now
            tp1 = price + atr_4h * 1.5 if cond_up else price - atr_4h * 1.5
            tp2 = price + atr_4h * 2.5 if cond_up else price - atr_4h * 2.5
            tp3 = price + atr_4h * 4 if cond_up else price - atr_4h * 4
            sl = price - atr_4h if cond_up else price + atr_4h
            direction = "LONG" if cond_up else "SHORT"
            trend_text = "Bullish" if cond_up else "Bearish"
            emoji = "🚀" if cond_up else "🔻"
            strength_emoji = "🔥🔥🔥" if cond_up else "❄️❄️❄️"

            msg = (
                f"\n{emoji} <b><u>{direction} SIGNAL</u></b> - <b>{symbol}</b>\n"
                f"Price: <b>{price:.2f}</b>\nSL: <b>{sl:.2f}</b>\nSize: <b>{size}</b>\n"
                f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n"
                f"📈 Trend: {trend_text}\n"
                f"📊 Konfirmasi: ✅✅✅✅\n🎯 Sinyal: <b>KUAT</b> {strength_emoji}\n"
                f"🔁 Trailing aktif setelah TP1"
            )
            send_telegram(msg)

            active_signals.append({
                "symbol": symbol,
                "side": direction,
                "entry": price,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "notified_tp1": False,
                "notified_tp2": False
            })

            with open("log_sinyal.txt", "a") as f:
                f.write(f"{datetime.now()} | {symbol} | {direction} | {price:.2f} | SL: {sl:.2f} | Size: {size}\n")

        # === SINYAL EARLY ENTRY ===
        if early_long or early_short:
            cooldowns[symbol] = now
            tp1 = price + atr_4h * 1.2 if early_long else price - atr_4h * 1.2
            tp2 = price + atr_4h * 2 if early_long else price - atr_4h * 2
            tp3 = price + atr_4h * 3.5 if early_long else price - atr_4h * 3.5
            sl = price - atr_4h if early_long else price + atr_4h
            direction = "EARLY LONG" if early_long else "EARLY SHORT"
            emoji = "🟡" if early_long else "🔸"

            msg = (
                f"\n{emoji} <b><u>{direction}</u></b> - <b>{symbol}</b>\n"
                f"Price: <b>{price:.2f}</b>\nSL: <b>{sl:.2f}</b>\n"
                f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n"
                f"⏳ <i>Entry awal - Konfirmasi belum penuh</i>"
            )
            send_telegram(msg)

    except Exception as e:
        print(f"[ERROR] Saat cek sinyal {symbol}: {str(e)}")

		
def monitor_active_signals():
    try:
        for signal in active_signals:
            symbol = signal['symbol']
            klines = client.futures_klines(symbol=symbol, interval='1m', limit=2)
            last_price = float(klines[-1][4])  # Close price dari candle terakhir

            # LONG
            if signal['side'] == 'LONG':
                if not signal['notified_tp1'] and last_price >= signal['tp1']:
                    send_telegram(f"✅ <b>{symbol} - TP1 TERCAPAI</b>\nHarga: {last_price:.2f}")
                    signal['notified_tp1'] = True
                if not signal['notified_tp2'] and last_price >= signal['tp2']:
                    send_telegram(f"🎯 <b>{symbol} - TP2 TERCAPAI</b>\nHarga: {last_price:.2f}")
                    signal['notified_tp2'] = True

            # SHORT
            elif signal['side'] == 'SHORT':
                if not signal['notified_tp1'] and last_price <= signal['tp1']:
                    send_telegram(f"✅ <b>{symbol} - TP1 TERCAPAI</b>\nHarga: {last_price:.2f}")
                    signal['notified_tp1'] = True
                if not signal['notified_tp2'] and last_price <= signal['tp2']:
                    send_telegram(f"🎯 <b>{symbol} - TP2 TERCAPAI</b>\nHarga: {last_price:.2f}")
                    signal['notified_tp2'] = True

    except Exception as e:
        print(f"[ERROR] Saat monitor TP: {str(e)}")


# Loop utama
while True:
    try:
        client.futures_ping()
        symbols = filter_symbols(get_all_usdt_futures_symbols())
        sampled = symbols[:20]  # Batasi simbol agar tidak overload
        for sym in sampled:
            check_signal(sym)
            time.sleep(0.5)

        monitor_active_signals()  # Panggil monitor TP di dalam try-block

        time.sleep(60)

    except Exception as err:
        print(f"Main loop error: {err}")
        time.sleep(60)

