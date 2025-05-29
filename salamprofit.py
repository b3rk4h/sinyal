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
    data = {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Telegram error: {e}")

# === Exchange & Market Config ===
exchange = ccxt.binance({'enableRateLimit': True})
symbols = ['SOL/USDT', 'SUI/USDT', 'BTC/USDT', 'ETH/USDT', 'OP/USDT', 'WIF/USDT', 'DOGE/USDT']
timeframe = '15m'
limit = 120
last_signal = {}
last_preview = {}
hit_tp = {}

# === Modal & Risk Management ===
TOTAL_CAPITAL = 20
RISK_PER_TRADE = 0.05

def calculate_targets(price, signal, atr):
    if signal == 'BUY':
        tp1 = price * 1.003
        tp2 = price * 1.006
        tp3 = price * 1.009
        sl = price - atr * 1.2
    else:  # SELL
        tp1 = price * 0.997
        tp2 = price * 0.994
        tp3 = price * 0.991
        sl = price + atr * 1.2

    risk_amount = TOTAL_CAPITAL * RISK_PER_TRADE
    position_size = risk_amount / abs(price - sl)

    tp1_amt = position_size * abs(tp1 - price)
    tp2_amt = position_size * abs(tp2 - price)
    tp3_amt = position_size * abs(tp3 - price)

    return (round(tp1, 4), round(tp2, 4), round(tp3, 4), round(sl, 4), round(position_size, 2),
            [round(tp1_amt, 2), round(tp2_amt, 2), round(tp3_amt, 2)])

def analyze(df):
    df['ma5'] = df['close'].rolling(5).mean()
    df['ma20'] = df['close'].rolling(20).mean()
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    df['volume_ma'] = df['volume'].rolling(10).mean()
    df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    signal = None
    reason = []

    price = latest['close']
    atr = latest['atr']
    atr_ratio = atr / price

    if atr_ratio < 0.002:
        return None, ["❌ Volatilitas terlalu rendah (<0.2%)"], atr
    elif atr_ratio > 0.015:
        return None, ["❌ Volatilitas terlalu tinggi (>1.5%)"], atr

    # BUY signal
    if (latest['ma5'] > latest['ma20'] and
        latest['macd'] > latest['macd_signal'] and
        latest['rsi'] < 70 and
        latest['adx'] > 20 and
        latest['close'] > prev['close'] and
        latest['volume'] > 1.2 * latest['volume_ma']):
        signal = 'BUY'
        reason += ["MA5 > MA20", "MACD bullish", "ADX > 20 (Trend kuat)", "Candle naik + volume"]

    # SELL signal
    elif (latest['ma5'] < latest['ma20'] and
          latest['macd'] < latest['macd_signal'] and
          latest['rsi'] > 30 and
          latest['adx'] > 20 and
          latest['close'] < prev['close'] and
          latest['volume'] > 1.2 * latest['volume_ma']):
        signal = 'SELL'
        reason += ["MA5 < MA20", "MACD bearish", "ADX > 20 (Trend kuat)", "Candle turun + volume"]

    return signal, reason, atr

print("\n✅ Bot sinyal trading dimulai dengan early signal, TP 0.3–0.9%, ADX + ATR filter aktif...")
while True:
    try:
        for symbol in symbols:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            signal, reason, atr = analyze(df)
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
            price = df['close'].iloc[-1]

            if signal:
                tp1, tp2, tp3, sl, position_size, [amt1, amt2, amt3] = calculate_targets(price, signal, atr)
                color_tag = '🟢 BUY SIGNAL' if signal == 'BUY' else '🔴 SELL SIGNAL'

                if last_signal.get(symbol) != signal:
                    message = (
                        f"<b>{color_tag}</b>\n"
                        f"📊 Pair: <b>{symbol}</b>\n"
                        f"💰 Harga: <b>{price:.4f}</b>\n"
                        f"📏 Posisi: <b>{position_size} USDT</b>\n"
                        f"🕒 Waktu: <b>{now} WIB</b>\n"
                        f"📋 Alasan: {', '.join(reason)}\n\n"
                        f"🎯 Target:\n"
                        f"• TP1: {tp1} (+${amt1})\n"
                        f"• TP2: {tp2} (+${amt2})\n"
                        f"• TP3: {tp3} (+${amt3})\n"
                        f"• SL : {sl} (−${TOTAL_CAPITAL * RISK_PER_TRADE})\n\n"
                        f"✅ Entry disarankan sekarang.\n📌 Gunakan trailing SL untuk amankan profit."
                    )
                    send_telegram(message)
                    last_signal[symbol] = signal
                    hit_tp[symbol] = {'tp1': False, 'tp2': False, 'tp3': False}
                    last_preview[symbol] = None

                elif last_preview.get(symbol) != signal:
                    preview_tag = '🟢 Early BUY Preview' if signal == 'BUY' else '🔴 Early SELL Preview'
                    preview_msg = (
                        f"<b>{preview_tag}</b>\n"
                        f"📊 Pair: {symbol}\n"
                        f"💰 Harga saat ini: {price:.4f}\n"
                        f"📋 Potensi sinyal: {', '.join(reason)}\n"
                        f"⏳ Menunggu candle berikut..."
                    )
                    send_telegram(preview_msg)
                    last_preview[symbol] = signal

            # === TP Monitor ===
            if symbol in last_signal:
                signal = last_signal[symbol]
                tp1, tp2, tp3, sl, _, _ = calculate_targets(price, signal, atr)

                if signal == 'BUY':
                    if not hit_tp[symbol]['tp1'] and price >= tp1:
                        send_telegram(f"✅ <b>TP1 HIT</b> di {symbol} — pertimbangkan naikkan SL!")
                        hit_tp[symbol]['tp1'] = True
                    if not hit_tp[symbol]['tp2'] and price >= tp2:
                        send_telegram(f"✅ <b>TP2 HIT</b> di {symbol} — SL bisa di atas TP1.")
                        hit_tp[symbol]['tp2'] = True
                    if not hit_tp[symbol]['tp3'] and price >= tp3:
                        send_telegram(f"✅ <b>TP3 HIT</b> di {symbol} — take full profit disarankan.")
                        hit_tp[symbol]['tp3'] = True

                elif signal == 'SELL':
                    if not hit_tp[symbol]['tp1'] and price <= tp1:
                        send_telegram(f"✅ <b>TP1 HIT</b> di {symbol} — pertimbangkan turunkan SL!")
                        hit_tp[symbol]['tp1'] = True
                    if not hit_tp[symbol]['tp2'] and price <= tp2:
                        send_telegram(f"✅ <b>TP2 HIT</b> di {symbol} — SL bisa di bawah TP1.")
                        hit_tp[symbol]['tp2'] = True
                    if not hit_tp[symbol]['tp3'] and price <= tp3:
                        send_telegram(f"✅ <b>TP3 HIT</b> di {symbol} — take full profit disarankan.")
                        hit_tp[symbol]['tp3'] = True

        time.sleep(60)

    except Exception as e:
        print(f"❌ Error: {e}")
        time.sleep(60)
