#!/usr/bin/env python3
"""Durable per-profile chat queue, transcript, event journal, and outbox."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


SCHEMA_VERSION = 1


class SessionStoreError(Exception):
    pass


class SessionStore:
    """Small SQLite store shared by Telegram bridges and terminal clients."""

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not parent_existed:
            self.path.parent.chmod(0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout = 30000')
        db.execute('PRAGMA foreign_keys = ON')
        if self.path.exists():
            self.path.chmod(0o600)
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with self._connection() as db:
            db.execute('PRAGMA journal_mode = WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('telegram', 'terminal')),
                    source_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('chat', 'clear')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'failed')),
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT,
                    UNIQUE(source, source_key)
                );
                CREATE INDEX IF NOT EXISTS requests_profile_status
                    ON requests(profile, status, id);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    request_id INTEGER REFERENCES requests(id),
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_profile_id ON messages(profile, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    request_id INTEGER REFERENCES requests(id),
                    kind TEXT NOT NULL CHECK(kind IN ('user', 'assistant', 'clear', 'error')),
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_profile_id ON events(profile, id);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent')),
                    message_id INTEGER,
                    created_at REAL NOT NULL,
                    sent_at REAL,
                    UNIQUE(request_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS outbox_profile_status
                    ON outbox(profile, status, id);
                CREATE TABLE IF NOT EXISTS legacy_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    imported_at REAL NOT NULL,
                    UNIQUE(profile, source_kind, source_name, digest)
                );
            ''')
            row = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if row and int(row['value']) != SCHEMA_VERSION:
                raise SessionStoreError('Unsupported session database schema version')
            db.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
                       (str(SCHEMA_VERSION),))

    @contextmanager
    def worker_lock(self, profile):
        """Hold the unique processor lease for one profile until the context exits."""
        lock_dir = self.path.parent / (self.path.name + '.workers')
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_dir.chmod(0o700)
        safe = hashlib.sha256(profile.encode()).hexdigest()[:20]
        fd = os.open(lock_dir / (safe + '.lock'), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'w') as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionStoreError('Another bridge already processes profile ' + profile) from None
            yield

    def enqueue(self, profile, source, source_key, kind, content):
        now = time.time()
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute(
                'SELECT id FROM requests WHERE source=? AND source_key=?',
                (source, source_key)).fetchone()
            if existing:
                return existing['id'], False
            cursor = db.execute('''
                INSERT INTO requests(profile, source, source_key, kind, content, status, created_at)
                VALUES(?, ?, ?, ?, ?, 'queued', ?)
            ''', (profile, source, source_key, kind, content, now))
            request_id = cursor.lastrowid
            if kind == 'chat':
                db.execute('''
                    INSERT INTO events(profile, request_id, kind, source, content, created_at)
                    VALUES(?, ?, 'user', ?, ?, ?)
                ''', (profile, request_id, source, content, now))
            return request_id, True

    def recover(self, profiles):
        profiles = tuple(profiles)
        if not profiles:
            return 0
        placeholders = ','.join('?' for _ in profiles)
        with self._connection() as db:
            cursor = db.execute(
                f"UPDATE requests SET status='queued', started_at=NULL "
                f"WHERE status='running' AND profile IN ({placeholders})", profiles)
            return cursor.rowcount

    def claim(self, profiles):
        profiles = tuple(profiles)
        if not profiles:
            return None
        placeholders = ','.join('?' for _ in profiles)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                f"SELECT * FROM requests WHERE status='queued' "
                f"AND profile IN ({placeholders}) ORDER BY id LIMIT 1", profiles).fetchone()
            if not row:
                return None
            db.execute("UPDATE requests SET status='running', started_at=? WHERE id=?",
                       (time.time(), row['id']))
            return dict(row)

    def history(self, profile, turns=None):
        limit = ''
        params = [profile]
        if turns is not None:
            limit = ' LIMIT ?'
            params.append(max(1, int(turns)) * 2)
        with self._connection() as db:
            if turns is None:
                rows = db.execute(
                    'SELECT role, content FROM messages WHERE profile=? ORDER BY id' + limit,
                    params).fetchall()
            else:
                rows = db.execute('''
                    SELECT role, content FROM (
                        SELECT id, role, content FROM messages
                        WHERE profile=? ORDER BY id DESC LIMIT ?
                    ) ORDER BY id
                ''', params).fetchall()
        return [{'role': row['role'], 'content': row['content']} for row in rows]

    def message_count(self, profile):
        with self._connection() as db:
            return db.execute('SELECT COUNT(*) AS value FROM messages WHERE profile=?',
                              (profile,)).fetchone()['value']

    def _queue_outbox(self, db, profile, request_id, texts, now):
        for ordinal, text in enumerate(texts):
            db.execute('''
                INSERT INTO outbox(profile, request_id, ordinal, text, created_at)
                VALUES(?, ?, ?, ?, ?)
            ''', (profile, request_id, ordinal, text, now))

    def complete_chat(self, request, answer, history_turns, deliveries):
        now = time.time()
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT status FROM requests WHERE id=?',
                                 (request['id'],)).fetchone()
            if not current or current['status'] != 'running':
                raise SessionStoreError('Request is not running')
            db.execute('''
                INSERT INTO messages(profile, request_id, role, content, created_at)
                VALUES(?, ?, 'user', ?, ?), (?, ?, 'assistant', ?, ?)
            ''', (request['profile'], request['id'], request['content'], now,
                  request['profile'], request['id'], answer, now))
            keep = max(1, int(history_turns)) * 2
            db.execute('''
                DELETE FROM messages WHERE profile=? AND id NOT IN (
                    SELECT id FROM messages WHERE profile=? ORDER BY id DESC LIMIT ?
                )
            ''', (request['profile'], request['profile'], keep))
            db.execute('''
                INSERT INTO events(profile, request_id, kind, source, content, created_at)
                VALUES(?, ?, 'assistant', 'bridge', ?, ?)
            ''', (request['profile'], request['id'], answer, now))
            self._queue_outbox(db, request['profile'], request['id'], deliveries, now)
            db.execute("UPDATE requests SET status='done', finished_at=? WHERE id=?",
                       (now, request['id']))

    def complete_clear(self, request, confirmation, deliveries):
        now = time.time()
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM messages WHERE profile=?', (request['profile'],))
            db.execute('''
                INSERT INTO events(profile, request_id, kind, source, content, created_at)
                VALUES(?, ?, 'clear', ?, ?, ?)
            ''', (request['profile'], request['id'], request['source'], confirmation, now))
            self._queue_outbox(db, request['profile'], request['id'], deliveries, now)
            db.execute("UPDATE requests SET status='done', finished_at=? WHERE id=?",
                       (now, request['id']))

    def fail(self, request, message, deliveries):
        now = time.time()
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''
                INSERT INTO events(profile, request_id, kind, source, content, created_at)
                VALUES(?, ?, 'error', 'bridge', ?, ?)
            ''', (request['profile'], request['id'], message, now))
            self._queue_outbox(db, request['profile'], request['id'], deliveries, now)
            db.execute("UPDATE requests SET status='failed', error=?, finished_at=? WHERE id=?",
                       (message, now, request['id']))

    def pending_outbox(self, profiles):
        profiles = tuple(profiles)
        if not profiles:
            return None
        placeholders = ','.join('?' for _ in profiles)
        with self._connection() as db:
            row = db.execute(
                f"SELECT * FROM outbox WHERE status='pending' "
                f"AND profile IN ({placeholders}) ORDER BY id LIMIT 1", profiles).fetchone()
            return dict(row) if row else None

    def mark_sent(self, item_id, message_id):
        with self._connection() as db:
            db.execute("UPDATE outbox SET status='sent', message_id=?, sent_at=? "
                       "WHERE id=? AND status='pending'", (message_id, time.time(), item_id))

    def latest_event_id(self, profile):
        with self._connection() as db:
            row = db.execute('SELECT COALESCE(MAX(id), 0) AS value FROM events WHERE profile=?',
                             (profile,)).fetchone()
            return row['value']

    def terminal_snapshot(self, profile):
        """Return startup history, unfinished inputs, and cursor from one read view."""
        with self._connection() as db:
            db.execute('BEGIN')
            history_rows = db.execute(
                'SELECT role, content FROM messages WHERE profile=? ORDER BY id',
                (profile,)).fetchall()
            pending_rows = db.execute('''
                SELECT e.*, r.source_key FROM events e
                JOIN requests r ON r.id=e.request_id
                WHERE e.profile=? AND e.kind='user'
                    AND r.status IN ('queued', 'running')
                ORDER BY e.id
            ''', (profile,)).fetchall()
            cursor = db.execute(
                'SELECT COALESCE(MAX(id), 0) AS value FROM events WHERE profile=?',
                (profile,)).fetchone()['value']
        history = [
            {'role': row['role'], 'content': row['content']} for row in history_rows
        ]
        return history, [dict(row) for row in pending_rows], cursor

    def events_since(self, profile, event_id):
        with self._connection() as db:
            rows = db.execute('''
                SELECT e.*, r.source_key FROM events e
                LEFT JOIN requests r ON r.id=e.request_id
                WHERE e.profile=? AND e.id>? ORDER BY e.id
            ''', (profile, event_id)).fetchall()
            return [dict(row) for row in rows]

    def archive_and_replace(self, profile, candidates, active_rows):
        """Archive every legacy candidate and seed an empty active profile atomically."""
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT COUNT(*) AS value FROM messages WHERE profile=?',
                               (profile,)).fetchone()['value']
            if count:
                raise SessionStoreError('Shared history is not empty for profile ' + profile)
            now = time.time()
            for candidate in candidates:
                payload = json.dumps(candidate['rows'], ensure_ascii=False, separators=(',', ':'))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                db.execute('''
                    INSERT OR IGNORE INTO legacy_imports(
                        profile, source_kind, source_name, digest, rows_json, imported_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                ''', (profile, candidate['kind'], candidate['name'], digest, payload, now))
            for row in active_rows:
                db.execute('''
                    INSERT INTO messages(profile, role, content, created_at)
                    VALUES(?, ?, ?, ?)
                ''', (profile, row['role'], row['content'], now))

    def legacy_import_count(self, profile):
        with self._connection() as db:
            return db.execute('SELECT COUNT(*) AS value FROM legacy_imports WHERE profile=?',
                              (profile,)).fetchone()['value']
