import numpy as np
import pandas as pd
import pytest
from strategy import TICKERS, STOCKS, target_weights, backtest


def fixture(slopes):
    index = pd.bdate_range('2019-01-01', '2022-02-10')
    return pd.DataFrame({t: 100 * np.exp(np.arange(len(index)) * slopes.get(t, 0)) for t in TICKERS}, index=index)


def test_three_equities_cap_and_sgov_residual():
    p = fixture({'QQQ': .001, 'VOO': .0009, 'VTV': .0008})
    w, d = target_weights(p, p, '2021-12-31')
    assert w[STOCKS].sum() == pytest.approx(.7)
    assert w.SGOV == pytest.approx(.3)
    assert (w.drop('SGOV') <= .35).all()
    assert w.sum() == pytest.approx(1)


def test_no_eligible_asset_stays_in_sgov():
    p = fixture({'SGOV': .0001})
    w, _ = target_weights(p, p, '2021-12-31')
    assert w.SGOV == 1


def test_trend_filter_excludes_positive_momentum_after_crash():
    p = fixture({'QQQ': .001})
    close = p.copy()
    close.loc[close.index[-1], 'QQQ'] *= .5
    w, d = target_weights(p, close, p.index[-1])
    assert d.loc['QQQ', 'score'] > 0
    assert not d.loc['QQQ', 'eligible']
    assert w.QQQ == 0


def test_signals_do_not_use_future_prices():
    p = fixture({'QQQ': .001, 'GLDM': .0008, 'IEF': .0005})
    altered = p.copy()
    altered.loc['2022-01-01':, 'VOO'] *= 10
    a, _ = target_weights(p, p, '2021-12-31')
    b, _ = target_weights(altered, altered, '2021-12-31')
    pd.testing.assert_series_equal(a, b)


def test_next_close_execution_costs_and_drift():
    p = fixture({'QQQ': .001, 'GLDM': .0008, 'IEF': .0005})
    curve, trades, signals = backtest(p, p, '2021-01-01', '2022-02-10', 10)
    free, _, _ = backtest(p, p, '2021-01-01', '2022-02-10', 0)
    assert len(trades) == 14
    assert (pd.to_datetime(signals.execution_date) > pd.to_datetime(signals.signal_date)).all()
    assert trades.iloc[0].execution_date == pd.Timestamp('2021-01-01')
    assert trades.iloc[0]['cost'] == pytest.approx(.002)
    assert curve.nav.iloc[0] == pytest.approx(.998)
    assert curve.nav.iloc[-1] < free.nav.iloc[-1]
    # No same-day pre-execution gain: first day is charged trading cost only.
    assert curve['return'].iloc[0] == pytest.approx(-.002)


def test_missing_data_rejected():
    p = fixture({})
    p.iloc[300, 0] = np.nan
    with pytest.raises(ValueError, match='Missing prices'):
        backtest(p, p, '2021-01-01', '2022-02-10')


def test_calendar_month_score():
    p = fixture({'QQQ': .001})
    date = pd.Timestamp('2021-12-31')
    _, detail = target_weights(p, p, date)
    expected = sum(weight * (p.loc[date, 'QQQ'] / p.loc[:date-pd.DateOffset(months=n), 'QQQ'].iloc[-1] - 1)
                   for n, weight in [(3, .3), (6, .4), (12, .3)])
    assert detail.loc['QQQ', 'score'] == pytest.approx(expected)


def test_initial_trading_cost_in_metrics():
    from strategy import metrics
    nav = pd.Series([.998, .998], index=pd.to_datetime(['2021-01-01', '2022-01-01']))
    result = metrics(nav)
    assert result['total_return'] == pytest.approx(-.002)
    assert result['max_drawdown'] == pytest.approx(-.002)


def test_offline_cli_exports_in_temporary_directory(tmp_path, monkeypatch):
    from backtest import main
    p = fixture({'QQQ': .001, 'GLDM': .0008, 'IEF': .0005})
    (tmp_path / 'data').mkdir()
    pd.concat({'Adj Close': p, 'Close': p}, axis=1).to_csv(tmp_path / 'data/prices.csv')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('sys.argv', ['backtest.py', '--offline', '--start', '2021-01-01', '--end', '2022-02-10'])
    main()
    for name in ['summary.csv', 'annual_returns.csv', 'equity.csv', 'rebalances.csv', 'signals.csv', 'metadata.json', 'equity.png']:
        assert (tmp_path / 'results' / name).stat().st_size > 0
