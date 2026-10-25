import numpy as np
import pandas as pd
import pytest
from proxy_backtest import splice, bill_model, build_histories, load_inputs, ROOT
from strategy import TICKERS


def test_splice_preserves_daily_returns_without_launch_jump():
    dates = pd.bdate_range('2020-01-01', periods=5)
    proxy = pd.Series([100., 110., 121., 133.1, 146.41], index=dates)
    actual = pd.Series([20., 24., 18.], index=dates[2:])
    result = splice(proxy, actual)
    np.testing.assert_allclose(result.pct_change().iloc[1:], [.1, .1, .2, -.25])
    assert result.loc[dates[2]] == 20


def test_rate_model_uses_previous_known_rate_and_calendar_days():
    dates = pd.to_datetime(['2020-01-02', '2020-01-03', '2020-01-06'])
    rates = pd.Series([3.6, 3.6, 99.], index=dates)
    result = bill_model(rates, dates, fee=0)
    factor = 1 / (1 - .036 * 91 / 360)
    assert result.iloc[1] == pytest.approx(factor ** (1/91))
    assert result.iloc[2] / result.iloc[1] == pytest.approx(factor ** (3/91))
    altered = rates.copy()
    altered.iloc[-1] = -5
    pd.testing.assert_series_equal(result, bill_model(altered, dates, fee=0))


def test_rate_missing_observation_uses_only_prior_and_negative_rates_preserved():
    dates = pd.bdate_range('2020-01-01', periods=4)
    rates = pd.Series([-.1, 5.], index=dates[[0, 3]])
    result = bill_model(rates, dates, fee=0)
    assert (result.diff().dropna() < 0).all()


def test_proxy_missing_anchor_rejected():
    with pytest.raises(ValueError, match='anchor'):
        splice(pd.Series([1.], index=pd.to_datetime(['2020-01-01'])),
               pd.Series([2.], index=pd.to_datetime(['2020-01-02'])))


def test_downloaded_histories_complete_and_real_returns_preserved():
    directory = ROOT / 'data/proxy20'
    if not (directory / 'IRX.csv').exists():
        pytest.skip('Optional integration test requires downloaded proxy cache')
    inputs = load_inputs(directory)
    total, close, log = build_histories(inputs)
    assert total.columns.tolist() == TICKERS
    assert not total.isna().any().any()
    assert not close.isna().any().any()
    assert total.index[0] <= pd.Timestamp('2005-10-25')
    for ticker in TICKERS:
        actual = inputs[ticker]['Adj Close']
        pd.testing.assert_series_equal(total[ticker].pct_change().reindex(actual.index[1:]),
                                       actual.pct_change().iloc[1:], check_names=False)


def test_monthly_benchmark_cost_and_passive_drift():
    from proxy_backtest import benchmark_70_30
    dates = pd.to_datetime(['2020-01-02', '2020-01-03', '2020-02-03'])
    prices = pd.DataFrame({'VOO': [100., 110., 121.], 'SGOV': [100., 100., 100.]}, index=dates)
    result = benchmark_70_30(prices, dates, 10.)
    assert result.iloc[0] == pytest.approx(.9986)
    assert result.iloc[1] == pytest.approx(.9986 * 1.07)
    assert result.iloc[2] < .9986 * (0.7 * 1.21 + .3)
