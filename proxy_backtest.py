"""Twenty-year proxy extension. Never presents backfilled history as live ETF returns."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import numpy as np
import pandas as pd
os.environ.setdefault('MPLCONFIGDIR', '/tmp/etf-matplotlib')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import yfinance as yf
from strategy import TICKERS, backtest, metrics

ROOT = Path(__file__).resolve().parent
SYMBOLS = TICKERS + ['SPY', 'VGTSX', 'GLD', 'BIL', 'SHV', '^IRX']
PERIODS = {
    'financial_crisis': ('2007-10-01', '2009-06-30'),
    'low_rates': ('2009-07-01', '2015-12-31'),
    'pandemic_crash': ('2020-02-19', '2020-03-23'),
    'pandemic_recovery': ('2020-03-24', '2021-12-31'),
    'rate_hikes_2022': ('2022-01-01', '2022-12-31'),
}


def splice(proxy: pd.Series, actual: pd.Series) -> pd.Series:
    """Scale proxy to actual on inception date. No cross-fund price jump.

    The inception-date return uses the proxy; subsequent returns use actual ETF.
    Applied independently to total-return and split-adjusted price histories.
    """
    actual = actual.dropna()
    anchor = actual.index[0]
    if anchor not in proxy.index or pd.isna(proxy.loc[anchor]):
        raise ValueError(f'Proxy missing anchor {anchor}')
    if actual.iloc[0] <= 0 or proxy.loc[anchor] <= 0:
        raise ValueError('Splice requires positive prices')
    before = proxy.loc[proxy.index < anchor] * actual.iloc[0] / proxy.loc[anchor]
    return pd.concat([before, actual]).sort_index()


def bill_model(rates: pd.Series, calendar: pd.DatetimeIndex, fee=.0009) -> pd.Series:
    """Approximate 91-day T-bill carry, not a Treasury total-return index.

    ^IRX is annual bank-discount yield in percentage points, actual/360 basis.
    Convert price discount to 91-day holding return; compound over elapsed days.
    Use only previous trading close's known rate; fee is annualized actual/365.25.
    Does not model mark-to-market/duration or changing bill maturity. Negative
    observed rates are preserved. Missing rate dates carry the prior observation.
    """
    known = rates.reindex(calendar.union(rates.index)).sort_index().ffill().reindex(calendar)
    if known.isna().any():
        raise ValueError('Missing initial T-bill rate')
    gaps = calendar.to_series().diff().dt.days.fillna(0)
    discount = known.shift(1) / 100
    factors = (1 / (1 - discount * 91 / 360)) ** (gaps / 91)
    factors *= np.exp(-fee * gaps / 365.25)
    factors.iloc[0] = 1
    return factors.cumprod()


def load_inputs(directory):
    inputs = {}
    for symbol in SYMBOLS:
        path = directory / (symbol.replace('^', '') + '.csv')
        frame = pd.read_csv(path, header=[0, 1], index_col=0, parse_dates=True)
        if frame.empty or not frame.index.is_unique:
            raise ValueError(f'Invalid cache: {symbol}')
        inputs[symbol] = frame.xs(symbol, axis=1, level=1).sort_index()
    return inputs


def build_histories(inputs, cash_proxy='BIL'):
    calendar = inputs['QQQ'].index
    actual = {t: inputs[t]['Adj Close'] for t in TICKERS}
    close = {t: inputs[t]['Close'] for t in TICKERS}
    mapping = {'VOO': 'SPY', 'VXUS': 'VGTSX', 'GLDM': 'GLD'}
    log = []
    for ticker, proxy in mapping.items():
        actual[ticker] = splice(inputs[proxy]['Adj Close'], actual[ticker])
        close[ticker] = splice(inputs[proxy]['Close'], close[ticker])
        log.append({'ticker': ticker, 'proxy': proxy,
                    'actual_from': str(inputs[ticker].index[0].date())})
    cash = bill_model(inputs['^IRX']['Close'], calendar)
    if cash_proxy != 'IRX':
        cash = splice(cash, inputs[cash_proxy]['Adj Close'])
        cash_transition = str(inputs[cash_proxy].index[0].date())
    else:
        cash_transition = None
    actual['SGOV'] = splice(cash, actual['SGOV'])
    # SGOV is not a trend-filter candidate, so a continuous cash index suffices.
    close['SGOV'] = actual['SGOV'].copy()
    log.append({'ticker': 'SGOV', 'proxy': cash_proxy,
                'rate_model_until': cash_transition,
                'actual_from': str(inputs['SGOV'].index[0].date())})
    total = pd.DataFrame(actual).reindex(calendar)[TICKERS]
    prices = pd.DataFrame(close).reindex(calendar)[TICKERS]
    if total.isna().any().any() or prices.isna().any().any():
        raise ValueError('Missing stitched prices; no risk-asset forward fill permitted')
    return total, prices, log


def report_metrics(curve):
    rows = []
    rf = curve.SGOV.pct_change().fillna(0)
    for name in ['nav', 'VOO', 'SGOV', '70_30']:
        m = metrics(curve[name])
        daily = curve[name].pct_change()
        daily.iloc[0] = curve[name].iloc[0] - 1
        excess = daily - rf
        m['sharpe_over_cash'] = excess.mean() / excess.std() * np.sqrt(252) if excess.std() > 1e-12 else np.nan
        rows.append({'portfolio': name, **m})
    return pd.DataFrame(rows).set_index('portfolio')


def benchmark_70_30(total, active, cost_bps):
    """Monthly first-session close rebalance, same cost convention as strategy."""
    holdings = pd.Series({'VOO': .7, 'SGOV': .3})
    values = []
    returns = total[['VOO', 'SGOV']].pct_change()
    for i, date in enumerate(active):
        if i:
            holdings *= 1 + returns.loc[date]
        if i == 0 or date.month != active[i-1].month or date.year != active[i-1].year:
            nav = holdings.sum()
            turnover = 1. if i == 0 else (holdings/nav - pd.Series({'VOO': .7, 'SGOV': .3})).abs().sum()
            # Initial funding is SGOV: 70% sale + 70% purchase = 140% traded.
            if i == 0:
                turnover = 1.4
            holdings = pd.Series({'VOO': .7, 'SGOV': .3}) * nav * (1 - turnover * cost_bps / 10000)
        values.append(holdings.sum())
    return pd.Series(values, index=active)


def window_results(curve):
    rows = []
    for label, (start, end) in PERIODS.items():
        window = curve.loc[start:end, ['nav', 'VOO', 'SGOV', '70_30']]
        if window.empty or curve.loc[curve.index < window.index[0]].empty:
            continue
        before = curve.loc[curve.index < window.index[0]].iloc[-1]
        normalized = window / before[window.columns]
        for portfolio in window:
            m = metrics(normalized[portfolio])
            rows.append({'period': label, 'start': str(window.index[0].date()),
                         'end': str(window.index[-1].date()), 'portfolio': portfolio,
                         'total_return': m['total_return'], 'max_drawdown': m['max_drawdown']})
    return pd.DataFrame(rows)


def run(offline=False, start='2006-10-25', end='2026-10-25', cost_bps=10.):
    directory = ROOT / 'data/proxy20'
    output = ROOT / 'results/proxy20'
    directory.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if not offline:
        yf.set_tz_cache_location(str(ROOT / 'data/yfinance-cache'))
        for symbol in SYMBOLS:
            frame = yf.download(symbol, start='2005-09-01', end=str((pd.Timestamp(end) + pd.Timedelta(days=1)).date()),
                                auto_adjust=False, actions=True, threads=False, progress=False)
            if frame.empty:
                raise ValueError(f'No data downloaded for {symbol}')
            frame.to_csv(directory / (symbol.replace('^', '') + '.csv'))
    inputs = load_inputs(directory)
    if inputs['QQQ'].index[-1] < pd.Timestamp(end) - pd.Timedelta(days=7):
        raise ValueError('Stale input data')
    summaries, manifest, main_curve = [], [], None
    for proxy in ['BIL', 'SHV', 'IRX']:
        total, close, log = build_histories(inputs, proxy)
        curve, trades, signals = backtest(total, close, start, end, cost_bps)
        curve['70_30'] = benchmark_70_30(total, curve.index, cost_bps)
        summary = report_metrics(curve)
        summary['cash_proxy'] = proxy
        summaries.append(summary.reset_index())
        folder = output / proxy
        folder.mkdir(exist_ok=True)
        curve.to_csv(folder / 'equity.csv')
        trades.to_csv(folder / 'rebalances.csv', index=False)
        signals.to_csv(folder / 'signals.csv', index=False)
        summary.to_csv(folder / 'summary.csv')
        total.to_csv(folder / 'stitched_total_return.csv')
        close.to_csv(folder / 'stitched_price.csv')
        manifest.append({'cash_proxy': proxy, 'rebalance_count': len(trades), 'proxies': log})
        if proxy == 'BIL':
            main_curve = curve
            cost_rows = []
            for bps in [0., 10., 25.]:
                cost_curve, _, _ = backtest(total, close, start, end, bps)
                cost_rows.append({'cost_bps': bps, **metrics(cost_curve.nav)})
            pd.DataFrame(cost_rows).to_csv(output / 'cost_sensitivity.csv', index=False)
            rolling_rows = []
            end_dates = curve.resample('YE').last().index
            end_dates = end_dates[end_dates <= curve.index[-1]].union(pd.DatetimeIndex([curve.index[-1]]))
            for stop in end_dates:
                begin = stop - pd.DateOffset(years=5)
                if begin < curve.index[0]:
                    continue
                window = curve.loc[begin:stop, ['nav', 'VOO', 'SGOV', '70_30']]
                before = curve.loc[curve.index < window.index[0]].iloc[-1]
                for portfolio in window:
                    m = metrics(window[portfolio] / before[portfolio])
                    rolling_rows.append({'start': str(window.index[0].date()), 'end': str(window.index[-1].date()),
                                         'portfolio': portfolio, **m})
            pd.DataFrame(rolling_rows).to_csv(output / 'rolling_five_years.csv', index=False)
            window_results(curve).to_csv(output / 'regimes.csv', index=False)
            baseline = pd.DataFrame([[1.]*4], columns=['nav', 'VOO', 'SGOV', '70_30'], index=[curve.index[0]-pd.Timedelta(days=1)])
            annual = pd.concat([baseline, curve[baseline.columns].resample('YE').last()]).pct_change().iloc[1:]
            annual.to_csv(output / 'annual_returns.csv')
    combined = pd.concat(summaries, ignore_index=True)
    combined.to_csv(output / 'sensitivity.csv', index=False)
    fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
    labels = {'nav': 'Rotation', 'VOO': 'US equity proxy', 'SGOV': 'Cash proxy', '70_30': '70/30'}
    colors = {'Rotation': '#1f77b4', 'US equity proxy': '#ff7f0e', 'Cash proxy': '#2ca02c', '70/30': '#d62728'}
    main_curve[['nav', 'VOO', 'SGOV', '70_30']].rename(columns=labels).plot(ax=axes[0], logy=True, color=colors)
    axes[0].set(ylabel='Growth of $1 (log scale)', title='20-year proxy extension: not actual ETF history')
    drawdown = main_curve[['nav', 'VOO', '70_30']]
    (drawdown / drawdown.cummax().clip(lower=1) - 1).rename(columns=labels).plot(ax=axes[1], color=colors)
    axes[1].set(ylabel='Drawdown')
    fig.tight_layout()
    fig.savefig(output / 'equity_drawdown.png', dpi=160)
    plt.close(fig)
    metadata = {'requested_start': start, 'requested_end': end,
                'actual_start': str(main_curve.index[0].date()), 'actual_end': str(main_curve.index[-1].date()),
                'evaluated_at_utc': str(pd.Timestamp.now(tz='UTC')), 'cost_bps': cost_bps,
                'description': 'Proxy extension; pre-inception history is NOT the named ETF actual return',
                'rate_model_annual_fee': .0009, 'variants': manifest,
                'inputs': {p.name: {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                                   'file_modified_at_utc': str(pd.Timestamp(p.stat().st_mtime, unit='s', tz='UTC'))}
                           for p in sorted(directory.glob('*.csv'))}}
    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    print(combined.to_string(index=False))
    print(pd.read_csv(output / 'regimes.csv').to_string(index=False))
    return combined


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--start', default='2006-10-25')
    parser.add_argument('--end', default='2026-10-25')
    parser.add_argument('--cost-bps', default=10., type=float)
    args = parser.parse_args()
    run(**vars(args))
