import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import pandas as pd
from .calendar import cycle_for_month, cycle_from_signal
from .config import load_config
from .signals import load_signal
from .state import Journal
from .planner import Quote, Snapshot, build_plan
from .broker import IBKRBroker
from .runner import execute_cycle


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Monthly ETF rotation via IBKR TWS / Gateway')
    parser.add_argument('command', choices=['signal', 'preview', 'run', 'status', 'reconcile', 'resolve'])
    parser.add_argument('--config', default='ibkr.local.toml')
    parser.add_argument('--signal-date', help='For signal/preview only, YYYY-MM-DD month-end session')
    parser.add_argument('--prices-csv', help='Override local two-header Yahoo cache for signal/preview')
    parser.add_argument('--nav', type=float, default=10000., help='Preview only: hypothetical all-cash USD capital')
    parser.add_argument('--execute', action='store_true', help='Run only: submit broker orders; otherwise read-only')
    parser.add_argument('--live', action='store_true', help='Explicitly allow a configured live account')
    parser.add_argument('--run-id', help='Resolve only: failed/interrupted journal run ID')
    parser.add_argument('--confirm-broker-audited', action='store_true', help='Resolve: operator checked TWS orders/fills/positions, including uncertain submissions')
    args = parser.parse_args(argv)
    if args.execute and args.command != 'run':
        parser.error('--execute is only valid with run')
    if (args.signal_date or args.prices_csv) and args.command not in {'signal', 'preview'}:
        parser.error('Signal/date overrides are only allowed for signal/preview')
    config = load_config(args.config)
    if args.prices_csv:
        config = replace(config, prices_csv=args.prices_csv)
    now = pd.Timestamp.now(tz='UTC')
    cycle = cycle_from_signal(args.signal_date) if args.signal_date else cycle_for_month(now)
    if args.command == 'run' and not cycle.due(now, config.start_minutes_before_close, config.end_minutes_before_close):
        emit({'status': 'not_due', 'signal_date': str(cycle.signal_date.date()),
              'execution_date': str(cycle.execution_date.date()), 'session_close_utc': str(cycle.close)})
        return 0
    if args.command in {'signal', 'preview', 'run'}:
        if calendar_close(cycle.signal_date) >= now:
            raise ValueError('Signal session has not closed yet')
        signal = load_signal(cycle.signal_date, Path(config.state_dir)/'signals', config.prices_csv)
        if args.command == 'signal':
            emit({'signal_date': str(signal.date.date()), 'execution_date': str(cycle.execution_date.date()),
                  'weights': signal.weights, 'sha256': signal.digest,
                  'detail': json.loads(signal.detail.to_json(orient='index'))})
            return 0
        if args.command == 'preview':
            quotes = {s: Quote(p, p, now) for s,p in signal.prices.items()}
            snap = Snapshot(args.nav, args.nav, args.nav, args.nav, {})
            emit({'status': 'illustrative_preview_using_month_end_closes_NOT_live_quotes',
                  'signal_date': str(signal.date.date()), 'plan': build_plan(signal.weights, snap, quotes, config, now).to_dict()})
            return 0
    journal = Journal(config.state_dir)
    try:
        if args.command == 'status':
            emit(journal.inspect())
            return 0
        config.check_account(args.live)
        with IBKRBroker(config, readonly=not args.execute) as broker:
            if args.command == 'run':
                result = execute_cycle(config, cycle, signal, broker, journal, args.execute, args.live)
                emit(result)
                return 2 if result['status'] == 'completed_with_shortfall' else 0
            if args.command == 'reconcile':
                emit({'broker': broker.reconcile(), 'journal': journal.inspect(),
                      'note': 'Broker APIs may expose only recent orders/executions. Absence is not proof of non-submission.'})
                return 0
            if not args.run_id or not args.confirm_broker_audited:
                raise ValueError('resolve requires --run-id and --confirm-broker-audited after manual TWS audit')
            with journal.lock():
                report = broker.reconcile()
                if report['working_orders']:
                    raise RuntimeError('Working orders remain; resolution refused')
                journal.resolve(args.run_id, report, config.account)
            emit({'status': 'resolved', 'run_id': args.run_id,
                  'note': 'A subsequent run replans from actual holdings; it still requires the execution window.'})
            return 0
    finally:
        journal.close()


def calendar_close(date):
    from .calendar import calendar
    return calendar(date.year).session_close(date)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        emit({'status': 'error', 'error': str(error)})
        sys.exit(1)
