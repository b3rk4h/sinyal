ow'] = df['low'].astype(float)
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

        cond_up = (
            df_1m.iloc[-1]['trend_up'] and
            df_5m.iloc[-1]['trend_up'] and
            df_15m.iloc[-1]['trend_up'] and
            df_1h.iloc[-1]['trend_up'] and
            df_1m.iloc[-1]['breakout_up'] and
            df_1m.iloc[-1]['volume_spike'] and
            df_1m.iloc[-1]['strong_adx']
        )

        cond_down = (
            not df_1m.iloc[-1]['trend_up'] and
            not df_5m.iloc[-1]['trend_up'] and
            not df_15m.iloc[-1]['trend_up'] and
            not df_1h.iloc[-1]['trend_up'] and
            df_1m.iloc[-1]['breakout_down'] and
            df_1m.iloc[-1]['volume_spike'] and
            df_1m.iloc[-1]['strong_adx']
        )

        price = df_1m.iloc[-1]['close']
        atr = df_1m.iloc[-1]['atr']
        risk_dollar = MODAL_TOTAL * RISK_PER_TRADE
		sl = price - atr if cond_up else price + atr
		price_sl_diff = abs(price - sl)

		if price_sl_diff == 0:
			print(f"[WARNING] {symbol} - SL sama dengan entry price! Size diset ke 0 untuk hindari error.")
			size = 0
		else:
			size = round((risk_dollar / price_sl_diff) * LEVERAGE, 2)


        if cond_up or cond_down:
            cooldowns[symbol] = now

        if cond_up:
            tp1 = price * (1 + TP1_PCT)
            tp2 = price * (1 + TP2_PCT)
            tp3 = price * (1 + TP3_PCT)
            msg = (
                f"\n🚀 <b><u>LONG SIGNAL</u></b> - <b>{symbol}</b>\n"
                f"Price: <b>{price:.2f}</b>\nSL: <b>{sl:.2f}</b>\nSize: <b>{size}</b>\n"
                f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n📈 Trend: Bullish\n"
                f"📊 Konfirmasi: ✅✅✅✅\n🎯 Sinyal: <b>KUAT</b> 🔥🔥🔥\n"
                f"🔁 Trailing aktif setelah TP1"
            )
            send_telegram(msg)

        elif cond_down:
            tp1 = price * (1 - TP1_PCT)
            tp2 = price * (1 - TP2_PCT)
            tp3 = price * (1 - TP3_PCT)
            msg = (
                f"\n🔻 <b><u>SHORT SIGNAL</u></b> - <b>{symbol}</b>\n"
                f"Price: <b>{price:.2f}</b>\nSL: <b>{sl:.2f}</b>\nSize: <b>{size}</b>\n"
                f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f} | TP3: {tp3:.2f}\n📉 Trend: Bearish\n"
                f"📊 Konfirmasi: ✅✅✅✅\n🎯 Sinyal: <b>KUAT</b> ❄️❄️❄️\n"
                f"🔁 Trailing aktif setelah TP1"
            )
            send_telegram(msg)

        with open("log_sinyal.txt", "a") as f:
            f.write(f"{datetime.now()} | {symbol} | {'LONG' if cond_up else 'SHORT'} | {price:.2f} | SL: {sl:.2f} | Size: {size}\n")

    except Exception as e:
        print(f"Error {symbol}: {e}")

# Loop utama
while True:
    try:
        client.futures_ping()
        symbols = get_all_usdt_futures_symbols()
        sampled = symbols[:30]  # Batasi simbol agar tidak overload
        for sym in sampled:
            check_signal(sym)
        time.sleep(60)
    except Exception as err:
        print(f"Main loop error: {err}")
        time.sleep(60)
