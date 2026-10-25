from dataclasses import dataclass
from pathlib import Path
import hashlib
import pandas as pd
import numpy as np
import yfinance as yf
from strategy import TICKERS, target_weights
from .calendar import calendar


@dataclass
class Signal:
    date: pd.Timestamp
    weights: dict
    prices: dict
    detail: pd.DataFrame
    digest: str


def make_signal(date, frame):
    date = pd.Timestamp(date)
    total = frame['Adj Close'].reindex(columns=TICKERS).loc[:date]
    close = frame['Close'].reindex(columns=TICKERS).loc[:date]
    start = date - pd.DateOffset(months=13)
    expected = calendar(date.year).sessions_in_range(start, date)
    total, close = total.reindex(expected), close.reindex(expected)
    for data in [total, close]:
        if data.empty or not np.isfinite(data.to_numpy()).all() or (data <= 0).any().any():
            raise ValueError('Missing, nonfinite or nonpositive prices; complete exchange sessions required')
    if len(total) < 252 or total.index[-1] != date:
        raise ValueError('Insufficient warm-up or stale month-end data')
    weights, detail = target_weights(total, close, date)
    digest = hashlib.sha256((total.to_csv() + close.to_csv()).encode()).hexdigest()
    return Signal(date, weights.to_dict(), close.iloc[-1].to_dict(), detail, digest)


def load_signal(date, directory, prices_csv=''):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if prices_csv:
        data = pd.read_csv(prices_csv, header=[0, 1], index_col=0, parse_dates=True)
    else:
        yf.set_tz_cache_location(str(directory / 'yfinance-cache'))
        date = pd.Timestamp(date)
        data = yf.download(TICKERS, start=str((date-pd.DateOffset(months=13)-pd.Timedelta(days=5)).date()),
                           end=str((date+pd.Timedelta(days=1)).date()), auto_adjust=False,
                           actions=True, threads=False, progress=False)
        if data.empty:
            raise ValueError('No market data downloaded')
    signal = make_signal(date, data)
    # Preserve the exact source used, including dividends and splits when provided.
    data.loc[:date].to_csv(directory / f'prices-{signal.date.date()}-{signal.digest[:12]}.csv')
    return signal
