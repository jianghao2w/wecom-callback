"""Determine when a registered work is eligible for comment monitoring."""
import sqlite3
import time
from contextlib import contextmanager

HOUR = 3600
DAY = 24 * HOUR
BEIJING_OFFSET = 8 * HOUR
QUIET_END = 9 * HOUR


def resume_after_quiet_hours(timestamp):
    """Return the same time, or 09:00 Beijing time on that day."""
    second_of_day = (timestamp + BEIJING_OFFSET) % DAY
    return timestamp + max(0, QUIET_END - second_of_day)


def next_monitor_time(published_at, last_run_at=None):
    """Return the earliest next run; the first run starts six hours after publication."""
    published_at = int(published_at)
    if published_at <= 0:
        raise ValueError('作品发布时间无效')
    first = published_at + 6 * HOUR
    if last_run_at is None:
        return resume_after_quiet_hours(first)
    last_run_at = int(last_run_at)
    if last_run_at < first:
        return resume_after_quiet_hours(first)
    if last_run_at < published_at + 12 * HOUR:
        return resume_after_quiet_hours(last_run_at + HOUR)
    return resume_after_quiet_hours(last_run_at + 6 * HOUR)


def due_for_monitoring(published_at, now, last_run_at=None):
    now = int(now)
    return now == resume_after_quiet_hours(now) and now >= next_monitor_time(published_at, last_run_at)


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
        if now != resume_after_quiet_hours(now):
            return []
        with self._connect() as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT * FROM monitor_schedule WHERE lease_until<=?',(now,)).fetchall()
            selected=[]
            for row in rows:
                if due_for_monitoring(row['published_at'],now,row['last_success']):
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
