"""Check an existing video account session without making changes to the account."""
import json
import time
import urllib.error
import urllib.request

AUTH_URL = "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/get_auth_info"
SESSION_FIELDS = ("token", "_log_finder_uin", "_log_finder_id", "rawKeyBuff",
                  "pluginSessionId", "scene", "reqScene")


def classify_response(data):
    if not isinstance(data, dict):
        return "unknown"
    if data.get("errCode") == 300330:
        return "expired"
    details = data.get("data")
    if data.get("errCode") == 0 and isinstance(details, dict) and "authType" in details:
        return "valid"
    return "unknown"


def check_session(session, opener=urllib.request.urlopen):
    if not isinstance(session, dict) or not session.get("cookie"):
        return "expired"
    if any(field not in session for field in SESSION_FIELDS):
        return "unknown"
    body = {field: session[field] for field in SESSION_FIELDS}
    body["timestamp"] = str(int(time.time() * 1000))
    headers = {"Content-Type": "application/json", "Cookie": session["cookie"],
               "User-Agent": session.get("user_agent") or "Mozilla/5.0"}
    if session.get("wechat_uin"):
        headers["X-WECHAT-UIN"] = session["wechat_uin"]
    if session.get("device_id"):
        headers["finger-print-device-id"] = session["device_id"]
    request = urllib.request.Request(AUTH_URL, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with opener(request, timeout=10) as response:
            return classify_response(json.load(response))
    except (OSError, ValueError, json.JSONDecodeError):
        return "unknown"
