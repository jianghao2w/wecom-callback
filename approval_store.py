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
            connection.execute('BEGIN IMMEDIATE')
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
            if 'account_key' not in {row['name'] for row in connection.execute('PRAGMA table_info(cloud_jobs)')}:
                connection.execute("ALTER TABLE cloud_jobs ADD COLUMN account_key TEXT NOT NULL DEFAULT 'default'")
            connection.execute('''CREATE TABLE IF NOT EXISTS cloud_routes (
                batch_id TEXT PRIMARY KEY, account_key TEXT NOT NULL)''')
            connection.execute('CREATE INDEX IF NOT EXISTS cloud_routes_account ON cloud_routes(account_key)')
            connection.execute('''CREATE TABLE IF NOT EXISTS cloud_accounts (
                account_key TEXT PRIMARY KEY, name TEXT NOT NULL, anchor_id TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 0, armed INTEGER NOT NULL DEFAULT 0)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS cloud_video_bindings (
                video_id TEXT PRIMARY KEY, account_key TEXT NOT NULL)''')

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
                   AND NOT EXISTS (SELECT 1 FROM cloud_jobs WHERE batch_id=?)
                   AND NOT EXISTS (SELECT 1 FROM cloud_routes WHERE batch_id=?)""",
                (batch_id, batch_id, batch_id),
            )
            return cursor.rowcount == 1

    def confirmed_batches(self, account_key=None):
        with self._connect() as connection:
            sql = "SELECT batch_id FROM deletion_batches WHERE state='confirmed'"
            if account_key is not None:
                sql += ' AND batch_id IN (SELECT batch_id FROM cloud_routes WHERE account_key=?)'
            rows = connection.execute(sql+' ORDER BY confirmed_at', (account_key,) if account_key is not None else ()).fetchall()
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

    def cloud_enabled(self, account_key='default'):
        with self._connect() as db:
            if account_key != 'default':
                row = db.execute('SELECT enabled FROM cloud_accounts WHERE account_key=?',(account_key,)).fetchone()
                return bool(row and row[0])
            return db.execute("SELECT 1 FROM cloud_settings WHERE name='enabled'").fetchone() is not None

    def cloud_enrollment_armed(self, account_key='default'):
        with self._connect() as db:
            if account_key != 'default':
                row = db.execute('SELECT armed FROM cloud_accounts WHERE account_key=?',(account_key,)).fetchone()
                return bool(row and row[0])
            return db.execute("SELECT 1 FROM cloud_settings WHERE name='enroll'").fetchone() is not None

    def arm_cloud_enrollment(self, account_key='default'):
        with self._connect() as db:
            if account_key != 'default':
                db.execute('UPDATE cloud_accounts SET armed=1 WHERE account_key=?',(account_key,))
                return
            db.execute("INSERT OR IGNORE INTO cloud_settings VALUES ('enroll')")

    def enable_cloud(self, account_key='default'):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if account_key != 'default':
                return db.execute('UPDATE cloud_accounts SET enabled=1,armed=0 WHERE account_key=?',(account_key,)).rowcount == 1
            if db.execute("SELECT 1 FROM deletion_batches WHERE state='executing'").fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO cloud_settings VALUES ('enabled')")
            db.execute("DELETE FROM cloud_settings WHERE name='enroll'")
            return True

    def claim_cloud(self, batch_id, account_key='default', routed=False):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            route = db.execute('SELECT account_key FROM cloud_routes WHERE batch_id=?',(batch_id,)).fetchone()
            old_job = db.execute('SELECT account_key FROM cloud_jobs WHERE batch_id=?',(batch_id,)).fetchone()
            if (routed and not route) or (route and route[0] != account_key) or (old_job and old_job[0] != account_key):
                return None
            result = db.execute("UPDATE deletion_batches SET state='executing' WHERE batch_id=? AND state='confirmed'", (batch_id,))
            if result.rowcount != 1:
                return None
            db.execute('INSERT OR IGNORE INTO cloud_jobs (batch_id,account_key) VALUES (?,?)', (batch_id,account_key))
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

    def cloud_waiting(self, account_key='default'):
        with self._connect() as db:
            return db.execute("SELECT 1 FROM deletion_batches JOIN cloud_jobs USING(batch_id) WHERE state='waiting_login' AND account_key=?",(account_key,)).fetchone() is not None

    def resume_cloud(self, account_key='default'):
        with self._connect() as db:
            db.execute("""UPDATE deletion_batches SET state='confirmed' WHERE state='waiting_login'
                AND batch_id IN (SELECT batch_id FROM cloud_jobs WHERE inflight IS NULL AND account_key=?)""",(account_key,))

    def recover_cloud(self, account_key='default'):
        """Stop on uncertain deletes, but requeue work that never issued a delete."""
        uncertain = 0
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT cloud_jobs.* FROM cloud_jobs JOIN deletion_batches USING(batch_id) WHERE state='executing' AND account_key=?",(account_key,)).fetchall()
            for row in rows:
                if row['inflight']:
                    result = {'status': 'unknown', 'deleted': json.loads(row['deleted']), 'uncertain': row['inflight']}
                    db.execute("UPDATE deletion_batches SET state='finished', result=? WHERE batch_id=?", (json.dumps(result), row['batch_id']))
                    uncertain += 1
                else:
                    db.execute("UPDATE deletion_batches SET state='confirmed' WHERE batch_id=?", (row['batch_id'],))
        return uncertain

    def add_cloud_account(self, name, anchor_id, key=None):
        key = key or secrets.token_hex(5)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM cloud_accounts WHERE anchor_id=?',(anchor_id,)).fetchone()
            if existing:
                return dict(existing)
            db.execute('INSERT INTO cloud_accounts(account_key,name,anchor_id) VALUES (?,?,?)',(key,name,anchor_id))
            return dict(db.execute('SELECT * FROM cloud_accounts WHERE account_key=?',(key,)).fetchone())

    def cloud_accounts(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM cloud_accounts ORDER BY rowid')]

    def cloud_video_account(self, video_id):
        with self._connect() as db:
            row = db.execute('SELECT account_key FROM cloud_video_bindings WHERE video_id=?',(video_id,)).fetchone()
            return row[0] if row else None

    def bind_cloud_video(self, video_id, key):
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO cloud_video_bindings VALUES (?,?)',(video_id,key))
            return db.execute('SELECT account_key FROM cloud_video_bindings WHERE video_id=?',(video_id,)).fetchone()[0] == key

    def route_cloud(self, batch_id, key):
        with self._connect() as db:
            db.execute('''INSERT OR IGNORE INTO cloud_routes (batch_id,account_key)
                SELECT batch_id,? FROM deletion_batches WHERE batch_id=? AND (state='confirmed'
                    OR EXISTS (SELECT 1 FROM cloud_jobs WHERE batch_id=? AND account_key=?))
                AND NOT EXISTS (SELECT 1 FROM cloud_jobs WHERE batch_id=? AND account_key<>?)''',
                (key,batch_id,batch_id,key,batch_id,key))
            row = db.execute('SELECT account_key FROM cloud_routes WHERE batch_id=?',(batch_id,)).fetchone()
            return bool(row and row[0] == key)

    def unassigned_confirmed(self):
        with self._connect() as db:
            return [(row['batch_id'], json.loads(row['payload']),row['old_account']) for row in db.execute('''
                SELECT d.batch_id,d.payload,j.account_key AS old_account FROM deletion_batches d
                LEFT JOIN cloud_jobs j USING(batch_id) LEFT JOIN cloud_routes r USING(batch_id)
                WHERE d.state IN ('confirmed','waiting_login','executing') AND r.batch_id IS NULL
                AND (d.state='confirmed' OR j.batch_id IS NOT NULL)''')]

    def hold_cloud_owner(self, batch_id):
        with self._connect() as db:
            db.execute("UPDATE deletion_batches SET state='waiting_owner' WHERE batch_id=? AND state='executing'",(batch_id,))

    def requeue_cloud_owner(self, video_id, key):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute('''SELECT d.batch_id,j.account_key,j.deleted FROM deletion_batches d
                JOIN cloud_jobs j USING(batch_id) WHERE state='waiting_owner' AND inflight IS NULL
                AND json_extract(payload,'$[0].object_id')=?''',(video_id,)).fetchall()
            for row in pending:
                if json.loads(row['deleted']) and row['account_key'] != key:
                    continue
                db.execute('UPDATE cloud_routes SET account_key=? WHERE batch_id=?',(key,row['batch_id']))
                db.execute('UPDATE cloud_jobs SET account_key=? WHERE batch_id=?',(key,row['batch_id']))
                db.execute("UPDATE deletion_batches SET state='confirmed' WHERE batch_id=?",(row['batch_id'],))
