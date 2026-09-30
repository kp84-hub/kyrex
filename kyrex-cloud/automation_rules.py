"""Durable owner-scoped email automation rules shared by Chat and the VPS gateway."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _connect():
    from paths import data_dir
    path = data_dir() / "email_automations.sqlite3"
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS email_automation_rules (
        rule_id TEXT PRIMARY KEY, owner TEXT NOT NULL, sender TEXT NOT NULL,
        bot_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(owner, sender, bot_id, conversation_id)
    )""")
    db.commit()
    for protected_path in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            os.chmod(protected_path, 0o600)
        except OSError:
            pass
    return db


def list_rules(owner: str) -> list[dict]:
    with _connect() as db:
        rows = db.execute(
            "SELECT rule_id, owner, sender, bot_id, conversation_id, enabled "
            "FROM email_automation_rules WHERE owner=? ORDER BY created_at, rule_id",
            (owner,)).fetchall()
    return [dict(row) for row in rows]


def create_rule(owner: str, sender: str, bot_id: str, conversation_id: str) -> dict:
    import uuid
    from datetime import datetime, timezone
    owner = str(owner or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    rule_id = uuid.uuid4().hex
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute(
            "SELECT count(*) FROM email_automation_rules WHERE owner=?", (owner,)
        ).fetchone()[0]
        if count >= 20:
            raise ValueError("At most 20 email rules are supported")
        db.execute("""INSERT INTO email_automation_rules
            (rule_id, owner, sender, bot_id, conversation_id, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                   (rule_id, owner, sender.lower(), bot_id, conversation_id, now, now))
    return next(rule for rule in list_rules(owner) if rule["rule_id"] == rule_id)


def set_enabled(owner: str, rule_id: str, enabled: bool) -> bool:
    from datetime import datetime, timezone
    with _connect() as db:
        changed = db.execute(
            "UPDATE email_automation_rules SET enabled=?, updated_at=? "
            "WHERE owner=? AND rule_id=?",
            (int(bool(enabled)), datetime.now(timezone.utc).isoformat(), owner, rule_id),
        ).rowcount
    return bool(changed)


def delete_rule(owner: str, rule_id: str) -> bool:
    with _connect() as db:
        changed = db.execute(
            "DELETE FROM email_automation_rules WHERE owner=? AND rule_id=?",
            (owner, rule_id)).rowcount
    return bool(changed)
