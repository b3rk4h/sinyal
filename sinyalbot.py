import ccxt
import pandas as pd
import ta
import time
import requests
from datetime import datetime

# === Telegram Config ===
TELEGRAM_TOKEN = '8074521734:AAHIJRTB9Md96h1b690T2iRRzytMwJACxkc'
CHAT_ID = '1950841966'

def send_telegram(text):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    data = {'chat_id': CHAT_ID, 'text': text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Telegram error: {e}")

# === Exchange & Market Config ===
exchange = ccxt.binance({'enableRateLimit': True})
symbols = ['SOL/USDT', 'OP/USDT', 'DOGE/USDT', 'PEPE/USDT', 'WIF/USDT']
timeframe = '15m'
limit = 120
last_signal = {}

# === Hitung TP & SL ===
def calculate_targets(price, signal, total_amount=20):
    if signal == 'BUY':
        tp1 = price * 1.015
        tp2 = price * 1.03
        tp3 = price * 1.05
        sl = price * 0.98
    elif signal == 'SELL':
        tp1 = price * 0.985
        tp2 = price * 0.97
        tp3 = price * 0.95
        sl = price * 1.02
    else:
        return None, None, None, None, None

    tp1_amt = total_amount * 0.5
    tp2_amt = total_amount * 0.3
    tp3_amt = total_amount * 0.2

    return (round(tp1, 4), round(tp2, 4), round(tp3, 4), round(sl, 4),
            [tp1_amt, tp2_amt, tp3_amt])

# === Analisa Sinyal ===
def analyze(df):
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    bb = ta.volatility.BollingerBands(df['close'], window=20)
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_lower'] = bb.bollinger_lband()
    df['volume_ma'] = df['volume'].rolling(10).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    signal = None
    reason = []

    if (latest['ma5'] > latest['ma20'] and
        latest['rsi'] < 70 and
        latest['close'] > latest['ma5'] and
        latest['close'] > prev['close'] and
        latest['volume'] > 1.2 * latest['volume_ma']):
        signal = 'BUY'
        reason.append("MA5 > MA20")
        reason.append("RSI OK")
        reason.append("Break MA + Volume Spike")
        reason.append("Konfirmasi Bullish")

    elif (latest['ma5'] < latest['ma20'] and
          latest['rsi'] > 30 and
          latest['close'] < latest['ma5'] and
          latest['close'] < prev['close'] and
          latest['volume'] > 1.2 * latest['volume_ma']):
        signal = 'SELL'
        reason.append("MA5 < MA20")
        reason.append("RSI OK")
        reason.append("Breakdown + Volume Spike")
        reason.append("Konfirmasi Bearish")

    return signal, reason

# === Main Bot ===
print("\n✅ Bot sinyal trading dimulai...")
while True:
    try:
        for symbol in symbols:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            signal, reason = analyze(df)
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M')

            if signal and last_signal.get(symbol) != signal:
                price = df['close'].iloc[-1]
                tp1, tp2, tp3, sl, [amt1, amt2, amt3] = calculate_targets(price, signal)

                message = (
                    f"== {signal} SIGNAL ==\n"
                    f"📈 Pair: {symbol}\n"
                    f"💰 Harga: {price:.4f}\n"
                    f"🕒 Waktu: {now} UTC\n"
                    f"📋 Alasan: {', '.join(reason)}\n\n"
                    f"🎯 Rekomendasi TP & SL:\n"
                    f"- TP1 (50%): {tp1} (${amt1})\n"
                    f"- TP2 (30%): {tp2} (${amt2})\n"
                    f"- TP3 (20%): {tp3} (${amt3})\n"
                    f"- SL: {sl}\n\n"
                    f"✅ Konfirmasi valid — kamu bisa entry sekarang!\n"
                    f"⚠️ Gunakan money management & trailing SL."
                )

                send_telegram(message)
                last_signal[symbol] = signal

        time.sleep(60)

    except Exception as e:
        print(f"Error: {e}")
        time.sleep(60)
		
