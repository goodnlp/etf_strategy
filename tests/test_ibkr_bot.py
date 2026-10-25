from dataclasses import replace
from pathlib import Path
import json
import pandas as pd
import pytest
pytest.importorskip('ib_async')
pytest.importorskip('exchange_calendars')
from ibkr_bot.calendar import cycle_for_month, cycle_from_signal
from ibkr_bot.config import Config, load_config
from ibkr_bot.planner import Quote, Snapshot, build_plan
from ibkr_bot.signals import Signal, make_signal
from ibkr_bot.state import Journal
from ibkr_bot.runner import execute_cycle
from strategy import TICKERS

NOW = pd.Timestamp('2026-11-02 20:40:00Z')
CONFIG = Config(account='DU12345')


def weights(**kwargs):
    result = dict.fromkeys(TICKERS, 0.)
    result.update(kwargs)
    result['SGOV'] = 1-sum(result[t] for t in TICKERS if t != 'SGOV')
    return result


def quote(bid=100., ask=100.02, **kwargs):
    return Quote(bid, ask, kwargs.pop('timestamp', NOW), **kwargs)


def test_calendar_month_end_dst_and_holidays():
    cycle = cycle_for_month(NOW)
    assert cycle.signal_date == pd.Timestamp('2026-10-30')
    assert cycle.execution_date == pd.Timestamp('2026-11-02')
    assert cycle.close == pd.Timestamp('2026-11-02 21:00Z')
    assert cycle.due(NOW)
    assert not cycle.due(pd.Timestamp('2026-11-03 20:40Z'))
    september = cycle_from_signal('2026-08-31')
    assert september.close == pd.Timestamp('2026-09-01 20:00Z')
    january = cycle_from_signal('2025-12-31')
    assert january.execution_date == pd.Timestamp('2026-01-02')
    with pytest.raises(ValueError, match='final exchange'):
        cycle_from_signal('2026-10-29')
    with pytest.raises(ValueError, match='timezone'):
        cycle_for_month(pd.Timestamp('2026-11-02'))


def test_half_day_market_close():
    # November 2024's last session was Black Friday's early close.
    assert cycle_from_signal('2024-11-29').signal_date == pd.Timestamp('2024-11-29')
    from ibkr_bot.calendar import calendar
    assert calendar(2024).session_close('2024-11-29') == pd.Timestamp('2024-11-29 18:00Z')


def test_live_account_gates_and_configuration_validation(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='DU account'):
        replace(CONFIG, account='U123').check_account()
    live = replace(CONFIG, mode='live', account='U123', allow_live=True)
    with pytest.raises(ValueError, match='Live requires'):
        live.check_account()
    live.check_account(live_flag=True)
    with pytest.raises(ValueError):
        replace(live, allow_live=False).check_account(live_flag=True)
    with pytest.raises(ValueError):
        replace(CONFIG, cash_buffer_pct=0).validate()
    with pytest.raises(ValueError, match='boolean'):
        replace(CONFIG, allow_live='false').validate()
    file = tmp_path/'config.toml'
    file.write_text('port=4002\naccount="DU111"\n')
    monkeypatch.setenv('IBKR_ACCOUNT', 'DU222')
    assert load_config(file).account == 'DU222'
    file.write_text('unknown=1')
    with pytest.raises(ValueError, match='Unknown'):
        load_config(file)


@pytest.mark.parametrize('bad', [quote(timestamp=NOW-pd.Timedelta(seconds=60)),
                                  quote(data_type=3), quote(data_type=2), quote(bid=90,ask=110),
                                  quote(bid=101,ask=100), quote(bid=float('nan'))])
def test_invalid_quotes_block_trading(bad):
    with pytest.raises(ValueError):
        bad.validate(NOW, CONFIG)


def test_tick_rounding_limits():
    q=quote(bid=99.99,ask=100.01,tick=.05)
    assert q.limit('BUY', 10) == 100.15
    assert q.limit('SELL', 10) == 99.85


def test_plan_whole_shares_caps_cash_reserve_and_sell_first():
    qs={s:quote() for s in TICKERS}
    snapshot=Snapshot(10000,1000,1000,1000,{'SGOV':90})
    plan=build_plan(weights(QQQ=.35,GLDM=.35),snapshot,qs,CONFIG,NOW)
    assert plan.reserve == 100
    assert sum(plan.targets[t]*qs[t].limit('BUY',10) for t in TICKERS) <= 9900
    assert plan.orders[0].side == 'SELL'
    assert plan.orders[0].symbol == 'SGOV'
    assert all(o.quantity > 0 and isinstance(o.quantity,int) for o in plan.orders)
    with pytest.raises(ValueError,match='Equity cap'):
        build_plan(weights(QQQ=.3,VOO=.3,VTV=.3),snapshot,qs,CONFIG,NOW)
    with pytest.raises(ValueError,match='max_order'):
        build_plan(weights(QQQ=.35),snapshot,qs,replace(CONFIG,max_order_usd=20),NOW)


@pytest.mark.parametrize('positions', [{'AAPL':1},{'QQQ':-1},{'QQQ':1.5}])
def test_unmanaged_short_fractional_positions_refused(positions):
    with pytest.raises(ValueError):
        Snapshot(10000,100,100,100,positions).validate()


def test_missing_month_end_bar_refused_and_future_prices_ignored():
    from ibkr_bot.calendar import calendar
    index=calendar(2026).sessions_in_range('2025-08-01','2026-11-02')
    prices=pd.DataFrame({t:100*(1.0001**pd.Series(range(len(index)),index=index)) for t in TICKERS})
    frame=pd.concat({'Adj Close':prices,'Close':prices},axis=1)
    first=make_signal('2026-10-30',frame)
    frame.loc['2026-11-02',('Adj Close','QQQ')]=1e9
    assert first.digest == make_signal('2026-10-30',frame).digest
    with pytest.raises(ValueError,match='Missing'):
        make_signal('2026-10-30',frame.drop(pd.Timestamp('2026-10-30')))


class FakeBroker:
    def __init__(self, positions=None, cash=10000., fail=None, settle=True):
        self.positions=dict(positions or {})
        self.cash=self.settled=cash
        self.calls=[]
        self.fail=fail
        self.settle=settle
        self.open=[]
    def snapshot(self):
        return Snapshot(10000.,self.cash,self.settled,self.cash,self.positions.copy())
    def quotes(self,symbols):
        return {s:quote() for s in symbols}
    def working_orders(self):
        return self.open
    def execute(self,intent,ref,on_update,cutoff):
        self.calls.append(intent)
        result={'filled':intent.quantity,'avg_fill_price':100.,'order_id':len(self.calls)}
        if self.fail:
            on_update(ref,'Submitted',{'filled':1,'order_id':len(self.calls)})
            raise RuntimeError(self.fail)
        change=intent.quantity*(1 if intent.side=='BUY' else -1)
        self.positions[intent.symbol]=self.positions.get(intent.symbol,0)+change
        self.cash-=change*100+1
        if self.settle or intent.side=='BUY':
            self.settled-=change*100+1
        on_update(ref,'Filled',result)
        return result


def invoke(tmp_path,broker,execute=True,config=CONFIG,journal=None):
    own=journal is None
    j=journal or Journal(tmp_path)
    signal=Signal(pd.Timestamp('2026-10-30'),weights(QQQ=.35,GLDM=.35),{},pd.DataFrame(),'hash')
    try:
        return execute_cycle(config,cycle_for_month(NOW),signal,broker,j,execute,clock=lambda:NOW)
    finally:
        if own:j.close()


def test_dry_run_never_submits_or_consumes_cycle(tmp_path):
    b=FakeBroker()
    result=invoke(tmp_path,b,False)
    assert result['status']=='dry_run'
    assert b.calls==[]
    j=Journal(tmp_path)
    assert j.inspect()['runs']==[]
    j.close()


def test_successful_execution_no_duplicate_reentry_and_cash_floor(tmp_path):
    b=FakeBroker({'SGOV':90},cash=1000.)
    result=invoke(tmp_path,b)
    assert result['status']=='completed'
    assert b.calls[0].side=='SELL'
    assert b.calls[0].symbol=='SGOV'
    assert result['final']['cash'] >= 100
    count=len(b.calls)
    assert invoke(tmp_path,b)['status']=='already_completed'
    assert len(b.calls)==count
    j=Journal(tmp_path)
    assert all(o['status']=='Filled' for o in j.inspect()['orders'])
    j.close()


def test_partial_or_unknown_submission_blocks_retries(tmp_path):
    b=FakeBroker(fail='disconnect after partial fill')
    with pytest.raises(RuntimeError,match='disconnect'):
        invoke(tmp_path,b)
    assert len(b.calls)==1
    with pytest.raises(RuntimeError,match='Unresolved'):
        invoke(tmp_path,b)
    assert len(b.calls)==1
    j=Journal(tmp_path)
    assert j.inspect()['runs'][0]['status']=='failed'
    assert j.inspect()['orders'][0]['status']=='Submitted'
    # Also block a different month's execution until the previous run is resolved.
    with pytest.raises(RuntimeError,match='Unresolved'):
        j.begin('DU12345|2026-11-30',{})
    j.close()


def test_settlement_shortfall_does_not_borrow_or_retry_automatically(tmp_path):
    b=FakeBroker({'SGOV':100},cash=0.,settle=False)
    result=invoke(tmp_path,b)
    assert result['status']=='completed_with_shortfall'
    assert all(o.side=='SELL' for o in b.calls)
    assert b.cash>=0 and b.settled>=0
    assert invoke(tmp_path,b)['status']=='already_completed'


def test_stop_file_and_external_orders_block_orders(tmp_path):
    b=FakeBroker()
    b.open=[{'status':'Submitted'}]
    with pytest.raises(RuntimeError,match='Working orders'):
        invoke(tmp_path,b)
    b.open=[]
    (tmp_path/'STOP').touch()
    with pytest.raises(RuntimeError,match='STOP'):
        invoke(tmp_path,b)
    assert b.calls==[]


def test_crash_after_intent_and_operator_resolution(tmp_path):
    j=Journal(tmp_path)
    run=j.begin('DU12345|2026-10-30',{})
    from ibkr_bot.planner import OrderIntent
    j.record_intent(run,OrderIntent('QQQ','BUY',1,100.),0)
    with pytest.raises(RuntimeError,match='Unresolved'):
        j.begin('DU12345|2026-10-30',{})
    with pytest.raises(ValueError,match='another account'):
        j.resolve(run,{'working_orders':[]},'DU999')
    j.resolve(run,{'working_orders':[]},'DU12345')
    assert j.begin('DU12345|2026-10-30',{}) != run
    j.close()


def test_two_process_lock_refuses_concurrent_execution(tmp_path):
    a,b=Journal(tmp_path),Journal(tmp_path)
    with a.lock():
        with pytest.raises(RuntimeError,match='Another trading process'):
            with b.lock():pass
    a.close();b.close()


def test_ibkr_adapter_uses_sdk_limit_order_and_records_full_fill(tmp_path):
    from ib_async import Stock, Trade, OrderStatus
    from ibkr_bot.broker import IBKRBroker
    from ibkr_bot.planner import OrderIntent
    adapter=IBKRBroker(CONFIG,readonly=False)
    adapter.contracts={'QQQ':Stock('QQQ','SMART','USD',conId=123)}
    seen=[]
    class API:
        def isConnected(self):return True
        def placeOrder(self,contract,order):
            order.orderId=42
            seen.append((contract,order))
            return Trade(contract,order,OrderStatus(orderId=42,status='Filled',filled=2,
                                                   remaining=0,avgFillPrice=100.,permId=99))
        def sleep(self,seconds):pass
    adapter.ib=API()
    events=[]
    result=adapter.execute(OrderIntent('QQQ','BUY',2,100.2),'ETF-test-0',
                           lambda *event:events.append(event),pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=1))
    assert result['filled']==2 and result['order_id']==42
    order=seen[0][1]
    assert order.orderType=='LMT' and order.account=='DU12345'
    assert order.totalQuantity==2 and order.tif=='DAY'
    assert not order.outsideRth and order.orderRef=='ETF-test-0'
    assert events[-1][1]=='Filled'


def test_ibkr_adapter_partial_cancelled_fill_is_not_success():
    from ib_async import Stock, Trade, OrderStatus
    from ibkr_bot.broker import IBKRBroker
    from ibkr_bot.planner import OrderIntent
    adapter=IBKRBroker(CONFIG,readonly=False)
    adapter.contracts={'QQQ':Stock('QQQ','SMART','USD',conId=123)}
    class API:
        def isConnected(self):return True
        def placeOrder(self,contract,order):
            return Trade(contract,order,OrderStatus(status='Cancelled',filled=1,remaining=1,avgFillPrice=100.))
        def sleep(self,seconds):pass
    adapter.ib=API()
    events=[]
    with pytest.raises(RuntimeError,match='incomplete'):
        adapter.execute(OrderIntent('QQQ','BUY',2,100.2),'ETF-test-0',
                        lambda *event:events.append(event),pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=1))
    assert events[-1][2]['filled']==1


def test_ibkr_adapter_timeout_cancels_own_order_and_never_resubmits():
    from ib_async import Stock, Trade, OrderStatus
    from ibkr_bot.broker import IBKRBroker
    from ibkr_bot.planner import OrderIntent
    adapter=IBKRBroker(replace(CONFIG,order_timeout=.001),readonly=False)
    adapter.contracts={'QQQ':Stock('QQQ','SMART','USD',conId=123)}
    class API:
        def __init__(self):self.calls=0;self.cancelled=[]
        def isConnected(self):return True
        def placeOrder(self,contract,order):
            self.calls+=1
            self.trade=Trade(contract,order,OrderStatus(status='Submitted',filled=0,remaining=2))
            return self.trade
        def sleep(self,seconds):
            import time
            time.sleep(.002)
        def cancelOrder(self,order):
            self.cancelled.append(order)
            self.trade.orderStatus.status='Cancelled'
    adapter.ib=API()
    with pytest.raises(RuntimeError,match='incomplete'):
        adapter.execute(OrderIntent('QQQ','BUY',2,100.2),'ETF-test-0',lambda *event:None,
                        pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=1))
    assert adapter.ib.calls==1
    assert adapter.ib.cancelled==[adapter.ib.trade.order]


def test_cli_not_due_never_connects_and_preview_never_places_orders(tmp_path,monkeypatch,capsys):
    from ibkr_bot.__main__ import main
    config=tmp_path/'example.toml'
    config.write_text(f'state_dir="{tmp_path}/runtime"\n')
    monkeypatch.setattr('ibkr_bot.__main__.IBKRBroker',lambda *a,**k:pytest.fail('Unexpected IBKR connection'))
    # Force the schedule decision without relying on the host's calendar date.
    from ibkr_bot.calendar import Cycle
    monkeypatch.setattr(Cycle,'due',lambda *a,**k:False)
    assert main(['run','--config',str(config)])==0
    assert json.loads(capsys.readouterr().out)['status']=='not_due'
    root=Path(__file__).resolve().parents[1]
    assert main(['preview','--config',str(config),'--signal-date','2026-09-30',
                 '--prices-csv',str(root/'data/prices.csv')])==0
    assert 'illustrative_preview' in json.loads(capsys.readouterr().out)['status']


def test_journal_update_failure_after_submission_still_attempts_cancellation():
    from ib_async import Stock, Trade, OrderStatus
    from ibkr_bot.broker import IBKRBroker
    from ibkr_bot.planner import OrderIntent
    adapter=IBKRBroker(CONFIG,readonly=False)
    adapter.contracts={'QQQ':Stock('QQQ','SMART','USD',conId=123)}
    class API:
        def __init__(self):self.cancelled=False
        def isConnected(self):return True
        def placeOrder(self,contract,order):
            self.trade=Trade(contract,order,OrderStatus(status='Submitted',filled=0,remaining=2))
            return self.trade
        def cancelOrder(self,order):
            self.cancelled=True
            self.trade.orderStatus.status='Cancelled'
        def sleep(self,seconds):pass
    adapter.ib=API()
    def unavailable(*args):raise OSError('disk full')
    with pytest.raises(OSError,match='disk full'):
        adapter.execute(OrderIntent('QQQ','BUY',2,100.2),'ETF-test-0',unavailable,
                        pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=1))
    assert adapter.ib.cancelled
