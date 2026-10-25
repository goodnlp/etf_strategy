from dataclasses import asdict
import math
from pathlib import Path
import pandas as pd
from .planner import build_plan, OrderIntent
from strategy import TICKERS


def execute_cycle(config, cycle, signal, broker, journal, execute=False, live_flag=False,
                  clock=lambda: pd.Timestamp.now(tz='UTC')):
    config.validate().check_account(live_flag)
    if signal.date != cycle.signal_date:
        raise ValueError('Signal date does not match execution cycle')
    if not cycle.due(clock(), config.start_minutes_before_close, config.end_minutes_before_close):
        raise ValueError('Not inside the first-session execution window')
    with journal.lock():
        if (journal.directory / 'STOP').exists():
            raise RuntimeError('STOP file present; execution disabled')
        if broker.working_orders():
            raise RuntimeError('Working orders exist in this account; reconcile before trading')
        initial = broker.snapshot().validate()
        quotes = broker.quotes(TICKERS)
        plan = build_plan(signal.weights, initial, quotes, config, clock())
        payload = {'signal_date': str(signal.date.date()), 'execution_date': str(cycle.execution_date.date()),
                   'signal_sha256': signal.digest, 'initial': asdict(initial), 'plan': plan.to_dict()}
        if not execute:
            return {'status': 'dry_run', **payload}
        run = journal.begin(f'{config.account}|{signal.date.date()}', payload)
        if run is None:
            return {'status': 'already_completed'}
        expected = {s: int(initial.positions.get(s, 0)) for s in TICKERS}
        cash_ceiling, shortfalls = initial.spendable, []
        try:
            for sequence, original in enumerate(plan.orders):
                if (journal.directory / 'STOP').exists():
                    raise RuntimeError('STOP file present; remaining orders stopped')
                if not cycle.due(clock(), config.start_minutes_before_close, config.end_minutes_before_close):
                    raise RuntimeError('Execution window expired; remaining orders stopped')
                if broker.working_orders():
                    raise RuntimeError('Unexpected working order appeared; stopped')
                snapshot = broker.snapshot().validate()
                if any(int(snapshot.positions.get(s, 0)) != expected[s] for s in TICKERS):
                    raise RuntimeError('Account positions differ from confirmed fills; reconcile before trading')
                quote = broker.quotes([original.symbol])[original.symbol].validate(clock(), config)
                price = quote.limit(original.side, config.limit_offset_bps)
                quantity = original.quantity
                if original.side == 'BUY':
                    # Fixed signal/budget, refreshed limit; never expand the original intent.
                    target = math.floor((plan.capital-plan.reserve)*signal.weights[original.symbol]/price)
                    wanted = min(quantity, max(0, target-expected[original.symbol]))
                    affordable = max(0, math.floor((min(snapshot.spendable, cash_ceiling)-plan.reserve-config.fee_buffer_usd)/price))
                    quantity = min(wanted, affordable)
                    if quantity < wanted:
                        shortfalls.append({'symbol': original.symbol, 'requested': wanted,
                                           'affordable': quantity, 'reason': 'insufficient settled USD funds'})
                if quantity == 0 or quantity*price < config.min_trade_usd:
                    continue
                intent = OrderIntent(original.symbol, original.side, quantity, price)
                if intent.notional > config.max_order_usd:
                    raise ValueError('Refreshed order exceeds max_order_usd')
                if intent.side == 'SELL' and quantity > expected[intent.symbol]:
                    raise ValueError('Sell would create a short position')
                ref = journal.record_intent(run, intent, sequence)
                result = broker.execute(intent, ref, journal.update_order,
                                        cycle.close-pd.Timedelta(minutes=config.end_minutes_before_close))
                # No automatic retry after a partial/unknown result.
                if result['filled'] != quantity or result['avg_fill_price'] <= 0:
                    raise RuntimeError('Broker did not confirm full execution')
                if intent.side == 'BUY':
                    expected[intent.symbol] += quantity
                    cash_ceiling -= intent.notional+config.fee_buffer_usd
                else:
                    expected[intent.symbol] -= quantity
                    cash_ceiling += result['avg_fill_price']*quantity-config.fee_buffer_usd
            final = broker.snapshot().validate()
            if any(int(final.positions.get(s, 0)) != expected[s] for s in TICKERS) or broker.working_orders():
                raise RuntimeError('Final broker positions/orders not reconciled')
            status = 'completed_with_shortfall' if shortfalls else 'completed'
            report = {'status': status, 'run_id': run, 'shortfalls': shortfalls,
                      'final': asdict(final), **payload}
            (journal.directory / f'result-{run}.json').write_text(__import__('json').dumps(report, indent=2))
            journal.finish(run, status)
            return report
        except BaseException as exc:
            journal.finish(run, 'failed', str(exc))
            raise
