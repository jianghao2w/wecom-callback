"""Determine when a registered work is eligible for comment monitoring."""
HOUR = 3600
# Per-work cadence requested for the video published on 2026-11-26 at 14:46.
WORK_INTERVAL_OVERRIDES = {('channels', '15063218626332068385'): HOUR}
import sqlite3
import time
from contextlib import contextmanager


def next_monitor_time(published_at, last_run_at=None, interval_seconds=None):
    """Return the earliest next run; the first run starts six hours after publication."""
    published_at = int(published_at)
    if published_at <= 0:
        raise ValueError('作品发布时间无效')
    first = published_at + 6 * HOUR
    if last_run_at is None:
        return first
    last_run_at = int(last_run_at)
    if last_run_at < first:
        return first
    if interval_seconds is not None:
        if int(interval_seconds) <= 0:
            raise ValueError('监控间隔无效')
        return last_run_at + int(interval_seconds)
    if last_run_at < published_at + 12 * HOUR:
        return last_run_at + HOUR
    return last_run_at + 6 * HOUR


def due_for_monitoring(published_at, now, last_run_at=None, interval_seconds=None):
    return int(now) >= next_monitor_time(published_at, last_run_at, interval_seconds)


class ScheduleStore:
    def __init__(self, path):
        self.path = path
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS monitor_schedule (
                platform TEXT NOT NULL, content_id TEXT NOT NULL, published_at INTEGER NOT NULL,
                chat_id TEXT NOT NULL, last_success INTEGER, lease_until INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(platform,content_id))''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, platform, content_id, published_at, chat_id):
        next_monitor_time(published_at)
        if not chat_id:
            raise ValueError('缺少群聊标识')
        with self._connect() as db:
            db.execute('''INSERT INTO monitor_schedule(platform,content_id,published_at,chat_id)
                          VALUES(?,?,?,?) ON CONFLICT(platform,content_id) DO UPDATE SET
                          published_at=excluded.published_at, chat_id=excluded.chat_id''',
                       (platform,content_id,int(published_at),chat_id))

    def claim_due(self, now=None):
        now=int(time.time() if now is None else now)
        with self._connect() as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT * FROM monitor_schedule WHERE lease_until<=?',(now,)).fetchall()
            selected=[]
            for row in rows:
                interval = WORK_INTERVAL_OVERRIDES.get((row['platform'], row['content_id']))
                if due_for_monitoring(row['published_at'],now,row['last_success'],interval):
                    db.execute('UPDATE monitor_schedule SET lease_until=? WHERE platform=? AND content_id=?',
                               (now+1800,row['platform'],row['content_id']))
                    selected.append(dict(row))
            return selected

    def complete(self, platform, content_id, success, now=None):
        now=int(time.time() if now is None else now)
        with self._connect() as db:
            if success:
                db.execute('UPDATE monitor_schedule SET last_success=?,lease_until=0 WHERE platform=? AND content_id=?',
                           (now,platform,content_id))
            else:
                db.execute('UPDATE monitor_schedule SET lease_until=? WHERE platform=? AND content_id=?',
                           (now+300,platform,content_id))
