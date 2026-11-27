"""Persistent approval state for comment deletion batches."""
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager


class ApprovalStore:
    def __init__(self, database_path):
        self.database_path = database_path
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS deletion_batches (
                    batch_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    confirmed_by TEXT,
                    confirmed_at INTEGER,
                    result TEXT
                )"""
            )
            connection.execute('CREATE TABLE IF NOT EXISTS local_login_notices (batch_id TEXT PRIMARY KEY)')
            connection.execute('CREATE TABLE IF NOT EXISTS webhook_messages (message_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS cloud_settings (name TEXT PRIMARY KEY)')
            connection.execute('''CREATE TABLE IF NOT EXISTS cloud_jobs (
                batch_id TEXT PRIMARY KEY, inflight TEXT, deleted TEXT NOT NULL DEFAULT '[]')''')

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, chat_id, comments, *, announcing=False):
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ValueError("缺少群聊标识")
        if not isinstance(comments, list) or not comments:
            raise ValueError("待确认评论为空")
        payload = json.dumps(comments, ensure_ascii=False, separators=(",", ":"))
        batch_id = secrets.token_hex(6)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO deletion_batches (batch_id,chat_id,payload,state,created_at) VALUES (?,?,?,?,?)",
                (batch_id, chat_id, payload, "announcing" if announcing else "pending", int(time.time())),
            )
        return batch_id

    def mark_announced(self, batch_id):
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deletion_batches SET state='pending' WHERE batch_id=? AND state='announcing'",
                (batch_id,),
            )
            return cursor.rowcount == 1

    def fail_announcement(self, batch_id):
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deletion_batches SET state='delivery_failed' WHERE batch_id=? AND state IN ('announcing','pending')",
                (batch_id,),
            )
            return cursor.rowcount == 1

    def get(self, batch_id):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM deletion_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        if result["result"] is not None:
            result["result"] = json.loads(result["result"])
        return result

    def confirm(self, batch_id, chat_id, user_id):
        if not chat_id or not user_id:
            raise ValueError("确认消息缺少群聊或发送人")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE deletion_batches SET state='confirmed',confirmed_by=?,confirmed_at=?
                   WHERE batch_id=? AND chat_id=? AND state='pending'""",
                (user_id, int(time.time()), batch_id, chat_id),
            )
            return cursor.rowcount == 1

    def claim(self, batch_id):
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE deletion_batches SET state='executing' WHERE batch_id=? AND state='confirmed'
                   AND NOT EXISTS (SELECT 1 FROM cloud_settings WHERE name='enabled')
                   AND NOT EXISTS (SELECT 1 FROM cloud_jobs WHERE batch_id=?)""",
                (batch_id, batch_id),
            )
            return cursor.rowcount == 1

    def confirmed_batches(self):
        with self._connect() as connection:
            rows = connection.execute("SELECT batch_id FROM deletion_batches WHERE state='confirmed' ORDER BY confirmed_at").fetchall()
            return [row['batch_id'] for row in rows]

    def claim_webhook_message(self, message_id):
        if not isinstance(message_id,str) or not message_id.strip():
            raise ValueError('消息标识缺失')
        now=int(time.time())
        with self._connect() as connection:
            connection.execute('DELETE FROM webhook_messages WHERE received_at < ?', (now-7*86400,))
            cursor=connection.execute('INSERT OR IGNORE INTO webhook_messages(message_id,received_at) VALUES (?,?)',(message_id,now))
            return cursor.rowcount == 1

    def mark_login_notified(self, batch_id):
        with self._connect() as connection:
            cursor=connection.execute("""INSERT OR IGNORE INTO local_login_notices(batch_id)
                SELECT batch_id FROM deletion_batches WHERE batch_id=? AND state='confirmed'""", (batch_id,))
            return cursor.rowcount == 1

    def wait_for_login(self, batch_id):
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deletion_batches SET state='waiting_login' WHERE batch_id=? AND state='executing'",
                (batch_id,),
            )
            return cursor.rowcount == 1

    def release_before_deletion(self, batch_id):
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deletion_batches SET state='confirmed' WHERE batch_id=? AND state='executing'",
                (batch_id,),
            )
            return cursor.rowcount == 1

    def resume_after_login(self, batch_id):
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deletion_batches SET state='confirmed' WHERE batch_id=? AND state='waiting_login'",
                (batch_id,),
            )
            return cursor.rowcount == 1

    def finish(self, batch_id, result):
        value = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deletion_batches SET state='finished',result=? WHERE batch_id=? AND state='executing'",
                (value, batch_id),
            )
            return cursor.rowcount == 1

    def cloud_enabled(self):
        with self._connect() as db:
            return db.execute("SELECT 1 FROM cloud_settings WHERE name='enabled'").fetchone() is not None

    def cloud_enrollment_armed(self):
        with self._connect() as db:
            return db.execute("SELECT 1 FROM cloud_settings WHERE name='enroll'").fetchone() is not None

    def arm_cloud_enrollment(self):
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO cloud_settings VALUES ('enroll')")

    def enable_cloud(self):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM deletion_batches WHERE state='executing'").fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO cloud_settings VALUES ('enabled')")
            db.execute("DELETE FROM cloud_settings WHERE name='enroll'")
            return True

    def claim_cloud(self, batch_id):
        with self._connect() as db:
            result = db.execute("UPDATE deletion_batches SET state='executing' WHERE batch_id=? AND state='confirmed'", (batch_id,))
            if result.rowcount != 1:
                return None
            db.execute('INSERT OR IGNORE INTO cloud_jobs (batch_id) VALUES (?)', (batch_id,))
        return self.get(batch_id)

    def cloud_deleted(self, batch_id):
        with self._connect() as db:
            return json.loads(db.execute('SELECT deleted FROM cloud_jobs WHERE batch_id=?', (batch_id,)).fetchone()[0])

    def begin_cloud_item(self, batch_id, comment_id):
        with self._connect() as db:
            if db.execute('UPDATE cloud_jobs SET inflight=? WHERE batch_id=? AND inflight IS NULL', (comment_id, batch_id)).rowcount != 1:
                raise RuntimeError('云端任务已有未确认的删除')

    def complete_cloud_item(self, batch_id, comment_id):
        with self._connect() as db:
            deleted = json.loads(db.execute('SELECT deleted FROM cloud_jobs WHERE batch_id=? AND inflight=?', (batch_id, comment_id)).fetchone()[0])
            db.execute('UPDATE cloud_jobs SET inflight=NULL, deleted=? WHERE batch_id=?', (json.dumps(deleted + [comment_id]), batch_id))

    def wait_cloud(self, batch_id, comment_id=None):
        with self._connect() as db:
            if comment_id is not None:
                if db.execute('UPDATE cloud_jobs SET inflight=NULL WHERE batch_id=? AND inflight=?', (batch_id, comment_id)).rowcount != 1:
                    raise RuntimeError('云端任务状态异常')
            db.execute("UPDATE deletion_batches SET state='waiting_login' WHERE batch_id=? AND state='executing'", (batch_id,))

    def cloud_waiting(self):
        with self._connect() as db:
            return db.execute("SELECT 1 FROM deletion_batches JOIN cloud_jobs USING(batch_id) WHERE state='waiting_login'").fetchone() is not None

    def resume_cloud(self):
        with self._connect() as db:
            db.execute("""UPDATE deletion_batches SET state='confirmed' WHERE state='waiting_login'
                AND batch_id IN (SELECT batch_id FROM cloud_jobs WHERE inflight IS NULL)""")

    def recover_cloud(self):
        """Stop on uncertain deletes, but requeue work that never issued a delete."""
        uncertain = 0
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT cloud_jobs.* FROM cloud_jobs JOIN deletion_batches USING(batch_id) WHERE state='executing'").fetchall()
            for row in rows:
                if row['inflight']:
                    result = {'status': 'unknown', 'deleted': json.loads(row['deleted']), 'uncertain': row['inflight']}
                    db.execute("UPDATE deletion_batches SET state='finished', result=? WHERE batch_id=?", (json.dumps(result), row['batch_id']))
                    uncertain += 1
                else:
                    db.execute("UPDATE deletion_batches SET state='confirmed' WHERE batch_id=?", (row['batch_id'],))
        return uncertain
