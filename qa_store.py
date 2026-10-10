"""SQLite-backed cache of application questions and answers."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "scanned_jobs.db"
MAX_STORED_OPTIONS = 25


def normalise(question):
    q = question.strip().lower()
    q = re.sub(r"\s+", " ", q)
    return re.sub(r"[^\w\s]", "", q)


class QAStore:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute(
                """CREATE TABLE IF NOT EXISTS qa_cache (
                    cache_key TEXT PRIMARY KEY,
                    entry_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

    def as_dict(self):
        with closing(sqlite3.connect(self.path)) as database:
            rows = database.execute("SELECT cache_key, entry_json FROM qa_cache").fetchall()
        return {key: json.loads(payload) for key, payload in rows}

    def replace(self, data):
        if not isinstance(data, dict):
            raise ValueError("QA cache must be a JSON object")
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("DELETE FROM qa_cache")
            database.executemany(
                "INSERT INTO qa_cache (cache_key, entry_json) VALUES (?, ?)",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in data.items()],
            )

    def get(self, question, field_type=None, options=None):
        with closing(sqlite3.connect(self.path)) as database:
            row = database.execute(
                "SELECT entry_json FROM qa_cache WHERE cache_key = ?",
                (normalise(question),),
            ).fetchone()
        if not row:
            return None
        entry = json.loads(row[0])
        if options and entry.get("answer") not in options:
            return None
        if field_type and entry.get("field_type") and entry["field_type"] != field_type:
            return None
        return entry.get("answer")

    def put(self, question, answer, field_type=None, options=None):
        stored = options or None
        if stored and len(stored) > MAX_STORED_OPTIONS:
            stored = stored[:MAX_STORED_OPTIONS] + [f"... +{len(options) - MAX_STORED_OPTIONS} more"]
        entry = {
            "question": question.strip(),
            "answer": answer,
            "field_type": field_type,
            "options": stored,
        }
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute(
                """INSERT INTO qa_cache (cache_key, entry_json) VALUES (?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    entry_json = excluded.entry_json,
                    updated_at = CURRENT_TIMESTAMP""",
                (normalise(question), json.dumps(entry, ensure_ascii=False)),
            )

    def __len__(self):
        with closing(sqlite3.connect(self.path)) as database:
            return database.execute("SELECT COUNT(*) FROM qa_cache").fetchone()[0]
