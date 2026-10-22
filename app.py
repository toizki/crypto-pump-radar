import time
from datetime import datetime, timezone
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BASE = 'https://api.mexc.com'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'MEXC-Pump-Radar/1.0'})
st.set_page_config(page_title='MEXC Pump Radar', page_icon='📡', layout='wide')

@st.cache_data(ttl=120, show_spinner=False)
def symbols():
    r = SESSION.get(f'{BASE}/api/v3/exchangeInfo', timeout=18)
    r.raise_for_status()
    return {s['symbol'] for s in r.json().get('symbols', []) if s.get('quoteAsset') == 'USDT' and s.get('status') == '1' and s.get('isSpotTradingAllowed', True)}

@st.cache_data(ttl=25, show_spinner=False)
def tickers():
    r = SESSION.get(f'{BASE}/api/v3/ticker/24hr', timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else [data]

@st.cache_data(ttl=45, show_spinner=False)
def candles(symbol):
    r = SESSION.get(f'{BASE}/api/v3/klines', params={'symbol': symbol, 'interval': '1m', 'limit': 61}, timeout=12)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or len(data) < 21:
        return None
    return data

def num(x):
    try: return float(x)
    except (ValueError, TypeError): return 0.0

def evaluate(symbol, rows, day_change, quote_vol, max_extension):
    # Ignore latest unfinished candle to avoid overstating signals.
    rows = rows[:-1]
    if len(rows) < 20: return None
    closes = [num(x[4]) for x in rows]
    highs = [num(x[2]) for x in rows]
    vols = [num(x[7]) for x in rows]  # quote asset volume
    buys = [num(x[9]) for x in rows] if len(rows[0]) > 9 else []
    if not closes[-1] or not closes[-6]: return None
    baseline = sum(vols[-16:-1]) / 15
    spike = vols[-1] / baseline if baseline > 0 else 0
    mom5 = (closes[-1] / closes[-6] - 1) * 100
    prior_high = max(highs[-16:-1])
    breakout = closes[-1] > prior_high
    pressure = buys[-1] / vols[-1] if buys and vols[-1] else None
    # This is a heuristic strength score, NOT a probability or a prediction.
    score = min(spike / 4, 1) * 35
    score += max(0, min(mom5 / 3, 1)) * 25
    score += 20 if breakout else 0
    score += max(0, min(((pressure or .5) - .5) / .25, 1)) * 20
    score = round(score)
    warnings = []
    if mom5 > max_extension: warnings.append('Overextended 5m move')
    if day_change > 40: warnings.append('Large 24h move')
    if pressure is None: warnings.append('Buy volume unavailable')
    return {'Coin': symbol, 'Score': score, 'Price (USDT)': closes[-1], '5m %': round(mom5, 2),
            '24h %': round(day_change, 2), 'Volume spike': round(spike, 2),
            'Buy share %': round(pressure * 100, 1) if pressure is not None else None,
            'Breakout': 'Yes' if breakout else 'No', '24h volume USDT': round(quote_vol),
            'Warnings': ', '.join(warnings) or '—'}

st.title('📡 MEXC Pump Radar')
st.caption('Spot USDT pairs · Public MEXC API · Research signals only, not trading advice')
with st.sidebar:
    st.header('Scanner settings')
    refresh = st.selectbox('Refresh dashboard', [30, 60, 120, 300], index=1, format_func=lambda x: f'{x} seconds')
    top = st.slider('Highest-volume pairs to analyze', 10, 100, 35, 5)
    min_volume = st.number_input('Minimum 24h USDT volume', min_value=0, value=250000, step=50000)
    threshold = st.slider('Minimum signal score', 0, 100, 50, 5)
    max_extension = st.slider('5-minute overextension warning above (%)', 2, 30, 10)
    st.info('This dashboard refreshes only while open. Always-on notifications require a separate hosted worker.')
st_autorefresh(interval=refresh * 1000, key='radar_refresh')

try:
    allowed = symbols()
    raw = tickers()
    market = []
    for x in raw:
        sym = x.get('symbol', '')
        vol = num(x.get('quoteVolume'))
        if sym in allowed and vol >= min_volume:
            market.append((sym, num(x.get('priceChangePercent')), vol))
    market.sort(key=lambda x: x[2], reverse=True)
    selected = market[:top]
    results, errors = [], 0
    progress = st.progress(0, text='Checking minute candles…')
    for i, (sym, change, volume) in enumerate(selected):
        try:
            data = candles(sym)
            if data:
                row = evaluate(sym, data, change, volume, max_extension)
                if row: results.append(row)
        except (requests.RequestException, ValueError, IndexError):
            errors += 1
        progress.progress((i + 1) / len(selected), text=f'Analyzed {i+1}/{len(selected)} pairs')
    progress.empty()
    st.caption(f'Last scan: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")} · {len(results)} analyzed · {errors} unavailable')
    if not results:
        st.warning('No results. Try lowering minimum volume or check MEXC availability from your hosting region.')
    else:
        df = pd.DataFrame(results).sort_values(['Score', '24h volume USDT'], ascending=False)
        a, b, c = st.columns(3)
        a.metric('Pairs analyzed', len(df))
        b.metric('Signals above threshold', int((df['Score'] >= threshold).sum()))
        c.metric('Top signal score', int(df['Score'].max()))
        st.subheader('🚨 Strongest signals')
        strong = df[df['Score'] >= threshold]
        st.dataframe(strong, use_container_width=True, hide_index=True)
        st.subheader('All scanned pairs')
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button('Download scan as CSV', df.to_csv(index=False), 'mexc_radar_scan.csv', 'text/csv')
        st.caption('Score weights: volume spike 35, 5m momentum 25, recent-high breakout 20, taker-buy share 20. Uses last completed 1m candle; sample is limited to top-volume pairs. A score of 80 does not mean an 80% chance of a pump.')
        st.warning('Crypto can move sharply in either direction. Exchange data can be delayed or unavailable, and unusual volume may reflect manipulation. Backtest and paper-trade before risking funds.')
except requests.HTTPError as e:
    st.error(f'MEXC API HTTP error: {e}. The API may be restricted or temporarily unavailable.')
except requests.RequestException as e:
    st.error(f'Unable to reach MEXC: {e}')
