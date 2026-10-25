from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math
import pandas as pd
from strategy import TICKERS, STOCKS


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    timestamp: pd.Timestamp
    tick: float = .01
    data_type: int = 1

    @property
    def mid(self):
        return (self.bid + self.ask)/2

    def validate(self, now, config):
        if not all(math.isfinite(v) and v > 0 for v in [self.bid, self.ask, self.tick]):
            raise ValueError('Invalid bid/ask/minimum tick')
        if self.bid > self.ask or (self.ask-self.bid)/self.mid*10000 > config.max_spread_bps:
            raise ValueError('Crossed market or excessive bid/ask spread')
        ts = pd.Timestamp(self.timestamp)
        if ts.tzinfo is None:
            raise ValueError('Quote must carry a timezone')
        age = (pd.Timestamp(now)-ts).total_seconds()
        if age < -2 or age > config.quote_max_age or self.data_type != 1:
            raise ValueError('Stale, frozen or delayed quote: realtime subscription required')
        return self

    def limit(self, side, offset_bps):
        if side not in {'BUY', 'SELL'}:
            raise ValueError('Invalid order side')
        value = self.ask*(1+offset_bps/10000) if side == 'BUY' else self.bid*(1-offset_bps/10000)
        tick = Decimal(str(self.tick))
        rounding = ROUND_CEILING if side == 'BUY' else ROUND_FLOOR
        return float((Decimal(str(value))/tick).to_integral_value(rounding=rounding)*tick)


@dataclass
class Snapshot:
    nav: float
    cash: float
    settled_cash: float
    available: float
    positions: dict

    def validate(self):
        if not all(math.isfinite(v) for v in [self.nav, self.cash, self.settled_cash, self.available]) or self.nav <= 0:
            raise ValueError('Missing or invalid USD account balances')
        if min(self.cash, self.settled_cash, self.available) < -.01:
            raise ValueError('Negative cash / funds: borrowing is not supported')
        for symbol, quantity in self.positions.items():
            if symbol not in TICKERS and quantity != 0:
                raise ValueError(f'Unmanaged position {symbol}: dedicated account required')
            if not math.isfinite(quantity) or quantity < 0 or not float(quantity).is_integer():
                raise ValueError('Only long whole-share positions supported; no shorts/fractional holdings')
        return self

    @property
    def spendable(self):
        return max(0., min(self.cash, self.settled_cash, self.available))


@dataclass
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    limit_price: float

    @property
    def notional(self):
        return self.quantity*self.limit_price


@dataclass
class Plan:
    capital: float
    reserve: float
    weights: dict
    targets: dict
    orders: list[OrderIntent]

    def to_dict(self):
        return asdict(self)


def validate_weights(weights):
    if set(weights) != set(TICKERS) or any(not math.isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError('Invalid strategy weights')
    if abs(sum(weights.values())-1) > 1e-8:
        raise ValueError('Weights must sum to one')
    if any(weights[t] > .35+1e-8 for t in TICKERS if t != 'SGOV'):
        raise ValueError('Risk ETF cap exceeded')
    if sum(weights[t] for t in STOCKS) > .7+1e-8:
        raise ValueError('Equity cap exceeded')


def build_plan(weights, snapshot, quotes, config, now):
    validate_weights(weights)
    snapshot.validate()
    capital = min(snapshot.nav, config.capital_limit)
    reserve = capital*config.cash_buffer_pct
    targets, orders = {}, []
    for ticker in TICKERS:
        q = quotes[ticker].validate(now, config)
        # Ask-side limit creates a conservative integer target under the budget.
        buy_limit = q.limit('BUY', config.limit_offset_bps)
        target = math.floor((capital-reserve)*weights[ticker]/buy_limit)
        targets[ticker] = target
        delta = target-int(snapshot.positions.get(ticker, 0))
        if delta:
            side = 'BUY' if delta > 0 else 'SELL'
            price = q.limit(side, config.limit_offset_bps)
            intent = OrderIntent(ticker, side, abs(delta), price)
            if intent.notional > config.max_order_usd:
                raise ValueError(f'{ticker} exceeds max_order_usd; edit configuration before trading')
            if intent.notional >= config.min_trade_usd:
                orders.append(intent)
    orders.sort(key=lambda o: (o.side != 'SELL', o.symbol == 'SGOV', -weights[o.symbol], o.symbol))
    return Plan(capital, reserve, weights, targets, orders)
