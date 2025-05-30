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
            trend_strength = "💪 KUAT (HIGH RISK)"
        elif trend_1h or trend_4h:
            risk_pct = 0.03
            trend_strength = "⚖️ MODERAT"
        else:
            risk_pct = 0.01
            trend_strength = "⚠️ LEMAH (LOW RISK)"

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

        price = df_1m.iloc[-1]['close']
        atr = df_1m.iloc[-1]['atr']
        risk_dollar = MODAL_TOTAL * risk_pct
        sl = price - atr if cond_up else price + atr
        price_sl_diff = abs(price - sl)

        if price_sl_diff == 0:
            print(f"[WARNING] {symbol} - SL sama dengan entry price! Size diset ke 0 untuk hindari error.")
            size = 0
        else:
            size = round((risk_dollar / price_sl_diff) * LEVERAGE, 2)

        if size == 0:
            print(f"[INFO] {symbol} dilewati karena size 0 akibat SL terlalu dekat.")
            return

        if cond_up or cond_down:
            cooldowns[symbol] = now

        if cond_up:
            tp1 = price + atr * 1.5
            tp2 = price + atr * 2.5
            tp3 = price + atr * 4
            msg = (
                f"\n🚀 <b><u>LONG SIGNAL</u></b> - <b>{symbol}</b>\n"
                f"Price: <b>{price:.2f}</b>\nSL: <b>{sl:.2f}</b>\nSize: <b>{size}</b>\n"
                f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n📈 Trend: Bullish\n"
                f"📊 Konfirmasi: ✅✅✅✅\n🎯 Sinyal: <b>KUAT</b> 🔥🔥🔥\n"
                f"🔁 Trailing aktif setelah TP1"
            )
            send_telegram(msg)

        elif cond_down:
            tp1 = price - atr * 1.5
            tp2 = price - atr * 2.5
            tp3 = price - atr * 4
            msg = (
                f"\n🔻 <b><u>SHORT SIGNAL</u></b> - <b>{symbol}</b>\n"
                f"Price: <b>{price:.2f}</b>\nSL: <b>{sl:.2f}</b>\nSize: <b>{size}</b>\n"
                f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n📉 Trend: Bearish\n"
                f"📊 Konfirmasi: ✅✅✅✅\n🎯 Sinyal: <b>KUAT</b> ❄️❄️❄️\n"
                f"🔁 Trailing aktif setelah TP1"
            )
            send_telegram(msg)

        # Log ke file jika ada sinyal valid
        if cond_up or cond_down:
            with open("log_sinyal.txt", "a") as f:
                f.write(f"{datetime.now()} | {symbol} | {'LONG' if cond_up else 'SHORT'} | {price:.2f} | SL: {sl:.2f} | Size: {size}\n")

    except Exception as e:
        print(f"[ERROR] Saat cek sinyal {symbol}: {str(e)}")

# Loop utama
while True:
    try:
        client.futures_ping()
        symbols = filter_symbols(get_all_usdt_futures_symbols())
        sampled = symbols[:20]  # Batasi simbol agar tidak overload
        for sym in sampled:
            check_signal(sym)
            time.sleep(0.5)
        time.sleep(60)
    except Exception as err:
        print(f"Main loop error: {err}")
        time.sleep(60)
