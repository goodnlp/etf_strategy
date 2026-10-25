from dataclasses import dataclass
from pathlib import Path
import math
import os
import tomllib


@dataclass(frozen=True)
class Config:
    host: str = '127.0.0.1'
    port: int = 7497
    client_id: int = 27
    account: str = ''
    mode: str = 'paper'
    allow_live: bool = False
    capital_limit: float = 10000.
    cash_buffer_pct: float = .01
    max_order_usd: float = 10000.
    max_spread_bps: float = 40.
    limit_offset_bps: float = 10.
    min_trade_usd: float = 10.
    fee_buffer_usd: float = 2.
    quote_timeout: float = 15.
    quote_max_age: float = 15.
    order_timeout: float = 45.
    cancel_timeout: float = 15.
    start_minutes_before_close: int = 25
    end_minutes_before_close: int = 5
    state_dir: str = 'runtime/ibkr'
    prices_csv: str = ''

    def validate(self):
        if type(self.allow_live) is not bool:
            raise ValueError('allow_live must be a TOML boolean')
        if self.mode not in {'paper', 'live'}:
            raise ValueError('mode must be paper or live')
        if not (0 < self.port < 65536) or self.client_id < 1:
            raise ValueError('Invalid IBKR port/client ID')
        for key in ['capital_limit', 'max_order_usd', 'max_spread_bps', 'limit_offset_bps',
                    'fee_buffer_usd', 'quote_timeout', 'quote_max_age', 'order_timeout', 'cancel_timeout']:
            v = getattr(self, key)
            if not math.isfinite(v) or v <= 0:
                raise ValueError(f'{key} must be finite and positive')
        if not 0 <= self.min_trade_usd or not math.isfinite(self.min_trade_usd):
            raise ValueError('Invalid minimum trade size')
        if not .005 <= self.cash_buffer_pct <= .20:
            raise ValueError('Cash buffer must be between 0.5% and 20%')
        if not 0 < self.end_minutes_before_close < self.start_minutes_before_close <= 60:
            raise ValueError('Invalid execution window')
        return self

    def check_account(self, live_flag=False):
        if not self.account:
            raise ValueError('Set IBKR_ACCOUNT or account in a private config file')
        if self.mode == 'paper':
            if live_flag or not self.account.startswith('DU'):
                raise ValueError('Paper mode requires a DU account and no --live flag')
        elif not (self.allow_live and live_flag and not self.account.startswith('DU')):
            raise ValueError('Live requires mode=live, allow_live=true, --live and a non-DU account')


def load_config(path):
    values = tomllib.loads(Path(path).read_text())
    unknown = values.keys() - Config.__dataclass_fields__.keys()
    if unknown:
        raise ValueError(f'Unknown configuration fields: {sorted(unknown)}')
    if os.environ.get('IBKR_ACCOUNT'):
        values['account'] = os.environ['IBKR_ACCOUNT']
    return Config(**values).validate()
