import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

BASE = 'https://api.binance.com'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'CryptoPumpRadar/1.0'})

st.set_page_config(page_title='Crypto Pump Radar', page_icon='📡', layout='wide')
st.title('📡 Crypto Pump Radar')
st.caption('Binance spot USDT pairs • Signal detection, not price prediction or financial advice')


def get_json(path, params=None):
    response = SESSION.get(BASE + path, params=params, timeout=12)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=55)
def universe(min_turnover, limit):
    tickers = get_json('/api/v3/ticker/24hr')
    excluded = ('UPUSDT', 'DOWNUSDT', 'BULLUSDT', 'BEARUSDT')
    stable = {'USDC', 'FDUSD', 'TUSD', 'USDP', 'DAI', 'EUR', 'EURC', 'TRY', 'BRL'}
    rows = []
    for t in tickers:
        symbol = t.get('symbol', '')
        if not symbol.endswith('USDT') or symbol.endswith(excluded):
            continue
        base = symbol[:-4]
        if base in stable:
            continue
        turnover = float(t.get('quoteVolume', 0))
        if turnover >= min_turnover:
            rows.append((symbol, turnover))
    return [symbol for symbol, _ in sorted(rows, key=lambda x: x[1], reverse=True)[:limit]]


def analyze(symbol):
    candles = get_json('/api/v3/klines', {'symbol': symbol, 'interval': '5m', 'limit': 48})
    if len(candles) < 26:
        return None
    closes = [float(x[4]) for x in candles]
    volumes = [float(x[7]) for x in candles]  # Quote asset volume, in USDT
    taker = [float(x[10]) for x in candles]  # Taker-buy quote volume
    last, prev = closes[-1], closes[-2]
    recent_vol = sum(volumes[-3:]) / 3
    baseline_vol = sum(volumes[-27:-3]) / 24
    spike = recent_vol / baseline_vol if baseline_vol else 0
    momentum = (last / closes[-4] - 1) * 100 if closes[-4] else 0
    change_1h = (last / closes[-13] - 1) * 100 if closes[-13] else 0
    prior_high = max(float(x[2]) for x in candles[-25:-1])
    breakout = last > prior_high
    taker_ratio = sum(taker[-3:]) / max(sum(volumes[-3:]), 1)
    # Heuristic signal strength: NOT a measured probability of a pump.
    score = 0
    score += min(max((spike - 1) * 18, 0), 30)
    score += min(max(momentum * 6, 0), 25)
    score += 20 if breakout else 0
    score += min(max((taker_ratio - .5) * 120, 0), 15)
    score += 10 if 0 < change_1h < 8 else 0
    if change_1h > 12:
        score -= 25  # Avoid late, extended entries
    return {
        'Coin': symbol, 'Score': round(max(0, min(100, score))),
        'Price (USDT)': last, '15m momentum %': round(momentum, 2),
        '1h change %': round(change_1h, 2), 'Volume spike': round(spike, 2),
        'Buy pressure %': round(taker_ratio * 100, 1),
        'Breakout': breakout, 'Last candle UTC': datetime.fromtimestamp(candles[-1][0] / 1000, timezone.utc).strftime('%H:%M'),
    }


def telegram_alert(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        return False
    response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                             json={'chat_id': chat_id, 'text': message}, timeout=10)
    response.raise_for_status()
    return True


with st.sidebar:
    st.header('Scanner settings')
    min_turnover = st.number_input('Minimum 24h turnover (USDT)', min_value=100000, value=2000000, step=500000)
    limit = st.slider('Top liquid coins to scan', 10, 150, 60, 10)
    threshold = st.slider('Alert score threshold', 40, 95, 75, 5)
    refresh = st.selectbox('Auto refresh', ['Off', '60 seconds', '120 seconds'], index=1)
    st.caption('Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as environment variables. Alerts are sent only on a new threshold crossing during this app session.')

if 'alerted' not in st.session_state:
    st.session_state.alerted = set()

try:
    symbols = universe(min_turnover, limit)
    results = []
    with st.spinner(f'Checking {len(symbols)} markets...'):
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = {pool.submit(analyze, symbol): symbol for symbol in symbols}
            for job in as_completed(jobs):
                try:
                    result = job.result()
                    if result:
                        results.append(result)
                except (requests.RequestException, ValueError, IndexError, ZeroDivisionError):
                    pass
    if not results:
        st.warning('No market data returned. Binance may be unavailable or restricted in your region.')
    else:
        df = pd.DataFrame(results).sort_values(['Score', 'Volume spike'], ascending=False)
        top = df.iloc[0]
        a, b, c = st.columns(3)
        a.metric('Markets analyzed', len(df))
        b.metric('Signals above threshold', int((df['Score'] >= threshold).sum()))
        c.metric('Top signal', f"{top['Coin']} ({top['Score']}/100)")
        st.subheader('Ranked early-momentum signals')
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.subheader('Highest-scoring coins')
        st.plotly_chart(px.bar(df.head(15), x='Coin', y='Score', hover_data=['Volume spike', 'Buy pressure %', '1h change %']), use_container_width=True)
        current = set(df.loc[df['Score'] >= threshold, 'Coin'])
        new_signals = current - st.session_state.alerted
        for symbol in sorted(new_signals):
            row = df.loc[df['Coin'] == symbol].iloc[0]
            message = f"📡 Pump Radar signal: {symbol}\nScore: {row['Score']}/100\nVolume: {row['Volume spike']}x\n15m momentum: {row['15m momentum %']}%\nBuy pressure: {row['Buy pressure %']}%\nNot a buy recommendation."
            try:
                telegram_alert(message)
            except requests.RequestException:
                st.warning(f'Telegram delivery failed for {symbol}')
        st.session_state.alerted = current
        st.caption(f"Updated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC. Candles may be incomplete; this scanner uses 5-minute candles and scans periodically, not tick-by-tick.")
except requests.RequestException as exc:
    st.error(f'Exchange API error: {exc}')

if refresh != 'Off':
    seconds = 60 if refresh == '60 seconds' else 120
    time.sleep(seconds)
    st.rerun()
