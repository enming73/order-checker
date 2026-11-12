import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


class Database:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "orders.sqlite3"
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, sha TEXT UNIQUE NOT NULL, filename TEXT NOT NULL,
                    raw BLOB NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'uploaded',
                    run_id TEXT, stage TEXT NOT NULL DEFAULT '', busy TEXT,
                    failure_step TEXT, failure_reason TEXT, fields TEXT NOT NULL DEFAULT '{}',
                    issues TEXT NOT NULL DEFAULT '[]', draft TEXT NOT NULL DEFAULT '',
                    model_response TEXT, subject TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL,
                    filename TEXT, page INTEGER, text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, filename TEXT NOT NULL, raw BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    step TEXT NOT NULL, started TEXT NOT NULL, duration_ms INTEGER,
                    result TEXT, reason TEXT, UNIQUE(case_id, run_id, step)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE, decision TEXT NOT NULL,
                    draft TEXT NOT NULL, reviewer TEXT NOT NULL, created TEXT NOT NULL,
                    snapshot TEXT NOT NULL, synced INTEGER NOT NULL DEFAULT 0, sync_error TEXT
                );
                CREATE TABLE IF NOT EXISTS erp_drawings (number TEXT PRIMARY KEY, revision TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS erp_results (
                    event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, created TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS faults (name TEXT PRIMARY KEY, mode TEXT NOT NULL);
            ''')
            db.executemany("INSERT OR IGNORE INTO erp_drawings VALUES (?, ?)",
                           [("SZ-4101", "A"), ("SZ-4102", "C"), ("SZ-4103", "C")])
            db.executemany("INSERT OR IGNORE INTO faults VALUES (?, 'normal')", [("read",), ("write",)])

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def case(self, case_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        result = dict(row)
        for key in ("fields", "issues"):
            result[key] = json.loads(result[key])
        return result

    def sources(self, case_id):
        with self.connect() as db:
            return [{**dict(r), "id": r["id"].split(":", 1)[1]} for r in db.execute("SELECT * FROM sources WHERE case_id=? ORDER BY kind, filename, page", (case_id,))]
