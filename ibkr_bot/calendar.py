from dataclasses import dataclass
from functools import lru_cache
import pandas as pd
import exchange_calendars as xcals


@lru_cache(maxsize=8)
def calendar(year):
    return xcals.get_calendar('XNYS', start=f'{year-2}-01-01', end=f'{year+2}-12-31')


@dataclass(frozen=True)
class Cycle:
    signal_date: pd.Timestamp
    execution_date: pd.Timestamp
    close: pd.Timestamp

    def due(self, now, start_minutes=25, end_minutes=5):
        now = utc(now)
        return (now.tz_convert('America/New_York').date() == self.execution_date.date()
                and self.close - pd.Timedelta(minutes=start_minutes) <= now
                < self.close - pd.Timedelta(minutes=end_minutes))


def utc(value):
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return timestamp.tz_convert('UTC')


def cycle_for_month(now):
    """Previous month's last session -> current month's first session."""
    local = utc(now).tz_convert('America/New_York').tz_localize(None).normalize()
    first = local.replace(day=1)
    previous = first - pd.Timedelta(days=1)
    cal = calendar(local.year)
    signal = cal.sessions_in_range(previous.replace(day=1), previous)[-1]
    execution = cal.sessions_in_range(first, first + pd.offsets.MonthEnd(0))[0]
    return Cycle(signal, execution, cal.session_close(execution))


def cycle_from_signal(date):
    signal = pd.Timestamp(date).normalize()
    if signal.tzinfo is not None:
        raise ValueError('Signal date must be a date without timezone')
    cal = calendar(signal.year)
    last = cal.sessions_in_range(signal.replace(day=1), signal + pd.offsets.MonthEnd(0))[-1]
    if signal != last:
        raise ValueError('Signal date must be the final exchange session of its month')
    execution = cal.next_session(signal)
    return Cycle(signal, execution, cal.session_close(execution))
