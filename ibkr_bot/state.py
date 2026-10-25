"""Durable order journal. Ambiguous submissions never auto-retry."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime, timezone


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.directory / 'journal.sqlite', timeout=10)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, cycle_key TEXT NOT NULL, status TEXT NOT NULL,
            created TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL, error TEXT);
          CREATE TABLE IF NOT EXISTS orders (
            ref TEXT PRIMARY KEY, run_id TEXT NOT NULL, intent TEXT NOT NULL,
            status TEXT NOT NULL, broker TEXT NOT NULL, updated TEXT NOT NULL);
        ''')

    def close(self):
        self.connection.close()

    @contextmanager
    def lock(self):
        with (self.directory / 'execution.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('Another trading process holds the execution lock') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def begin(self, key, payload):
        rows = self.connection.execute('SELECT id,status FROM runs WHERE cycle_key=? ORDER BY rowid DESC', (key,)).fetchall()
        if any(status in {'completed', 'completed_with_shortfall'} for _, status in rows):
            return None
        account_prefix = key.split('|', 1)[0] + '|'
        unresolved = self.connection.execute(
            "SELECT id,status FROM runs WHERE substr(cycle_key,1,?)=? AND status IN ('executing','failed') ORDER BY rowid DESC",
            (len(account_prefix), account_prefix)).fetchall()
        if unresolved:
            raise RuntimeError(f'Unresolved previous run {unresolved[0][0]}; reconcile before resuming')
        run = uuid.uuid4().hex[:20]
        with self.connection:
            self.connection.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',
                                    (run, key, 'executing', timestamp(), timestamp(), json.dumps(payload), None))
        return run

    def finish(self, run, status, error=None):
        with self.connection:
            self.connection.execute('UPDATE runs SET status=?,updated=?,error=? WHERE id=?',
                                    (status, timestamp(), error, run))

    def record_intent(self, run, intent, sequence):
        ref = f'ETF-{run}-{sequence}'
        # Commit before submitting. A crash here is intentionally ambiguous.
        with self.connection:
            self.connection.execute('INSERT INTO orders VALUES (?,?,?,?,?,?)',
                                    (ref, run, json.dumps(intent.__dict__), 'pending_submit', '{}', timestamp()))
        return ref

    def update_order(self, ref, status, broker):
        with self.connection:
            self.connection.execute('UPDATE orders SET status=?,broker=?,updated=? WHERE ref=?',
                                    (status, json.dumps(broker), timestamp(), ref))

    def inspect(self):
        return {'runs': [dict(zip(['id','cycle_key','status','created','updated','payload','error'], row))
                         for row in self.connection.execute('SELECT * FROM runs ORDER BY rowid')],
                'orders': [dict(zip(['ref','run_id','intent','status','broker','updated'], row))
                           for row in self.connection.execute('SELECT * FROM orders ORDER BY rowid')]}

    def resolve(self, run, broker_report, account):
        row = self.connection.execute('SELECT status,cycle_key FROM runs WHERE id=?', (run,)).fetchone()
        if not row or row[0] not in {'failed', 'executing'}:
            raise ValueError('Only failed/interrupted runs can be resolved')
        if row[1].split('|', 1)[0] != account:
            raise ValueError('Run belongs to another account; resolution refused')
        (self.directory / f'reconciliation-{run}.json').write_text(json.dumps(broker_report, indent=2))
        self.finish(run, 'resolved', 'Operator audited broker orders, executions and positions; no working orders')
