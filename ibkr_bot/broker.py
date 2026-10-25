"""IBKR socket adapter. The pure planner/runner can be tested without this adapter."""
import math
import time
import pandas as pd
from ib_async import IB, Stock, LimitOrder, ExecutionFilter
from strategy import TICKERS
from .planner import Quote, Snapshot

PRIMARY = {'QQQ':'NASDAQ', 'VOO':'ARCA', 'VTV':'ARCA', 'VXUS':'NASDAQ',
           'GLDM':'ARCA', 'XLE':'ARCA', 'IEF':'NASDAQ', 'SGOV':'ARCA'}


class IBKRBroker:
    def __init__(self, config, readonly=True):
        self.config, self.readonly = config, readonly
        self.ib = IB()
        self.ib.RequestTimeout = 20
        self.contracts, self.ticks = {}, {}

    def __enter__(self):
        try:
            self.ib.connect(self.config.host, self.config.port, clientId=self.config.client_id,
                            timeout=15, readonly=self.readonly, account=self.config.account, raiseSyncErrors=True)
            if self.config.account not in self.ib.managedAccounts():
                raise ValueError('Configured account is not managed by this TWS/Gateway session')
            self.ib.reqMarketDataType(1)
            for ticker in TICKERS:
                details = self.ib.reqContractDetails(Stock(ticker, 'SMART', 'USD', primaryExchange=PRIMARY[ticker]))
                if len(details) != 1:
                    raise ValueError(f'{ticker}: ambiguous/unavailable IBKR contract')
                contract = details[0].contract
                if contract.secType != 'STK' or contract.currency != 'USD' or contract.symbol != ticker:
                    raise ValueError('Unexpected IBKR contract')
                contract.exchange = 'SMART'
                self.contracts[ticker], self.ticks[ticker] = contract, details[0].minTick
            return self
        except BaseException:
            self.ib.disconnect()
            raise

    def __exit__(self, *_):
        self.ib.disconnect()

    def assert_connected(self):
        if not self.ib.isConnected():
            raise RuntimeError('IBKR disconnected; order state must be reconciled')

    def snapshot(self):
        self.assert_connected()
        positions = {}
        for item in self.ib.reqPositions():
            if item.account != self.config.account or not item.position:
                continue
            symbol = item.contract.symbol
            if (symbol not in self.contracts or item.contract.conId != self.contracts[symbol].conId
                    or item.contract.secType != 'STK'):
                raise ValueError('Dedicated account required: found an unmanaged instrument')
            positions[symbol] = positions.get(symbol, 0.) + float(item.position)
        values = self.ib.accountSummary(self.config.account)
        for value in self.ib.accountValues(self.config.account):
            if value.tag == 'CashBalance' and value.currency not in {'USD', 'BASE'} and abs(float(value.value)) > .01:
                raise ValueError('Non-USD cash balance: automatic FX conversion is not supported')

        def usd(tag):
            rows = [v for v in values if v.tag == tag and v.currency == 'USD']
            if len(rows) != 1:
                raise ValueError(f'USD base account with {tag} required; unknown balances cannot fund orders')
            return float(rows[0].value)
        return Snapshot(usd('NetLiquidation'), usd('TotalCashValue'), usd('SettledCash'),
                        usd('AvailableFunds'), positions).validate()

    def working_orders(self):
        self.assert_connected()
        return [{'order_id': t.order.orderId, 'ref': t.order.orderRef, 'symbol': t.contract.symbol,
                 'status': t.orderStatus.status, 'remaining': t.orderStatus.remaining}
                for t in self.ib.reqAllOpenOrders()
                if t.order.account in {'', self.config.account} and not t.isDone()]

    def quotes(self, symbols):
        self.assert_connected()
        streams = {s: self.ib.reqMktData(self.contracts[s]) for s in symbols}
        try:
            stop = time.monotonic() + self.config.quote_timeout
            while time.monotonic() < stop:
                self.ib.sleep(.2)
                if all(t.time is not None and math.isfinite(t.bid) and math.isfinite(t.ask)
                       and t.bid > 0 and t.ask > 0 for t in streams.values()):
                    now = pd.Timestamp.now(tz='UTC')
                    return {s: Quote(t.bid, t.ask, pd.Timestamp(t.time), self.ticks[s], t.marketDataType)
                            .validate(now, self.config) for s, t in streams.items()}
                self.assert_connected()
            raise TimeoutError('No valid realtime bid/ask; verify IBKR market data subscriptions')
        finally:
            for symbol in streams:
                self.ib.cancelMktData(self.contracts[symbol])

    def execute(self, intent, ref, on_update, cutoff):
        self.assert_connected()
        if self.readonly:
            raise RuntimeError('Read-only connection cannot submit orders')
        if pd.Timestamp.now(tz='UTC') >= cutoff:
            raise RuntimeError('Execution window ended before submission')
        order = LimitOrder(intent.side, intent.quantity, intent.limit_price,
                           tif='DAY', outsideRth=False, account=self.config.account,
                           orderRef=ref, transmit=True)
        trade = self.ib.placeOrder(self.contracts[intent.symbol], order)

        def record():
            status = trade.orderStatus
            payload = {'order_id': trade.order.orderId, 'perm_id': status.permId,
                       'filled': float(status.filled), 'remaining': float(status.remaining),
                       'avg_fill_price': float(status.avgFillPrice),
                       'messages': [entry.message for entry in trade.log]}
            on_update(ref, status.status or 'submitted', payload)
            return payload

        try:
            record()
            stop = min(time.monotonic()+self.config.order_timeout,
                       time.monotonic()+max(0., (cutoff-pd.Timestamp.now(tz='UTC')).total_seconds()))
            while not trade.isDone() and time.monotonic() < stop:
                self.ib.sleep(.2)
                self.assert_connected()
                record()
            if not trade.isDone():
                self.ib.cancelOrder(trade.order)
                cancel_stop = time.monotonic()+self.config.cancel_timeout
                while not trade.isDone() and time.monotonic() < cancel_stop:
                    self.ib.sleep(.2)
                    self.assert_connected()
            result = record()
            if trade.orderStatus.status != 'Filled' or result['filled'] != intent.quantity:
                raise RuntimeError(f'{intent.symbol}: incomplete/rejected/unconfirmed order; reconcile journal')
            if not math.isfinite(result['avg_fill_price']) or result['avg_fill_price'] <= 0:
                raise RuntimeError('Missing execution price; reconcile order')
            # Give streaming account/position events a chance to arrive.
            self.ib.sleep(.5)
            return result
        except BaseException:
            # Only cancel the order this invocation created; never cancel unrelated orders.
            if self.ib.isConnected() and not trade.isDone():
                self.ib.cancelOrder(trade.order)
                stop = time.monotonic()+self.config.cancel_timeout
                while not trade.isDone() and time.monotonic() < stop:
                    self.ib.sleep(.2)
            record()
            raise

    def reconcile(self):
        self.assert_connected()
        completed = self.ib.reqCompletedOrders(apiOnly=False)
        fills = self.ib.reqExecutions(ExecutionFilter(acctCode=self.config.account))
        return {'observed_at': str(pd.Timestamp.now(tz='UTC')),
                'snapshot': self.snapshot().__dict__, 'working_orders': self.working_orders(),
                'completed_orders': [{'ref': t.order.orderRef, 'order_id': t.order.orderId,
                                      'status': t.orderStatus.status, 'symbol': t.contract.symbol}
                                     for t in completed if t.order.account == self.config.account],
                'executions': [{'ref': f.execution.orderRef, 'exec_id': f.execution.execId,
                                'symbol': f.contract.symbol, 'shares': float(f.execution.shares),
                                'price': f.execution.price, 'time': str(f.execution.time)} for f in fills]}
