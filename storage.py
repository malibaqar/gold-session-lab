"""Durable pre-send journal and TradingView confirmation inbox."""
import json
import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "runtime.sqlite3"


class Store:
    def __init__(self, path=DB):
        self.db = sqlite3.connect(str(path), timeout=2)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS kv (scope TEXT, key TEXT, value TEXT,
                PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS scans (scope TEXT, key TEXT, at REAL,
                PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS attempts (scope TEXT, key TEXT, at REAL,
                status TEXT, payload TEXT, PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS alerts (ticker TEXT, timeframe TEXT,
                bar_open INTEGER, direction INTEGER, received REAL,
                PRIMARY KEY(ticker,timeframe,bar_open,direction));
        ''')

    def get(self, scope, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE scope=? AND key=?", (scope, key)).fetchone()
        return default if row is None else json.loads(row[0])

    def put(self, scope, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (scope, key, json.dumps(value)))

    def claim_scan(self, scope, key):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO scans VALUES (?,?,?)", (scope, key, time.time())).rowcount == 1

    def reserve(self, scope, key, payload):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO attempts VALUES (?,?,?,?,?)",
                                   (scope, key, time.time(), "sending", json.dumps(payload))).rowcount == 1

    def finish(self, scope, key, status, payload):
        with self.db:
            self.db.execute("UPDATE attempts SET status=?, payload=? WHERE scope=? AND key=?",
                            (status, json.dumps(payload), scope, key))

    def unresolved(self, scope):
        return self.db.execute("SELECT key FROM attempts WHERE scope=? AND status='sending'", (scope,)).fetchall()

    def attempt_count(self, scope, since):
        return self.db.execute("SELECT COUNT(*) FROM attempts WHERE scope=? AND at>=?", (scope, since)).fetchone()[0]

    def insert_alert(self, ticker, tf, bar, direction):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?)", (ticker, tf, bar, direction, time.time()))

    def confirmed(self, ticker, signal, now, max_age):
        row = self.db.execute("SELECT received FROM alerts WHERE ticker=? AND timeframe=? AND bar_open=? AND direction=?",
                              (ticker, signal.timeframe, signal.bar_open, signal.direction)).fetchone()
        return row is not None and 0 <= now - signal.closes_at <= max_age and now - row[0] <= max_age
