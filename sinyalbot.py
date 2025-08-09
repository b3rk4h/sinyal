import time
import os
import csv
import requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from ta.trend import SMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange

# ========= CONFIG =========
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MODAL_TOTAL = 20
LEVERAGE = 20

# Feature toggles
USE_MTF = True                # require 4H trend confirmation
USE_ADX_VOLUME = True         # require ADX + volume confirmation
USE_ATR_SL_TP = True          # use ATR-based SL/TP
USE_RSI_DIVERGENCE = False    # optional more strict filter (RSI divergence)
USE_VOL_CONTRACTION = False   # optional squeeze filter
LOG_CSV = True
COOLDOWN_SECONDS = 300        # cooldown per symbol for NEW/HOLD/REVERSE
CACHE_4H_SECONDS = 60 * 5     # cache 4H/1H for 5 minutes
LOG_FLUSH_SECONDS = 60        # flush logs buffer every 60s
SCAN_LIMIT = 20               # symbols per loop

# thresholds
ADX_4H_MIN = 25
VOL1M_MULT = 1.5
VOL4H_MULT = 1.2
VOL_CONTRACTION_WINDOW = 20
VOL_CONTRACTION_THRESHOLD = 0.6
DIVERGENCE_LOOKBACK = 8

# Files
CSV_FILE = "signals_log.csv"
TXT_LOG = "log_sinyal.txt"

# ========= CLIENT & STATE =========
client = Client(API_KEY, API_SECRET)
active_signals = []
cooldowns = {}
tf_cache = {}       # {symbol: { '1h': (ts, df), '4h': (ts, df) }}
log_buffer = []     # buffered csv rows

# ========= UTIL =========
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - message:\n", msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=8)
    except Exception as e:
        print("Telegram send error:", e)

def append_txt_log(text):
    try:
        with open(TXT_LOG, "a") as f:
            f.write(f"{datetime.now()} | {text}\n")
    except Exception:
        pass

def buffer_log_csv(event, symbol, side, entry, price, sl, tp_level, notes=""):
    if not LOG_CSV:
        return
    log_buffer.append([datetime.utcnow().isoformat(), event, symbol, side, entry, price, sl, tp_level, notes])

def flush_log_buffer():
    if not LOG_CSV or not log_buffer:
        return
    header = not os.path.exists(CSV_FILE)
    try:
        with open(CSV_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if header:
                w.writerow(["timestamp","event","symbol","side","entry","price","sl","tp_level","notes"])
            w.writerows(log_buffer)
        log_buffer.clear()
    except Exception as e:
        print("Flush CSV error:", e)

def fetch_klines_df(symbol, interval, limit=200):
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        'timestamp','open','high','low','close','volume','_','_','_','_','_','_'
    ])
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def enrich_indicators(df):
    # safe compute indicators
    try:
        df['ma_fast'] = SMAIndicator(df['close'], 5).sma_indicator()
        df['ma_slow'] = SMAIndicator(df['close'], 20).sma_indicator()
        df['rsi'] = RSIIndicator(df['close']).rsi()
        df['adx'] = ADXIndicator(df['high'], df['low'], df['close']).adx()
        df['atr'] = AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
        bb = BollingerBands(df['close'])
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_lower'] = bb.bollinger_lband()
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['close']
        df['volume_spike'] = df['volume'] > df['volume'].rolling(20).mean() * VOL1M_MULT
        df['trend_up'] = df['ma_fast'] > df['ma_slow']
        df['breakout_up'] = df['close'] > df['bb_upper']
        df['breakout_down'] = df['close'] < df['bb_lower']
        df['strong_adx'] = df['adx'] > 25
        df['volatility_ok'] = df['atr'] > df['atr'].rolling(20).mean()
    except Exception as e:
        # indicator calc errors ignored (df may miss some cols)
        print("Indicator enrich error:", e)
    return df

def get_cached_tf(symbol, tf):
    now = time.time()
    if tf in ('1m','5m','15m'):
        df = fetch_klines_df(symbol, tf, limit=200)
        return enrich_indicators(df)
    # cache policy for 1h/4h
    if symbol not in tf_cache:
        tf_cache[symbol] = {}
    ent = tf_cache[symbol].get(tf)
    if ent and now - ent[0] < CACHE_4H_SECONDS:
        return ent[1]
    df = fetch_klines_df(symbol, tf, limit=200)
    df = enrich_indicators(df)
    tf_cache[symbol][tf] = (now, df)
    return df

# small helpers
def atr_based_tp_sl(price, atr, direction, sl_mult=1.0, tp_mults=(1.5,2.5,4.0)):
    if direction == "LONG":
        tp1 = price + atr * tp_mults[0]
        tp2 = price + atr * tp_mults[1]
        tp3 = price + atr * tp_mults[2]
        sl = price - atr * sl_mult
    else:
        tp1 = price - atr * tp_mults[0]
        tp2 = price - atr * tp_mults[1]
        tp3 = price - atr * tp_mults[2]
        sl = price + atr * sl_mult
    return tp1, tp2, tp3, sl

def detect_rsi_divergence(df_short, lookback=DIVERGENCE_LOOKBACK):
    # simple check: compare last vs mid for RSI vs price
    if len(df_short) < lookback + 2 or 'rsi' not in df_short:
        return None
    sub = df_short[-(lookback+1):].reset_index(drop=True)
    prices = sub['close'].values
    rsi = sub['rsi'].values
    mid_idx = max(0, len(prices)//2 - 1)
    try:
        if prices[-1] < prices[mid_idx] and rsi[-1] > rsi[mid_idx]:
            return "BULL"
        if prices[-1] > prices[mid_idx] and rsi[-1] < rsi[mid_idx]:
            return "BEAR"
    except Exception:
        return None
    return None

def detect_vol_contraction(df_short, window=VOL_CONTRACTION_WINDOW, threshold=VOL_CONTRACTION_THRESHOLD):
    if 'bb_width' not in df_short or len(df_short) < window + 2:
        return False
    mean_bw = df_short['bb_width'].rolling(window).mean().iloc[-1]
    cur = df_short['bb_width'].iloc[-1]
    if pd.isna(mean_bw) or mean_bw == 0:
        return False
    return (cur / mean_bw) < threshold

# ========= MAIN check_signal =========
def check_signal(symbol):
    try:
        now = time.time()
        # fetch fast TFs
        d1 = get_cached_tf(symbol, '1m')
        d5 = get_cached_tf(symbol, '5m')
        d15 = get_cached_tf(symbol, '15m')
        # fetch cached heavy TFs
        d1h = get_cached_tf(symbol, '1h')
        d4h = get_cached_tf(symbol, '4h')

        # validation
        for df in (d1,d5,d15,d1h,d4h):
            if not isinstance(df, pd.DataFrame) or len(df) < 30:
                return

        trend_1h = bool(d1h.iloc[-1].get('trend_up', False))
        trend_4h = bool(d4h.iloc[-1].get('trend_up', False))
        adx_1h = float(d1h.iloc[-1].get('adx', 0) or 0)
        adx_4h = float(d4h.iloc[-1].get('adx', 0) or 0)

        # adaptive risk (original)
        if trend_1h and trend_4h and adx_1h > 30 and adx_4h > 30:
            risk_pct = 0.05
        elif trend_1h or trend_4h:
            risk_pct = 0.03
        else:
            risk_pct = 0.01

        cond_up = (
            bool(d1.iloc[-1].get('trend_up', False)) and
            bool(d5.iloc[-1].get('trend_up', False)) and
            bool(d15.iloc[-1].get('trend_up', False)) and
            trend_1h and
            bool(d1.iloc[-1].get('breakout_up', False)) and
            bool(d1.iloc[-1].get('volume_spike', False)) and
            bool(d1.iloc[-1].get('strong_adx', False)) and
            bool(d1.iloc[-1].get('volatility_ok', False))
        )

        cond_down = (
            not bool(d1.iloc[-1].get('trend_up', False)) and
            not bool(d5.iloc[-1].get('trend_up', False)) and
            not bool(d15.iloc[-1].get('trend_up', False)) and
            not trend_1h and
            bool(d1.iloc[-1].get('breakout_down', False)) and
            bool(d1.iloc[-1].get('volume_spike', False)) and
            bool(d1.iloc[-1].get('strong_adx', False)) and
            bool(d1.iloc[-1].get('volatility_ok', False))
        )

        # optional MTF confirm (4H)
        if USE_MTF:
            if cond_up and not trend_4h:
                return
            if cond_down and trend_4h:
                return

        # ADX + volume filter
        if USE_ADX_VOLUME and (cond_up or cond_down):
            vol4h_avg = d4h['volume'].rolling(20).mean().iloc[-1]
            vol4h_spike = d4h['volume'].iloc[-1] > vol4h_avg * VOL4H_MULT if not pd.isna(vol4h_avg) else False
            if adx_4h < ADX_4H_MIN and d1.iloc[-1].get('adx', 0) < ADX_4H_MIN:
                return
            if not (d1.iloc[-1].get('volume_spike', False) or vol4h_spike):
                return

        price = float(d1.iloc[-1]['close'])
        atr_4h = float(d4h.iloc[-1].get('atr', 0) or d1.iloc[-1].get('atr', 0) or 0)
        if atr_4h == 0:
            return

        risk_dollar = MODAL_TOTAL * risk_pct
        sl_main = price - atr_4h if cond_up else price + atr_4h
        price_sl_diff = abs(price - sl_main)
        if price_sl_diff == 0:
            return

        size = round((risk_dollar / price_sl_diff) * LEVERAGE, 2)
        if size == 0:
            return

        # cooldown per symbol (avoid spam)
        last_ts = cooldowns.get(symbol, 0)
        if now - last_ts < COOLDOWN_SECONDS:
            return

        existing = next((s for s in active_signals if s['symbol'] == symbol), None)

        # optional divergence & squeeze
        if USE_RSI_DIVERGENCE and (cond_up or cond_down):
            div = detect_rsi_divergence(d5, DIVERGENCE_LOOKBACK)
            if cond_up and div == "BEAR":
                return
            if cond_down and div == "BULL":
                return

        if USE_VOL_CONTRACTION and (cond_up or cond_down):
            if not detect_vol_contraction(d1, VOL_CONTRACTION_WINDOW, VOL_CONTRACTION_THRESHOLD):
                return

        # compute TP/SL (ATR-based if enabled)
        direction = "LONG" if cond_up else "SHORT"
        if USE_ATR_SL_TP:
            tp1, tp2, tp3, sl = atr_based_tp_sl(price, atr_4h, direction)
        else:
            tp1 = price + atr_4h * 1.5 if cond_up else price - atr_4h * 1.5
            tp2 = price + atr_4h * 2.5 if cond_up else price - atr_4h * 2.5
            tp3 = price + atr_4h * 4 if cond_up else price - atr_4h * 4
            sl = sl_main

        emoji = "🚀" if cond_up else "🔻"
        strength_emoji = "🔥🔥🔥" if cond_up else "❄️❄️❄️"

        # existing -> HOLD or REVERSE
        if existing:
            # same direction -> HOLD & update
            if (cond_up and existing['side']=="LONG") or (cond_down and existing['side']=="SHORT"):
                existing['entry'] = price
                existing['tp1'] = tp1
                existing['tp2'] = tp2
                existing['tp3'] = tp3
                if existing.get('sl') is not None:
                    if existing['side']=="LONG":
                        existing['sl'] = max(existing['sl'], sl)
                    else:
                        existing['sl'] = min(existing['sl'], sl)
                else:
                    existing['sl'] = sl

                hold_msg = (f"⏸ HOLD {symbol} | {emoji} {existing['side']} | Update Entry {price:.3f} "
                            f"| TP1: {existing['tp1']:.3f} | TP2: {existing['tp2']:.3f} | TP3: {existing['tp3']:.3f} | SL: {existing['sl']:.3f}")
                send_telegram(hold_msg)
                append_txt_log(f"{symbol} | HOLD | {existing['side']} | Entry {price:.3f}")
                buffer_log_csv("HOLD", symbol, existing['side'], price, price, existing['sl'], "", "HOLD update")
                cooldowns[symbol] = now
                return
            # opposite -> REVERSE: remove and create new
            else:
                old_side = existing['side']
                active_signals.remove(existing)
                rev_msg = (f"🔄 REVERSE ENTRY {symbol} | {emoji} {direction} | Entry {price:.3f} "
                           f"| TP1: {tp1:.3f} | TP2: {tp2:.3f} | TP3: {tp3:.3f} | SL: {sl:.3f}")
                send_telegram(rev_msg)
                append_txt_log(f"{symbol} | REVERSE | {old_side} -> {direction} | Entry {price:.3f}")
                buffer_log_csv("REVERSE", symbol, direction, price, price, sl, "", f"Replaced {old_side}->{direction}")
                # continue to add new

        # NEW signal
        if cond_up or cond_down:
            cooldowns[symbol] = now
            new_msg = (f"{emoji} <b><u>{direction} SIGNAL</u></b> - <b>{symbol}</b>\n"
                       f"Price: <b>{price:.3f}</b>\nSL: <b>{sl:.3f}</b>\nSize: <b>{size}</b>\n"
                       f"🎯 TP1: {tp1:.3f} | TP2: {tp2:.3f} | TP3: {tp3:.3f}\n"
                       f"📊 Sinyal: <b>KUAT</b> {strength_emoji}\n🔁 Trailing aktif setelah TP1")
            send_telegram(new_msg)
            append_txt_log(f"{symbol} | NEW | {direction} | Entry {price:.3f} | SL {sl:.3f}")
            buffer_log_csv("NEW", symbol, direction, price, price, sl, "", "NEW signal")
            active_signals.append({
                "symbol": symbol, "side": direction, "entry": price,
                "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl,
                "trailing_active": False, "notified_tp1": False, "notified_tp2": False, "notified_tp3": False
            })

    except Exception as e:
        print(f"check_signal error {symbol}: {e}")

# ========= monitor_active_signals (TP/SL notifications + CSV logging) =========
def monitor_active_signals():
    try:
        for signal in active_signals[:]:
            symbol = signal['symbol']
            entry_price = signal['entry']
            side = signal['side']
            side_emoji = "🚀" if side=="LONG" else "🔻"
            klines = client.futures_klines(symbol=symbol, interval='1m', limit=2)
            last_price = float(klines[-1][4])

            # Stop Loss
            if (side=='LONG' and last_price <= signal['sl']) or (side=='SHORT' and last_price >= signal['sl']):
                send_telegram(f"❌ {symbol} | STOP LOSS 💀 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                append_txt_log(f"{symbol} | SL Hit | {last_price:.3f}")
                buffer_log_csv("SL_HIT", symbol, side, signal.get("entry",""), last_price, signal.get("sl",""), "SL", "")
                active_signals.remove(signal)
                continue

            # TP levels
            if side == 'LONG':
                if not signal.get('notified_tp1') and last_price >= signal['tp1']:
                    send_telegram(f"✅ {symbol} | TP1 🎯 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp1'] = True; signal['trailing_active'] = True; signal['sl'] = entry_price
                    append_txt_log(f"{symbol} | TP1 Hit | {last_price:.3f}"); buffer_log_csv("TP_HIT", symbol, side, entry_price, last_price, signal.get("sl",""), "TP1", "")
                if not signal.get('notified_tp2') and last_price >= signal['tp2']:
                    send_telegram(f"🏅 {symbol} | TP2 🥈 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp2'] = True; signal['sl'] = signal['tp1']
                    append_txt_log(f"{symbol} | TP2 Hit | {last_price:.3f}"); buffer_log_csv("TP_HIT", symbol, side, entry_price, last_price, signal.get("sl",""), "TP2", "")
                if not signal.get('notified_tp3') and last_price >= signal['tp3']:
                    send_telegram(f"🏆 {symbol} | TP3 🥇 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp3'] = True; append_txt_log(f"{symbol} | TP3 Hit | {last_price:.3f}"); buffer_log_csv("TP_HIT", symbol, side, entry_price, last_price, signal.get("sl",""), "TP3", "")
                    active_signals.remove(signal)
            else:  # SHORT
                if not signal.get('notified_tp1') and last_price <= signal['tp1']:
                    send_telegram(f"✅ {symbol} | TP1 🎯 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp1'] = True; signal['trailing_active'] = True; signal['sl'] = entry_price
                    append_txt_log(f"{symbol} | TP1 Hit | {last_price:.3f}"); buffer_log_csv("TP_HIT", symbol, side, entry_price, last_price, signal.get("sl",""), "TP1", "")
                if not signal.get('notified_tp2') and last_price <= signal['tp2']:
                    send_telegram(f"🏅 {symbol} | TP2 🥈 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp2'] = True; signal['sl'] = signal['tp1']
                    append_txt_log(f"{symbol} | TP2 Hit | {last_price:.3f}"); buffer_log_csv("TP_HIT", symbol, side, entry_price, last_price, signal.get("sl",""), "TP2", "")
                if not signal.get('notified_tp3') and last_price <= signal['tp3']:
                    send_telegram(f"🏆 {symbol} | TP3 🥇 | {last_price:.3f} | Entry {entry_price:.3f} | {side_emoji} {side}")
                    signal['notified_tp3'] = True; append_txt_log(f"{symbol} | TP3 Hit | {last_price:.3f}"); buffer_log_csv("TP_HIT", symbol, side, entry_price, last_price, signal.get("sl",""), "TP3", "")
                    active_signals.remove(signal)
    except Exception as e:
        print("monitor error:", e)

# ========= MAIN LOOP =========
if __name__ == "__main__":
    last_flush = time.time()
    while True:
        try:
            client.futures_ping()
            symbols = filter_symbols(get_all_usdt_futures_symbols())
            for sym in symbols[:SCAN_LIMIT]:
                check_signal(sym)
                time.sleep(0.15)  # small pause to reduce rate pressure
            monitor_active_signals()
            # flush buffer periodically
            if time.time() - last_flush >= LOG_FLUSH_SECONDS:
                flush_log_buffer()
                last_flush = time.time()
            time.sleep(30)  # main sleep interval (adjust as needed)
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(5)
