"""Monthly ETF momentum rotation. Signals use only information available at close."""
from __future__ import annotations
import numpy as np
import pandas as pd

TICKERS = ['QQQ', 'VOO', 'VTV', 'VXUS', 'GLDM', 'XLE', 'IEF', 'SGOV']
STOCKS = ['QQQ', 'VOO', 'VTV', 'VXUS', 'XLE']


def target_weights(total_return: pd.DataFrame, close: pd.DataFrame, date) -> tuple[pd.Series, pd.DataFrame]:
    """Calendar-month lookbacks; last available close on/before each lookback date."""
    history = total_return.loc[:date, TICKERS]
    prices = close.loc[:date, TICKERS]
    date = history.index[-1]
    score = pd.Series(0., index=TICKERS)
    for months, coefficient in [(3, .3), (6, .4), (12, .3)]:
        past = history.loc[:date - pd.DateOffset(months=months)]
        if past.empty:
            raise ValueError('At least twelve calendar months of warm-up data required')
        score += coefficient * (history.iloc[-1] / past.iloc[-1] - 1)
    sma = prices.tail(200).mean() if len(prices) >= 200 else pd.Series(np.nan, index=TICKERS)
    eligible = (score > score['SGOV']) & (prices.iloc[-1] > sma)
    eligible['SGOV'] = False
    selected = score[eligible].sort_values(ascending=False, kind='stable').head(3).index
    weights = pd.Series(0., index=TICKERS)
    weights.loc[selected] = .35
    if weights.sum() > 1:
        weights /= weights.sum()
    equity = weights[STOCKS].sum()
    if equity > .70:
        weights.loc[STOCKS] *= .70 / equity
    weights['SGOV'] = 1 - weights.drop('SGOV').sum()
    detail = pd.DataFrame({'score': score, 'close': prices.iloc[-1], 'sma200': sma,
                           'eligible': eligible, 'target_weight': weights})
    return weights, detail


def backtest(total_return, close, start, end, cost_bps=10.):
    """Next-session close execution; drift between trades; one-way traded notional cost.

    Caps apply to rebalance targets. Market moves can exceed caps between rebalances.
    Month ends are inferred from the next observed trading month, so an incomplete
    final month never creates a signal. Initial capital sits in SGOV until execution.
    """
    if cost_bps < 0:
        raise ValueError('cost_bps must be nonnegative')
    total_return = total_return[TICKERS].sort_index()
    close = close.reindex(index=total_return.index, columns=TICKERS)
    if total_return.isna().any().any() or close.isna().any().any():
        raise ValueError('Missing prices: no forward fill or silent asset removal allowed')
    if not total_return.index.is_unique or (total_return <= 0).any().any():
        raise ValueError('Prices must be positive with a unique date index')
    dates = total_return.index
    active = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    if len(active) < 2:
        raise ValueError('Insufficient backtest dates')
    month_ends = dates[:-1][dates[:-1].to_period('M') != dates[1:].to_period('M')]
    schedule, signals = {}, []
    # Include the preceding month end to establish the first executable target.
    for signal_date in month_ends:
        execution_date = dates[dates.get_loc(signal_date) + 1]
        if execution_date < active[0] or execution_date > active[-1]:
            continue
        weights, detail = target_weights(total_return, close, signal_date)
        schedule[execution_date] = weights
        detail['signal_date'], detail['execution_date'] = signal_date, execution_date
        signals.append(detail.rename_axis('ticker').reset_index())
    holdings = pd.Series(0., index=TICKERS)
    holdings['SGOV'] = 1.
    previous = 1.
    rows, trades = [], []
    returns = total_return.pct_change()
    for i, date in enumerate(active):
        # NAV starts at the first close; no return earned before initial valuation.
        if i:
            holdings *= 1 + returns.loc[date]
        turnover = fee = 0.
        if date in schedule:
            nav = holdings.sum()
            current = holdings / nav
            target = schedule[date]
            turnover = (target - current).abs().sum()
            fee = nav * turnover * cost_bps / 10000
            holdings = target * (nav - fee)
            trades.append({'execution_date': date, 'turnover': turnover, 'cost': fee,
                           **target.to_dict()})
        nav = holdings.sum()
        rows.append({'date': date, 'nav': nav, 'return': nav / previous - 1,
                     'turnover': turnover, 'cost': fee})
        previous = nav
    curve = pd.DataFrame(rows).set_index('date')
    for ticker in ['VOO', 'SGOV']:
        curve[ticker] = total_return.loc[active, ticker] / total_return.loc[active[0], ticker]
    return curve, pd.DataFrame(trades), pd.concat(signals, ignore_index=True) if signals else pd.DataFrame()


def metrics(nav):
    days = (nav.index[-1] - nav.index[0]).days
    daily = nav.pct_change()
    daily.iloc[0] = nav.iloc[0] - 1
    cagr = nav.iloc[-1] ** (365.25 / days) - 1
    vol = daily.std() * np.sqrt(252)
    return {'total_return': nav.iloc[-1] - 1, 'cagr': cagr,
            'annual_volatility': vol, 'max_drawdown': (nav / nav.cummax().clip(lower=1.) - 1).min(),
            'sharpe_rf0': daily.mean() / daily.std() * np.sqrt(252) if daily.std() else np.nan}
