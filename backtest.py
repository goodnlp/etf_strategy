"""Download auditable adjusted prices, run rotation, and export results."""
import argparse
import hashlib
import json
from pathlib import Path
import pandas as pd
import os
os.environ.setdefault('MPLCONFIGDIR', '/tmp/etf-matplotlib')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import yfinance as yf
from strategy import TICKERS, backtest, metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--end', default=str(pd.Timestamp.now(tz='Asia/Singapore').date()))
    parser.add_argument('--start')
    parser.add_argument('--cost-bps', type=float, default=10.)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    end = pd.Timestamp(args.end)
    start = pd.Timestamp(args.start) if args.start else end - pd.DateOffset(years=5)
    warmup = start - pd.DateOffset(months=13)
    Path('data').mkdir(exist_ok=True)
    Path('results').mkdir(exist_ok=True)
    cache = Path('data/prices.csv')
    if args.offline:
        data = pd.read_csv(cache, header=[0, 1], index_col=0, parse_dates=True)
    else:
        yf.set_tz_cache_location('/workspace/etf_strategy/data/yfinance-cache')
        data = yf.download(TICKERS, start=str(warmup.date()),
                           end=str((end + pd.Timedelta(days=1)).date()),
                           auto_adjust=False, actions=True, threads=False, progress=False)
        if data.empty:
            raise RuntimeError('No real market data downloaded; no synthetic results will be substituted')
        data.to_csv(cache)
    prices = data['Adj Close'][TICKERS]
    close = data['Close'][TICKERS]
    if prices.index[-1] < end - pd.Timedelta(days=7):
        raise RuntimeError(f'Stale data ending {prices.index[-1].date()}; requested {end.date()}')
    curve, trades, signals = backtest(prices, close, start, end, args.cost_bps)
    summary = pd.DataFrame({name: metrics(curve[name]) for name in ['nav', 'VOO', 'SGOV']}).T
    curve.to_csv('results/equity.csv')
    trades.to_csv('results/rebalances.csv', index=False)
    signals.to_csv('results/signals.csv', index=False)
    summary.to_csv('results/summary.csv')
    annual = curve[['nav', 'VOO', 'SGOV']].resample('YE').last()
    baseline = pd.DataFrame([[1., 1., 1.]], columns=['nav', 'VOO', 'SGOV'], index=[curve.index[0] - pd.Timedelta(days=1)])
    annual = pd.concat([baseline, annual]).pct_change().iloc[1:]
    annual.to_csv('results/annual_returns.csv')
    curve[['nav', 'VOO', 'SGOV']].plot(title='Monthly ETF rotation (total return, after trading costs)', figsize=(11, 6))
    plt.ylabel('Growth of 1 USD')
    plt.tight_layout()
    plt.savefig('results/equity.png', dpi=160)
    metadata = {'source': 'Yahoo Finance via yfinance; Adj Close includes dividends and splits',
                'requested_start': str(start.date()), 'requested_end': str(end.date()),
                'actual_start': str(curve.index[0].date()), 'actual_end': str(curve.index[-1].date()),
                'cost_bps_per_traded_dollar': args.cost_bps, 'rebalance_count': len(trades),
                'cache_modified_at_utc': str(pd.Timestamp(cache.stat().st_mtime, unit='s', tz='UTC')),
                'evaluated_at_utc': str(pd.Timestamp.now(tz='UTC')),
                'offline': args.offline,
                'prices_sha256': hashlib.sha256(cache.read_bytes()).hexdigest()}
    Path('results/metadata.json').write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, indent=2))
    print(summary.to_string(float_format=lambda x: f'{x:.4f}'))


if __name__ == '__main__':
    main()
