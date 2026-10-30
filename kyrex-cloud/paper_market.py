"""Public Kraken market reads only. No authentication, account or order API."""
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
import json
import time

import requests

PAIRS = {'BTC': ('XBTUSD', 'XXBTZUSD'), 'ETH': ('ETHUSD', 'XETHZUSD'),
         'SOL': ('SOLUSD', 'SOLUSD'), 'XRP': ('XRPUSD', 'XXRPZUSD'),
         'ADA': ('ADAUSD', 'ADAUSD'), 'DOGE': ('XDGUSD', 'XDGUSD'),
         'LINK': ('LINKUSD', 'LINKUSD'), 'AVAX': ('AVAXUSD', 'AVAXUSD')}
LEGACY_SYMBOLS = ('BTC', 'ETH')


def symbols(value):
    if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= len(PAIRS)
            or any(not isinstance(s, str) or s not in PAIRS for s in value)
            or len(set(value)) != len(value)):
        raise ValueError('Choose one to eight supported coins without duplicates.')
    return [s for s in PAIRS if s in value]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Invalid market number.')
    try: result = Decimal(str(value))
    except InvalidOperation: raise ValueError('Invalid market number.') from None
    if not result.is_finite() or not Decimal('0.00000001') <= result <= Decimal('1000000000'):
        raise ValueError('Market price is outside supported bounds.')
    return result


def public_read(endpoint, params):
    if endpoint not in {'Ticker', 'OHLC'}:
        raise ValueError('Only public ticker and candle reads are available.')
    # Fixed HTTPS origin, GET only, bounded response, no redirects/credentials.
    began = time.monotonic()
    with requests.get('https://api.kraken.com/0/public/' + endpoint, params=params,
                      headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'},
                      timeout=(5, 15), allow_redirects=False, stream=True) as response:
        if response.status_code != 200: raise ValueError('Public market data is unavailable.')
        chunks, size = [], 0
        for chunk in response.iter_content(16384):
            if time.monotonic() - began > 20: raise ValueError('Public market response was too slow.')
            size += len(chunk)
            if size > 512000: raise ValueError('Public market response is too large.')
            chunks.append(chunk)
    try: value = json.loads(b''.join(chunks))
    except (ValueError, TypeError): raise ValueError('Public market data is invalid.') from None
    if not isinstance(value, dict) or value.get('error') != [] or not isinstance(value.get('result'), dict):
        raise ValueError('Public market data is unavailable.')
    return value['result']


def quotes(selected=None):
    selected = symbols(list(PAIRS) if selected is None else selected)
    began = time.time()
    result = public_read('Ticker', {'pair': ','.join(PAIRS[s][0] for s in selected)})
    out = {}
    try:
        for symbol in selected:
            _, key = PAIRS[symbol]
            row = result[key]
            ask, bid = number(row['a'][0]), number(row['b'][0])
            if ask < bid or (ask - bid) / bid > Decimal('.01'):
                raise ValueError('Market spread is too wide or invalid.')
            out[symbol] = {'ask': str(ask), 'bid': str(bid)}
    except (KeyError, TypeError, IndexError): raise ValueError('Incomplete public market quote.') from None
    if time.time() - began > 60: raise ValueError('Public market read took too long.')
    return {'source': 'Kraken public ticker', 'retrieved_at': time.time(), 'prices': out}


def context(selected=None):
    selected = symbols(list(PAIRS) if selected is None else selected)
    now = time.time()
    def candles(symbol):
        pair, key = PAIRS[symbol]
        rows = public_read('OHLC', {'pair': pair, 'interval': 60}).get(key)
        if not isinstance(rows, list): raise ValueError('Public candles are unavailable.')
        closed = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 8 or type(row[0]) is not int:
                raise ValueError('Invalid public candle.')
            # Kraken's last candle is uncommitted: include completed hours only.
            if row[0] + 3600 <= now:
                closed.append({'time': row[0], 'close': str(number(row[4]))})
        closed = closed[-30:]
        if (len(closed) < 12 or closed[-1]['time'] + 3600 < now - 7200
                or any(a['time'] >= b['time'] for a, b in zip(closed, closed[1:]))):
            raise ValueError('Closed candle coverage is stale or insufficient.')
        return symbol, closed
    # Bounded parallel public reads keep a broader watchlist from serially
    # ageing its first quote. Fetch the shared quote snapshot after candles.
    with ThreadPoolExecutor(max_workers=4) as pool:
        history = dict(pool.map(candles, selected))
    snapshot = quotes(selected)
    for symbol in selected:
        snapshot['prices'][symbol]['closed_hourly_candles'] = history[symbol]
    if time.time() - snapshot['retrieved_at'] > 60:
        raise ValueError('Market context became stale.')
    return snapshot
