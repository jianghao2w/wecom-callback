"""Run a confirmed batch only after validating the account session."""


def execute_confirmed_batch(store, batch_id, session_check, request_login, delete_comments):
    if not store.claim(batch_id):
        return "not_ready"
    batch = store.get(batch_id)
    try:
        session_state = session_check()
    except Exception:
        session_state = "unknown"

    if session_state == "expired":
        if not store.wait_for_login(batch_id):
            raise RuntimeError("无法保存等待授权状态")
        try:
            request_login(batch["chat_id"], batch_id)
        except Exception:
            # No deletion has been attempted; allow a later authorization retry.
            if not store.resume_after_login(batch_id):
                raise RuntimeError("无法保存授权重试状态")
            return "login_request_failed"
        return "waiting_login"
    if session_state != "valid":
        if not store.release_before_deletion(batch_id):
            raise RuntimeError("无法保存待重试状态")
        return "session_unknown"

    try:
        result = delete_comments(batch["payload"])
    except Exception as error:
        store.finish(batch_id, {"status": "unknown", "error": type(error).__name__})
        return "deletion_unknown"
    if not store.finish(batch_id, result):
        raise RuntimeError("无法保存删除结果")
    return "finished"
