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
                "UPDATE deletion_batches SET state='executing' WHERE batch_id=? AND state='confirmed'",
                (batch_id,),
            )
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
