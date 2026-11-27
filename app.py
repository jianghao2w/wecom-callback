#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微机器人 URL 回调服务：接收群消息，检测链接，触发 Dify 监控"""
import os
import json
import hashlib
import hmac
import base64
import re
import sqlite3
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from approval_store import ApprovalStore
from approval_coordinator import execute_confirmed_batch
from channels_delete import delete_verified_batch
from channels_session_probe import check_session
from monitor_schedule import ScheduleStore
from deepseek_batch import DeepSeekBatch

app = Flask(__name__)

# ===== 配置（Railway 环境变量里填）=====
WECOM_TOKEN = os.environ.get("WECOM_CALLBACK_TOKEN", "")
WECOM_AES_KEY = os.environ.get("WECOM_CALLBACK_AES_KEY", "")
WECOM_BOT_WEBHOOK = os.environ.get("WECOM_BOT_WEBHOOK", "")  # 群机器人 Webhook，用于回复确认
DIFY_API_URL = os.environ.get("DIFY_API_URL", "")  # Dify 工作流 API 地址
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")  # Dify API Key
TARGETS_FILE = os.environ.get("TARGETS_FILE", "/data/targets.json")  # 本地文件存储（未配置 GitHub 时使用；容器不持久）
TARGETS_API_TOKEN = os.environ.get("TARGETS_API_TOKEN", "")  # /targets 接口访问口令（可选，设置后需 ?token= 才能读）
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")        # 清单存储仓库，如 jianghao2w/wecom-callback
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")      # GitHub PAT（Contents 读写权限）
GITHUB_DATA_BRANCH = os.environ.get("GITHUB_DATA_BRANCH", "data")  # 数据分支（避开 main，防止触发 Railway 重新部署）
TARGETS_PATH = os.environ.get("TARGETS_PATH", "data/targets.json") # 清单文件在仓库内的路径
TIKHUB_TOKEN = os.environ.get("TIKHUB_API_TOKEN", "")  # TikHub Token：视频号分享链接换数字ID用
COMMENTS_PATH = os.environ.get("COMMENTS_PATH", "data/comment-cache")
COLLECTION_PAGE_LIMIT = max(1, int(os.environ.get("COLLECTION_PAGE_LIMIT", "200")))
COLLECTION_REQUEST_TIMEOUT = max(3, int(os.environ.get("COLLECTION_REQUEST_TIMEOUT", "20")))
APPROVAL_DB_PATH = os.environ.get("APPROVAL_DB_PATH", "")
APPROVAL_API_TOKEN = os.environ.get("APPROVAL_API_TOKEN", "")
APPROVAL_CHAT_ID = os.environ.get("APPROVAL_CHAT_ID", "")
APPROVAL_EXECUTION_ENABLED = os.environ.get("APPROVAL_EXECUTION_ENABLED", "").lower() == "true"
LOCAL_WORKER_ENABLED = os.environ.get("LOCAL_WORKER_ENABLED", "true").lower() == "true"
LOCAL_WORKER_TOKEN = (os.environ.get("LOCAL_WORKER_TOKEN", "") or
    (hmac.new(APPROVAL_API_TOKEN.encode('utf-8'), b'local-worker-v1', hashlib.sha256).hexdigest()
     if APPROVAL_API_TOKEN else ""))
CHANNELS_SESSION_PATH = os.environ.get("CHANNELS_SESSION_PATH", "")
DIFY_CHAT_INPUT_ENABLED = os.environ.get("DIFY_CHAT_INPUT_ENABLED", "").lower() == "true"
FULL_ANALYSIS_ENABLED = os.environ.get("FULL_ANALYSIS_ENABLED", "").lower() == "true"
LLM_API_URL = os.environ.get("LLM_API_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# ===== 企微加解密 =====
def get_aes_key():
    key = base64.b64decode(WECOM_AES_KEY + "=")
    return key

def decrypt_msg(encrypt_text):
    """解密企微回调消息"""
    key = get_aes_key()
    data = base64.b64decode(encrypt_text)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    # Remove PKCS7 padding
    pad_len = padded[-1]
    content = padded[:-pad_len]
    # Remove 16-byte random prefix, 4-byte msg_len, msg, 16-byte corp_id
    msg_len = int.from_bytes(content[16:20], 'big')
    msg = content[20:20+msg_len].decode('utf-8')
    return msg

def encrypt_msg(reply_msg, nonce, timestamp):
    """加密回复消息"""
    key = get_aes_key()
    random_str = os.urandom(16)
    reply_bytes = reply_msg.encode('utf-8')
    msg_len = len(reply_bytes).to_bytes(4, 'big')
    corp_id = WECOM_TOKEN.encode('utf-8')  # simplified
    plain = random_str + msg_len + reply_bytes + corp_id
    # PKCS7 padding
    block_size = 32
    pad_len = block_size - (len(plain) % block_size)
    plain += bytes([pad_len]) * pad_len
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(plain) + encryptor.finalize()
    return base64.b64encode(encrypted).decode('utf-8')

def verify_signature(token, timestamp, nonce, encrypt):
    """验证企微签名"""
    sort_list = sorted([token, timestamp, nonce, encrypt])
    sha = hashlib.sha1("".join(sort_list).encode('utf-8')).hexdigest()
    return sha

# ===== 链接检测与登记（v2：短链展开/视频号/文本格式/持久化清单）=====
LINK_PATTERN = re.compile(r'https?://[^\s<>"\']+')
SHORT_HOSTS = ("v.douyin.com", "v.kuaishou.com", "xhslink.com")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
ALIASES = {"douyin":"douyin","dy":"douyin","抖音":"douyin",
           "channel":"channels","channels":"channels","wechat_channels":"channels","视频号":"channels",
           "kuaishou":"kuaishou","ks":"kuaishou","快手":"kuaishou",
           "xiaohongshu":"xiaohongshu","xhs":"xiaohongshu","小红书":"xiaohongshu"}
PLATFORM_NAMES = {"douyin":"抖音","channels":"视频号","kuaishou":"快手","xiaohongshu":"小红书"}

def expand_short(url):
    """短链展开：跟随 30x 重定向拿最终链接"""
    try:
        if "://" not in url:
            url = "https://" + url
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.url or ""
    except Exception as e:
        print(f"⚠️ 短链展开失败: {e}")
        return ""

def parse_target(text_in):
    """识别 (平台, 内容ID)；支持链接/短链/channel ID/中文平台名/纯ID"""
    text_in = str(text_in or "").strip()
    if not text_in:
        return None
    if any(h in text_in for h in SHORT_HOSTS):
        expanded = expand_short(text_in)
        if expanded and expanded != text_in:
            return parse_target(expanded)
    if "douyin.com" in text_in or "iesdouyin.com" in text_in:
        m = re.search(r'(?:video|note)/([0-9]+)', text_in)
        if m: return ("douyin", m.group(1))
    if "channels.weixin.qq.com" in text_in:
        m = re.search(r'(?:feed|post)/([a-zA-Z0-9_-]+)', text_in)
        if m: return ("channels", m.group(1))
    if "weixin.qq.com/sph" in text_in:
        # 视频号短链（助手后台分享出来的是这种），登记时再换成数字 object_id
        return ("channels", text_in if text_in.startswith("http") else "https://weixin.qq.com/sph/" + text_in.rstrip("/").split("/")[-1])
    if "kuaishou.com" in text_in:
        m = re.search(r'short-video/([a-zA-Z0-9_-]+)', text_in)
        if m: return ("kuaishou", m.group(1))
    if "xiaohongshu.com" in text_in:
        m = re.search(r'(?:explore|discovery/item)/([a-zA-Z0-9]+)', text_in)
        if m:
            tok = ""
            mt = re.search(r'[?&]xsec_token=([^&\s]+)', text_in)
            if mt: tok = mt.group(1)
            return ("xiaohongshu", m.group(1) + (("?xsec_token=" + tok) if tok else ""))
    parts = text_in.split()
    if len(parts) >= 2:
        plat = ALIASES.get(parts[0].lower(), "")
        if plat: return (plat, parts[1])
    clean = text_in
    if re.match(r'^[0-9]{15,}$', clean): return ("douyin", clean)
    if re.match(r'^[a-zA-Z0-9]{20,}$', clean): return ("xiaohongshu", clean)
    return None

def extract_platform_and_id(text):
    """从消息中提取平台和内容ID：优先取链接，其次按文本解析"""
    for link in LINK_PATTERN.findall(text):
        p = parse_target(link)
        if p: return p
    return parse_target(text.strip()) or (None, None)

def channels_resolve_id(text_in, token, metadata_out=None):
    """视频号：分享链接/export_id 换成数字 object_id（export 有时效，登记必须存数字ID）；失败返回原文"""
    s = str(text_in or "").strip()
    if re.match(r'^[0-9]{15,}$', s):
        return s  # 已是数字 ID
    if not token:
        return s
    try:
        share_url = s if s.startswith("http") else "https://channels.weixin.qq.com/web/pages/feed/" + s
        body = json.dumps({"share_url": share_url, "raw": False}).encode('utf-8')
        req = urllib.request.Request(
            "https://api.tikhub.io/api/v1/wechat_channels/v2/fetch_video_detail",
            data=body,
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json",
                     "User-Agent": "monitor/1.0"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode('utf-8'))
        def dig(o, depth=0):
            if depth > 7 or o is None: return None
            if isinstance(o, dict):
                for k in ("objectId", "object_id", "objectid", "id"):
                    v = o.get(k)
                    if isinstance(v, str) and re.match(r'^[0-9]{15,25}$', v):
                        return v
                for v in o.values():
                    r = dig(v, depth + 1)
                    if r: return r
            elif isinstance(o, list):
                for v in o[:8]:
                    r = dig(v, depth + 1)
                    if r: return r
            return None
        resolved = dig(d)
        if resolved:
            details = d.get("data") if isinstance(d, dict) else None
            if isinstance(metadata_out, dict) and isinstance(details, dict):
                description, created = details.get("description"), details.get("create_time")
                if str(created or "").isdigit() and 0 < int(created) <= time.time() + 300:
                    author = details.get('author') if isinstance(details.get('author'), dict) else {}
                    account = details.get('nickname') or details.get('nick_name') or details.get('author_name') or author.get('nickname') or author.get('nick_name') or ''
                    metadata_out.update({"id": resolved, "description": description if isinstance(description,str) else "", "create_time": int(created), "account_name": account if isinstance(account, str) else ''})
            print(f"🔗 视频号链接已解析: {s[:50]} -> {resolved}")
            return resolved
        return s
    except Exception as e:
        print(f"⚠️ 视频号ID解析失败: {e}")
        return s

# ===== 登记清单存储（GitHub 仓库优先，未配置时退回本地文件）=====
def github_api(method, path, body=None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/{path}"
    headers = {"Accept": "application/vnd.github+json",
               "Authorization": f"Bearer {GITHUB_TOKEN}",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "wecom-callback"}
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode('utf-8')
    return json.loads(raw) if raw.strip() else {}

def github_ensure_branch():
    """确保数据分支存在（不存在则基于 main 创建）"""
    try:
        github_api("GET", f"branches/{GITHUB_DATA_BRANCH}")
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    main = github_api("GET", "branches/main")
    github_api("POST", "git/refs", {"ref": f"refs/heads/{GITHUB_DATA_BRANCH}",
                                    "sha": main["commit"]["sha"]})

def github_load(with_sha=False):
    """读仓库清单；文件不存在返回 ([], None)"""
    try:
        r = github_api("GET", f"contents/{TARGETS_PATH}?ref={GITHUB_DATA_BRANCH}")
        content = base64.b64decode(r.get("content", "")).decode('utf-8')
        data = json.loads(content)
        data = data if isinstance(data, list) else []
        return (data, r.get("sha")) if with_sha else data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            github_ensure_branch()
            return ([], None) if with_sha else []
        raise

def github_load_json(path, default=None):
    """从 data 分支读取任意 JSON 文件；不存在时返回 default。"""
    try:
        r = github_api("GET", f"contents/{path}?ref={GITHUB_DATA_BRANCH}")
        return json.loads(base64.b64decode(r.get("content", "")).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return default
        raise

def github_save_json(path, data, message):
    """覆盖 data 分支中的 JSON 文件，并处理 GitHub Contents API 的 sha 要求。"""
    try:
        old = github_api("GET", f"contents/{path}?ref={GITHUB_DATA_BRANCH}")
        sha = old.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        sha = None
    body = {
        "message": message,
        "branch": GITHUB_DATA_BRANCH,
        "content": base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8"),
    }
    if sha:
        body["sha"] = sha
    github_api("PUT", f"contents/{path}", body)

def comment_cache_key(platform, content_id):
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", f"{platform}_{content_id}")

def comment_cache_path(platform, content_id):
    return f"{COMMENTS_PATH.rstrip('/')}/{comment_cache_key(platform, content_id)}.json"

def video_metadata_path(content_id):
    if not re.fullmatch(r"\d{15,25}", str(content_id or "")):
        raise ValueError("视频号作品 ID 无效")
    return f"data/video-metadata/channels_{content_id}.json"

def save_video_metadata(metadata):
    path = video_metadata_path(metadata.get("id"))
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip() or not str(metadata.get("create_time") or "").isdigit():
        raise ValueError("作品匹配信息不完整")
    if GITHUB_REPO and GITHUB_TOKEN:
        github_save_json(path, metadata, "save video metadata")
    else:
        local = os.path.join(os.path.dirname(TARGETS_FILE), path.replace("/", os.sep))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False)

def load_video_metadata(content_id):
    path = video_metadata_path(content_id)
    if GITHUB_REPO and GITHUB_TOKEN:
        return github_load_json(path, {}) or {}
    local = os.path.join(os.path.dirname(TARGETS_FILE), path.replace("/", os.sep))
    try:
        with open(local, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}

def load_comment_cache(platform, content_id):
    path = comment_cache_path(platform, content_id)
    if GITHUB_REPO and GITHUB_TOKEN:
        try:
            return github_load_json(path, {}) or {}
        except Exception as e:
            print(f"⚠️ GitHub 读评论缓存失败: {e}")
            return {}
    try:
        local = os.path.join(os.path.dirname(TARGETS_FILE), "comment-cache", os.path.basename(path))
        with open(local, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_comment_cache(platform, content_id, data, message):
    path = comment_cache_path(platform, content_id)
    if GITHUB_REPO and GITHUB_TOKEN:
        github_save_json(path, data, message)
        return
    local = os.path.join(os.path.dirname(TARGETS_FILE), "comment-cache", os.path.basename(path))
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_targets():
    if GITHUB_REPO and GITHUB_TOKEN:
        try:
            return github_load()
        except Exception as e:
            print(f"⚠️ GitHub 读清单失败: {e}")
            return []
    try:
        with open(TARGETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def register_target(platform, cid):
    """登记目标（按 platform+id 去重）；返回 (是否新增, 当前总数)"""
    if GITHUB_REPO and GITHUB_TOKEN:
        try:
            targets, sha = github_load(with_sha=True)
            if any(str(t.get("platform")) == str(platform) and str(t.get("id")) == str(cid) for t in targets):
                return False, len(targets)
            targets.append({"platform": platform, "id": str(cid), "added_at": int(time.time())})
            body = {"message": f"register: {platform} {cid}",
                    "branch": GITHUB_DATA_BRANCH,
                    "content": base64.b64encode(json.dumps(targets, ensure_ascii=False, indent=2).encode('utf-8')).decode('utf-8')}
            if sha:
                body["sha"] = sha
            github_api("PUT", f"contents/{TARGETS_PATH}", body)
            return True, len(targets)
        except Exception as e:
            print(f"❌ GitHub 登记写入失败: {e}")
            return False, 0
    targets = load_targets()
    if any(str(t.get("platform")) == str(platform) and str(t.get("id")) == str(cid) for t in targets):
        return False, len(targets)
    targets.append({"platform": platform, "id": str(cid), "added_at": int(time.time())})
    try:
        os.makedirs(os.path.dirname(TARGETS_FILE), exist_ok=True)
        with open(TARGETS_FILE, "w", encoding="utf-8") as f:
            json.dump(targets, f, ensure_ascii=False, indent=2)
        return True, len(targets)
    except Exception as e:
        print(f"❌ 登记写入失败: {e}")
        return False, len(targets)

# ===== 后台全量评论采集 =====
COMMENT_LIST_PATHS = [
    ("data", "aweme_list", "comments"), ("data", "comments"),
    ("data", "data", "comments"), ("data", "data", "comment_list"),
    ("data", "comment_list"), ("data", "comments_data"),
    ("data", "note_comments"), ("data", "comment_resp"),
    ("data", "data", "comment_resp"),
]
COLLECTION_LOCK = threading.Lock()
COLLECTING = set()

def tikhub_json(url, body=None):
    headers = {"Accept": "application/json", "Authorization": "Bearer " + TIKHUB_TOKEN,
               "User-Agent": "wecom-comment-collector/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=COLLECTION_REQUEST_TIMEOUT) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if not isinstance(result, dict) or result.get("code") != 200:
        raise RuntimeError("TikHub 接口返回错误，采集未完成")
    return result

def douyin_publication_metadata(content_id):
    query = urllib.parse.urlencode({'aweme_id': content_id})
    payload = tikhub_json('https://api.tikhub.io/api/v1/douyin/web/fetch_one_video?' + query)
    detail = (payload.get('data') or {}).get('aweme_detail') or {}
    if str(detail.get('aweme_id') or '') != str(content_id):
        raise ValueError('抖音作品信息不匹配')
    created = int(detail.get('create_time') or 0)
    if created <= 0 or created > time.time() + 300:
        raise ValueError('抖音发布时间无效')
    author = detail.get('author') if isinstance(detail.get('author'), dict) else {}
    return {'id': str(content_id), 'create_time': created,
            'description': str(detail.get('desc') or ''), 'account_name': str(author.get('nickname') or '')}

def nested_value(value, path):
    cur = value
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur

def comment_rows(payload):
    for path in COMMENT_LIST_PATHS:
        rows = nested_value(payload, path)
        if isinstance(rows, list):
            return rows
    return []

def first_value(row, *keys):
    for key in keys:
        if isinstance(row, dict) and row.get(key) is not None:
            return row[key]
    return None

def safe_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

def normalise_comment(platform, row):
    content = str(first_value(row, "content", "text", "comment", "content_desc") or "").strip()
    unavailable = not content
    if not content:
        if platform != 'channels' or not first_value(row, 'id', 'comment_id', 'cid'):
            return None
        content = '[评论内容无法获取]'
    author = first_value(row, "author", "nickname", "user_name", "nick_name")
    if not author:
        for path in (("user", "nickname"), ("user_info", "nickname"), ("author", "nickname")):
            author = nested_value(row, path)
            if author:
                break
    return {
        "comment_id": str(first_value(row, "id", "comment_id", "cid") or "")[:100],
        "author": str(author or "匿名")[:50],
        "content": content[:1500],
        "likes": safe_int(first_value(row, "likes", "like_count", "digg_count", "likemount")),
        "replies": safe_int(first_value(row, "reply_count", "sub_comment_count")),
        "created_at": first_value(row, "create_time", "created_at", "createTime", "timestamp") or 0,
        "platform": platform,
        "content_unavailable": unavailable,
    }

def fetch_all_comments(platform, content_id, checkpoint=None):
    """在 Railway 后台抓取所有可用分页；不受 Dify Sandbox 的 15 秒限制。"""
    checkpoint = checkpoint or {}
    comments = [comment for comment in (checkpoint.get("comments") or [])
                if not comment.get("parent_comment_id")]
    seen = {c.get("comment_id") or (c.get("author", "") + "\n" + c.get("content", "")) for c in comments}
    pages = int(checkpoint.get("pages") or 0)
    cursor, last_buffer = checkpoint.get("cursor", 0), checkpoint.get("last_buffer")
    if pages >= COLLECTION_PAGE_LIMIT:
        raise RuntimeError("已达到分页上限，无法继续采集")
    while pages < COLLECTION_PAGE_LIMIT:
        if platform == "douyin":
            query = urllib.parse.urlencode({"aweme_id": content_id, "count": 20, "cursor": cursor})
            payload = tikhub_json("https://api.tikhub.io/api/v1/douyin/web/fetch_video_comments?" + query)
            if not isinstance(payload.get("data"), dict) or "has_more" not in payload["data"]:
                raise RuntimeError("抖音评论响应缺少分页状态，采集未完成")
            more = bool((payload.get("data") or {}).get("has_more"))
            next_cursor = (payload.get("data") or {}).get("cursor")
        elif platform == "channels":
            body = {"object_id": content_id, "raw": False}
            if last_buffer:
                body["last_buffer"] = last_buffer
            payload = tikhub_json("https://api.tikhub.io/api/v1/wechat_channels/v2/fetch_video_comments", body)
            data = payload.get("data") or {}
            if not isinstance(data, dict) or "down_continue" not in data or not isinstance(data.get("comments"), list):
                raise RuntimeError("视频号评论响应缺少评论列表或分页状态，采集未完成")
            more = data.get("down_continue") == 1
            next_buffer = data.get("last_buffer")
        elif platform == "xiaohongshu":
            note_id = str(content_id).split("?", 1)[0]
            query = {"note_id": note_id, "sort_strategy": "latest_v2"}
            if cursor:
                query["cursor"] = cursor
            payload = tikhub_json("https://api.tikhub.io/api/v1/xiaohongshu/app_v2/get_note_comments?" + urllib.parse.urlencode(query))
            data = ((payload.get("data") or {}).get("data")) or {}
            more = bool(data.get("has_more"))
            next_cursor = data.get("cursor")
        elif platform == "kuaishou":
            query = urllib.parse.urlencode({"photo_id": content_id})
            payload = tikhub_json("https://api.tikhub.io/api/v1/kuaishou/app/fetch_video_comment?" + query)
            more = False
        else:
            raise ValueError("不支持的平台: " + str(platform))

        rows = comment_rows(payload)
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = normalise_comment(platform, row)
            if not item:
                continue
            identity = item["comment_id"] or (item["author"] + "\n" + item["content"])
            if identity in seen:
                continue
            seen.add(identity)
            comments.append(item)
        pages += 1
        if not more:
            break
        if not rows:
            raise RuntimeError("分页接口表示还有数据，但本页为空；采集未完成")
        if platform == "channels":
            if not next_buffer or next_buffer == last_buffer:
                raise RuntimeError("视频号分页游标缺失或未变化；采集未完成")
            last_buffer = next_buffer
        elif platform in ("douyin", "xiaohongshu"):
            if next_cursor is None or next_cursor == cursor:
                raise RuntimeError("分页游标缺失或未变化；采集未完成")
            cursor = next_cursor
        else:
            break
        save_comment_cache(platform, content_id, {
            "status": "collecting", "platform": platform, "content_id": content_id,
            "comments": comments, "pages": pages, "cursor": cursor, "last_buffer": last_buffer,
            "updated_at": int(time.time()),
        }, f"collect: checkpoint {platform} {content_id}")
    if pages >= COLLECTION_PAGE_LIMIT and more:
        raise RuntimeError("达到分页上限，但接口仍有下一页；采集未完成")
    return comments, pages, False


def fetch_all_replies(platform, content_id, primary_comments):
    """Fetch every parent's replies, preserving IDs for later verification."""
    if platform not in ('channels', 'douyin'):
        raise ValueError('该平台暂不支持楼中楼采集')
    replies, seen, page_count = [], set(), 0
    for parent in primary_comments:
        parent_id = str(parent.get('comment_id') or '').strip()
        if not parent_id:
            raise RuntimeError('一级评论缺少 ID，无法完整采集楼中楼')
        cursor, last_buffer = 0, None
        for page in range(COLLECTION_PAGE_LIMIT):
            if platform == 'channels':
                body = {'object_id': str(content_id), 'comment_id': parent_id, 'raw': False}
                if last_buffer: body['last_buffer'] = last_buffer
                payload = tikhub_json('https://api.tikhub.io/api/v1/wechat_channels/v2/fetch_video_comments', body)
                data = payload.get('data') or {}
                if not isinstance(data, dict) or 'down_continue' not in data or not isinstance(data.get('comments'), list):
                    raise RuntimeError('视频号楼中楼响应缺少分页状态或评论')
                rows, more, next_cursor = data['comments'], bool(data['down_continue']), data.get('last_buffer')
                prior_cursor = last_buffer
            else:
                query = urllib.parse.urlencode({'item_id': str(content_id), 'comment_id': parent_id, 'cursor': cursor, 'count': 20})
                payload = tikhub_json('https://api.tikhub.io/api/v1/douyin/web/fetch_video_comment_replies?' + query)
                data = payload.get('data') or {}
                if not isinstance(data, dict) or 'has_more' not in data or not isinstance(data.get('comments'), list):
                    raise RuntimeError('抖音楼中楼响应缺少分页状态或评论')
                rows, more, next_cursor = data['comments'], bool(data['has_more']), data.get('cursor')
                prior_cursor = cursor
            page_count += 1
            for row in rows:
                if not isinstance(row,dict): continue
                item = normalise_comment(platform, row)
                if not item or not item.get('comment_id'):
                    raise RuntimeError('楼中楼缺少评论内容或 ID，无法完整采集')
                key = (parent_id, item['comment_id'])
                if key in seen: continue
                seen.add(key); item['parent_comment_id'] = parent_id; replies.append(item)
            if not more: break
            if not rows or next_cursor is None or next_cursor == '' or next_cursor == prior_cursor:
                raise RuntimeError('楼中楼分页异常，采集未完成')
            if platform == 'channels': last_buffer = next_cursor
            else: cursor = next_cursor
        else:
            raise RuntimeError('楼中楼达到分页上限，采集未完成')
    return replies, page_count

def collect_target(platform, content_id, chat_id="", on_complete=None, first_run=False):
    key = (str(platform), str(content_id))
    with COLLECTION_LOCK:
        if key in COLLECTING:
            return False
        COLLECTING.add(key)

    def run():
        started = int(time.time())
        success = False
        try:
            previous = load_comment_cache(platform, content_id)
            checkpoint = previous if previous.get("status") in ("collecting", "failed") else {}
            comments, pages, capped = fetch_all_comments(platform, content_id, checkpoint)
            primary_count = len(comments)
            reply_pages = 0
            if platform in ('channels', 'douyin'):
                replies, reply_pages = fetch_all_replies(platform, content_id, comments)
                comments.extend(replies)
            result = {
                "status": "complete", "platform": platform, "content_id": content_id,
                "started_at": started, "completed_at": int(time.time()), "pages": pages,
                "page_limit_reached": capped, "comments": comments,
                "primary_count": primary_count, "reply_count": len(comments) - primary_count,
                "reply_pages": reply_pages,
            }
            if FULL_ANALYSIS_ENABLED:
                numbered = [{**comment, 'number': i + 1} for i, comment in enumerate(comments)]
                result['analysis'] = DeepSeekBatch(LLM_API_URL, LLM_API_KEY, LLM_MODEL).analyze(numbered)
            if platform in ('channels', 'douyin'):
                try:
                    metadata = load_video_metadata(content_id) if platform == 'channels' else douyin_publication_metadata(content_id)
                    if str(metadata.get('id') or '') == str(content_id):
                        result['video'] = {'description': str(metadata.get('description') or ''),
                                           'create_time': metadata.get('create_time'),
                                           'account_name': str(metadata.get('account_name') or '')}
                except (OSError, ValueError, RuntimeError, TypeError, urllib.error.URLError) as error:
                    print(f"读取作品信息失败: {type(error).__name__}")
            save_comment_cache(platform, content_id, result, f"collect: complete {platform} {content_id}")
            print(f"✅ 全量评论采集完成: {platform} {content_id}, {len(comments)} 条, {pages} 页")
            success = trigger_dify(f"{platform} {content_id}", "collected_first" if first_run else "collected", chat_id)
        except Exception as e:
            print(f"❌ 全量评论采集失败: {e}")
            try:
                saved = load_comment_cache(platform, content_id)
                saved.update({"status": "failed", "platform": platform, "content_id": content_id,
                              "started_at": started, "error": str(e)[:300]})
                save_comment_cache(platform, content_id, saved, f"collect: failed {platform} {content_id}")
            except Exception as save_error:
                print(f"❌ 保存采集失败状态失败: {save_error}")
        finally:
            with COLLECTION_LOCK:
                COLLECTING.discard(key)
            if on_complete:
                try:
                    on_complete(success)
                except Exception as error:
                    print(f"更新监控调度失败: {type(error).__name__}")

    threading.Thread(target=run, daemon=True).start()
    return True

# ===== 触发 Dify =====
def trigger_dify(content, mode="register", chat_id=""):
    """调 Dify API 触发监控工作流；mode=register 表示登记触发（强制推送评论现状）"""
    if not DIFY_API_URL or not DIFY_API_KEY:
        print("⚠️ Dify API 未配置")
        return False
    inputs = {"content": content, "mode": mode}
    if chat_id and DIFY_CHAT_INPUT_ENABLED:
        inputs["chat_id"] = chat_id
    body = json.dumps({
        "inputs": inputs,
        "response_mode": "blocking",
        "user": "wecom-bot"
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        DIFY_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DIFY_API_KEY}",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            status = (result.get('data') or {}).get('status')
            if status != 'succeeded':
                print(f"❌ Dify 运行未成功: {status}")
                return False
            print("✅ Dify 工作流运行成功")
            return True
    except Exception as e:
        print(f"❌ Dify 触发失败: {e}")
        return False

def run_monitor_tick():
    if not APPROVAL_DB_PATH:
        return
    store = ScheduleStore(APPROVAL_DB_PATH)
    for item in store.claim_due():
        platform, cid, chat = item['platform'], item['content_id'], item['chat_id']
        def completed(success, platform=platform, cid=cid):
            ScheduleStore(APPROVAL_DB_PATH).complete(platform, cid, success)
        if not collect_target(platform, cid, chat, on_complete=completed, first_run=item['last_success'] is None):
            store.complete(platform, cid, False)

def monitor_loop():
    while True:
        try:
            run_monitor_tick()
        except Exception as error:
            print(f"监控调度失败: {type(error).__name__}")
        time.sleep(60)

# ===== 回复群消息 =====
def reply_to_group(message):
    """通过 Webhook 回复群消息"""
    if not WECOM_BOT_WEBHOOK:
        return
    body = json.dumps({
        "msgtype": "text",
        "text": {"content": message}
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        WECOM_BOT_WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"✅ 群回复成功")
    except Exception as e:
        print(f"❌ 群回复失败: {e}")

# ===== 路由 =====
@app.route('/healthz', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "wecom-callback", "targets": len(load_targets())})

@app.route('/targets', methods=['GET'])
def targets_api():
    """登记清单接口，供 Dify 定时任务拉取；设置 TARGETS_API_TOKEN 后需 ?token=xxx"""
    if TARGETS_API_TOKEN and request.args.get('token') != TARGETS_API_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(load_targets())


def approval_messages(comments):
    """Render all candidate text without exceeding the robot message size."""
    lines = []
    for index, comment in enumerate(comments, 1):
        author = str(comment.get("author") or "匿名").replace("@", "＠").replace("\n", " ").replace("\r", " ")
        content = comment["content"].replace("@", "＠").replace("\n", " ").replace("\r", " ")
        reason = str(comment.get("reason") or "").replace("@", "＠").replace("\n", " ").replace("\r", " ")
        label = "[楼中楼] " if comment.get('parent_comment_id') else ""
        lines.append(f"{index}. {label}{author}：{content}" + (f"（{reason}）" if reason else ""))
    text = "\n".join(lines)
    chunks = []
    chunk = ""
    for char in text:
        if len((chunk + char).encode("utf-8")) > 2600:
            chunks.append(chunk)
            chunk = ""
        chunk += char
    if chunk:
        chunks.append(chunk)
    return [f"待确认删除评论（{index}/{len(chunks)}）\n{chunk}"
            for index, chunk in enumerate(chunks, 1)]


def send_approval_message(content):
    body = json.dumps({"msgtype": "text", "text": {"content": content}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WECOM_BOT_WEBHOOK, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") != 0:
        raise RuntimeError("群消息发送失败")


def announce_approval_batch(store, batch_id, comments):
    try:
        for message in approval_messages(comments):
            send_approval_message(message)
        if not store.mark_announced(batch_id):
            raise RuntimeError("确认批次状态错误")
        send_approval_message(f"以上评论已列出。如确认删除，请 @机器人 发送：确认删除 {batch_id}")
    except Exception as error:
        store.fail_announcement(batch_id)
        print(f"待确认清单发送失败: {type(error).__name__}")

def run_approved_deletion(batch_id):
    """Run the already-confirmed batch after a session and target check."""
    store = ApprovalStore(APPROVAL_DB_PATH)
    session = {}
    def session_check():
        if not CHANNELS_SESSION_PATH:
            return "expired"
        try:
            with open(CHANNELS_SESSION_PATH, "r", encoding="utf-8") as handle:
                session.update(json.load(handle))
        except (OSError, ValueError):
            return "expired"
        return check_session(session)
    def request_login(chat_id, target_batch):
        # The cloud QR login provider is a separate, currently unconfigured component.
        send_approval_message("视频号登录已失效；云端扫码授权尚未接通，本批评论尚未删除。")
    def delete_comments(comments):
        first = comments[0]
        if any(item.get("platform") != "channels" or item.get("object_id") != first.get("object_id") for item in comments):
            raise ValueError("此批次不是单一视频号作品")
        metadata = load_video_metadata(first.get("object_id"))
        if str(metadata.get("id") or "") != str(first.get("object_id") or ""):
            raise ValueError("缺少已验证的作品信息，请重新登记原始分享链接")
        return delete_verified_batch(session, metadata, comments)
    outcome = execute_confirmed_batch(store, batch_id, session_check, request_login, delete_comments)
    record = store.get(batch_id)
    if outcome == "finished":
        result = record.get("result") or {}
        if result.get("status") == "completed":
            send_approval_message("删除执行完成：已删除 " + str(len(result.get("deleted") or [])) + " 条。")
        else:
            send_approval_message("删除结果未完全确认，请核对后台；后续评论未继续删除。")
    elif outcome == "deletion_unknown":
        send_approval_message("作品校验或删除执行未完成，请核对后台；系统不会自动重试该批次。")
    elif outcome == "session_unknown":
        send_approval_message("无法确认视频号登录状态，本批评论尚未删除。")
    elif outcome == "login_request_failed":
        print("视频号登录通知发送失败")

def worker_authorized():
    token = request.headers.get('X-Worker-Token', '')
    return bool(LOCAL_WORKER_ENABLED and LOCAL_WORKER_TOKEN and APPROVAL_DB_PATH and token and
                hmac.compare_digest(token.encode('utf-8'), LOCAL_WORKER_TOKEN.encode('utf-8')))

@app.route('/local-worker/jobs', methods=['GET'])
def local_worker_jobs():
    if not worker_authorized():
        return jsonify({'error':'unauthorized'}), 403
    return jsonify({'batches': ApprovalStore(APPROVAL_DB_PATH).confirmed_batches()})

@app.route('/local-worker/jobs/<batch_id>/claim', methods=['POST'])
def local_worker_claim(batch_id):
    if not worker_authorized():
        return jsonify({'error':'unauthorized'}), 403
    if not re.fullmatch(r'[0-9a-f]{12}',batch_id):
        return jsonify({'error':'invalid batch'}), 400
    store=ApprovalStore(APPROVAL_DB_PATH)
    batch=store.get(batch_id)
    if not batch or batch['state']!='confirmed':
        return jsonify({'error':'not confirmed'}),409
    comments=batch['payload']
    if not comments or any(c.get('platform')!='channels' or c.get('object_id')!=comments[0].get('object_id') for c in comments):
        return jsonify({'error':'unsupported batch'}),400
    video=load_video_metadata(comments[0]['object_id'])
    if str(video.get('id') or '')!=str(comments[0]['object_id']):
        return jsonify({'error':'video metadata unavailable'}),409
    if not store.claim(batch_id):
        return jsonify({'error':'already claimed'}),409
    return jsonify({'batch_id':batch_id,'comments':comments,'video':video})

@app.route('/local-worker/jobs/<batch_id>/login-needed', methods=['POST'])
def local_worker_login_needed(batch_id):
    if not worker_authorized():
        return jsonify({'error':'unauthorized'}),403
    if not re.fullmatch(r'[0-9a-f]{12}',batch_id):
        return jsonify({'error':'invalid batch'}),400
    store=ApprovalStore(APPROVAL_DB_PATH)
    if store.mark_login_notified(batch_id):
        reply_to_group('本机视频号助手不可用，请打开 Ego Lite 并登录视频号助手；本批评论尚未删除。')
    return jsonify({'ok':True})

@app.route('/local-worker/jobs/<batch_id>/finish', methods=['POST'])
def local_worker_finish(batch_id):
    if not worker_authorized():
        return jsonify({'error':'unauthorized'}),403
    body=request.get_json(silent=True) or {}
    result=body.get('result')
    if not isinstance(result,dict) or result.get('status') not in ('completed','unknown','rejected','not_started'):
        return jsonify({'error':'invalid result'}),400
    store=ApprovalStore(APPROVAL_DB_PATH)
    if result['status']=='not_started':
        changed=store.release_before_deletion(batch_id)
    else:
        changed=store.finish(batch_id,result)
    if not changed:
        return jsonify({'error':'invalid batch state'}),409
    if result['status']=='completed':
        reply_to_group('已完成确认删除：成功 '+str(len(result.get('deleted') or []))+' 条。')
    elif result['status']=='unknown':
        reply_to_group('本机删除结果未完全确认，请核对后台；未确认结果不会自动重试。')
    elif result['status']=='rejected':
        reply_to_group('本机未能核对作品或评论信息，本批评论未删除，请检查视频号后台。')
    return jsonify({'ok':True})


@app.route('/approval-batches', methods=['POST'])
def create_approval_batch():
    """Create a batch only for an authenticated workflow with persistent storage configured."""
    if not APPROVAL_DB_PATH or not APPROVAL_API_TOKEN or not APPROVAL_CHAT_ID or not WECOM_BOT_WEBHOOK:
        return jsonify({"error": "approval service unavailable"}), 503
    token = request.headers.get("X-Approval-Token", "")
    if not hmac.compare_digest(token.encode("utf-8"), APPROVAL_API_TOKEN.encode("utf-8")):
        return jsonify({"error": "unauthorized"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "invalid request"}), 400
    chat_id = body.get("chat_id")
    comments = body.get("comments")
    if not isinstance(chat_id, str) or not chat_id.strip() or not isinstance(comments, list) or not comments:
        return jsonify({"error": "chat_id and comments required"}), 400
    if not hmac.compare_digest(chat_id.encode("utf-8"), APPROVAL_CHAT_ID.encode("utf-8")):
        return jsonify({"error": "chat not configured"}), 403
    if len(comments) > 1000:
        return jsonify({"error": "too many comments"}), 400
    for comment in comments:
        if not isinstance(comment, dict) or not all(
            isinstance(comment.get(key), str) and comment[key].strip()
            for key in ("platform", "object_id", "comment_id", "content")
        ):
            return jsonify({"error": "invalid comment"}), 400
    if any(c["platform"] != "channels" or c["object_id"] != comments[0]["object_id"] for c in comments):
        return jsonify({"error": "only one channels video per batch is supported"}), 400
    try:
        store = ApprovalStore(APPROVAL_DB_PATH)
        batch_id = store.create(chat_id, comments, announcing=True)
    except (OSError, ValueError, sqlite3.Error):
        return jsonify({"error": "unable to persist batch"}), 503
    threading.Thread(target=announce_approval_batch, args=(store, batch_id, comments), daemon=True).start()
    return jsonify({"batch_id": batch_id, "count": len(comments), "status": "announcing"}), 202

def api_authorized():
    return not TARGETS_API_TOKEN or request.args.get('token') == TARGETS_API_TOKEN

@app.route('/comments', methods=['GET'])
def comments_api():
    """返回某个作品已完成的评论缓存，供 Dify 的 collected 模式读取。"""
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 403
    platform = str(request.args.get('platform', '')).strip()
    content_id = str(request.args.get('id', '')).strip()
    if not platform or not content_id:
        return jsonify({"error": "platform and id are required"}), 400
    return jsonify(load_comment_cache(platform, content_id))

@app.route('/collect', methods=['POST'])
def collect_api():
    """异步刷新一个作品的完整评论；请求立即返回，采集完成后再触发 Dify。"""
    if not api_authorized():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    platform = str(data.get('platform', '')).strip()
    content_id = str(data.get('id') or data.get('content_id') or '').strip()
    if not platform or not content_id:
        return jsonify({"error": "platform and id are required"}), 400
    started = collect_target(platform, content_id)
    return jsonify({"accepted": True, "started": started, "platform": platform, "id": content_id})

CACHE_PATH = os.environ.get("CACHE_PATH", "data/comment_cache.json")

@app.route('/comment_cache', methods=['GET'])
def comment_cache_get():
    """分批采集缓存读接口：{ "platform|id": {pages_done, cursor, last_buffer, comments, complete, ...} }"""
    if TARGETS_API_TOKEN and request.args.get('token') != TARGETS_API_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    if not (GITHUB_REPO and GITHUB_TOKEN):
        return jsonify({})
    try:
        r = github_api("GET", f"contents/{CACHE_PATH}?ref={GITHUB_DATA_BRANCH}")
        content = base64.b64decode(r.get("content", "")).decode('utf-8')
        return jsonify(json.loads(content) if content.strip() else {})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return jsonify({})
        return jsonify({"error": str(e)[:120]}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 500

def cache_write_authorized():
    """写缓存口令：请求头 X-Cache-Token 或 ?token=，匹配 TARGETS_API_TOKEN 或 TIKHUB_API_TOKEN 任一即可；两者都未配置时不校验"""
    sent = request.headers.get("X-Cache-Token") or request.args.get("token") or ""
    allowed = [t for t in (TARGETS_API_TOKEN, TIKHUB_TOKEN) if t]
    return (not allowed) or (sent in allowed)

@app.route('/comment_cache', methods=['PUT', 'POST'])
def comment_cache_put():
    """分批采集缓存写接口：body 为完整缓存 JSON"""
    if not cache_write_authorized():
        return jsonify({"error": "unauthorized"}), 403
    if not (GITHUB_REPO and GITHUB_TOKEN):
        return jsonify({"ok": False, "error": "github not configured"}), 500
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400
    try:
        sha = None
        try:
            cur = github_api("GET", f"contents/{CACHE_PATH}?ref={GITHUB_DATA_BRANCH}")
            sha = cur.get("sha")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                github_ensure_branch()
        put_body = {"message": "update comment cache", "branch": GITHUB_DATA_BRANCH,
                    "content": base64.b64encode(json.dumps(body, ensure_ascii=False).encode('utf-8')).decode('utf-8')}
        if sha:
            put_body["sha"] = sha
        github_api("PUT", f"contents/{CACHE_PATH}", put_body)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]}), 500

@app.route('/wecom', methods=['GET'])
def wecom_verify():
    """企微回调 URL 验证"""
    signature = request.args.get('msg_signature', '')
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')
    echostr = request.args.get('echostr', '')
    
    # 验证签名
    calc_sig = verify_signature(WECOM_TOKEN, timestamp, nonce, echostr)
    if calc_sig != signature:
        return "signature error", 403
    
    # 解密 echostr 并返回
    decrypted = decrypt_msg(echostr)
    return decrypted

@app.route('/wecom', methods=['POST'])
def wecom_callback():
    """接收企微推送的消息（XML格式）"""
    try:
        signature = request.args.get('msg_signature', '')
        timestamp = request.args.get('timestamp', '')
        nonce = request.args.get('nonce', '')
        
        # 解析 XML 格式的请求体
        raw_body = request.get_data()
        print(f"📥 原始请求体: {raw_body[:500]}")
        
        encrypt = ""
        # Try JSON first (WeCom smart bot sends JSON with lowercase "encrypt")
        try:
            data = json.loads(raw_body)
            if isinstance(data, dict):
                encrypt = str(data.get('encrypt') or data.get('Encrypt') or '').strip()
                print("JSON FORMAT OK, encrypt=" + encrypt[:50] + "...")
        except:
            pass
        # Try XML as fallback
        if not encrypt:
            try:
                root = ET.fromstring(raw_body)
                encrypt_elem = root.find('Encrypt')
                if encrypt_elem is not None and encrypt_elem.text:
                    encrypt = encrypt_elem.text.strip()
                    print("XML FORMAT OK, encrypt=" + encrypt[:50] + "...")
            except:
                pass
        
        print(f"🔐 Encrypt字段: {encrypt[:100]}..." if encrypt else "❌ 未找到Encrypt字段")
        
        if not encrypt:
            return "ok"
        
        # 验证签名
        calc_sig = verify_signature(WECOM_TOKEN, timestamp, nonce, encrypt)
        if calc_sig != signature:
            print(f"❌ 签名验证失败: 期望={signature}, 计算={calc_sig}")
            print(f"   Token={WECOM_TOKEN[:10]}..., Timestamp={timestamp}, Nonce={nonce}")
            return "ok"
        
        # 解密消息
        try:
            msg_json = decrypt_msg(encrypt)
        except Exception as dec_err:
            print(f"❌ 解密失败: {dec_err}")
            print(f"   AES Key前10字符: {WECOM_AES_KEY[:10]}...")
            return "ok"
        print(f"🔓 解密后: {msg_json[:500]}")
        
        # 解析解密后的消息（也是XML格式）
        msg = {}
        try:
            msg_root = ET.fromstring(msg_json)
            for child in msg_root:
                msg[child.tag] = (child.text or '').strip()
        except:
            try:
                msg = json.loads(msg_json)
            except:
                pass
        
        print(f"📩 解析后消息: {msg}")
        
        # 检查是否包含链接
        content = msg.get('Content', '') or msg.get('text', {}).get('content', '')
        if not content:
            return "ok"

        confirmation = re.search(r'(?:^|\s)确认删除\s+([0-9a-f]{12})(?=\s|@|$)', content.strip())
        if confirmation:
            sender = msg.get("from") or {}
            user_id = sender.get("userid", "") if isinstance(sender, dict) else ""
            user_id = user_id or msg.get("FromUserName", "")
            chat_id = msg.get("chatid") or msg.get("ChatId") or ""
            chat_type = str(msg.get("chattype") or msg.get("ChatType") or "").lower()
            if APPROVAL_DB_PATH and chat_type == "group" and chat_id and user_id:
                try:
                    confirmed = ApprovalStore(APPROVAL_DB_PATH).confirm(
                        confirmation.group(1), chat_id, user_id
                    )
                    if confirmed:
                        if LOCAL_WORKER_ENABLED:
                            reply_to_group("已收到删除确认，等待本机执行程序检查视频号登录。")
                        elif APPROVAL_EXECUTION_ENABLED:
                            reply_to_group("已收到删除确认，正在核验视频号登录和评论。")
                            threading.Thread(target=run_approved_deletion,
                                             args=(confirmation.group(1),), daemon=True).start()
                        else:
                            reply_to_group("已收到确认，但删除服务尚未启用，本批评论尚未删除。")
                except (OSError, ValueError, sqlite3.Error) as error:
                    print(f"确认批次失败: {type(error).__name__}")
            return "ok"
        
        chat_type = str(msg.get("chattype") or msg.get("ChatType") or "").lower()
        chat_id = (msg.get("chatid") or msg.get("ChatId") or "") if chat_type == "group" else ""
        message_id = str(msg.get('msgid') or msg.get('MsgId') or '').strip()
        if message_id and APPROVAL_DB_PATH:
            if not ApprovalStore(APPROVAL_DB_PATH).claim_webhook_message(message_id):
                return 'ok'
        threading.Thread(target=handle_registration, args=(content, chat_id), daemon=True).start()
        
        return "ok"
    except Exception as e:
        print(f"❌ 回调处理错误: {e}")
        return "ok"

def handle_registration(content, chat_id):
    try:
        platform, content_id = extract_platform_and_id(content)
        if not platform or not content_id:
            return
        metadata = {}
        if platform == 'channels':
            original = content_id
            for attempt in range(3):
                content_id = channels_resolve_id(original, TIKHUB_TOKEN, metadata)
                if metadata or re.fullmatch(r'\d{15,25}', str(original)) or not TIKHUB_TOKEN:
                    break
                if attempt < 2:
                    time.sleep((3,8)[attempt])
            if metadata:
                try:
                    save_video_metadata(metadata)
                except (OSError, ValueError, urllib.error.URLError) as error:
                    print(f"保存作品匹配信息失败: {type(error).__name__}")
            if not metadata and re.fullmatch(r'\d{15,25}', str(content_id)):
                metadata = load_video_metadata(content_id)
        elif platform == 'douyin':
            try:
                metadata = douyin_publication_metadata(content_id)
            except (OSError, ValueError, RuntimeError, TypeError):
                metadata = {}
        if not chat_id or not APPROVAL_DB_PATH:
            reply_to_group('无法保存群聊监控计划，请检查服务配置。')
            return
        if not metadata.get('create_time'):
            reply_to_group('暂时无法读取作品信息，可能是链接无效或平台数据尚未就绪；未加入监控，请稍后重新发送分享链接。')
            return
        added, total = register_target(platform, content_id)
        if total == 0:
            reply_to_group('登记失败，请稍后重试。')
            return
        try:
            ScheduleStore(APPROVAL_DB_PATH).register(platform, content_id, metadata['create_time'], chat_id)
        except (OSError, ValueError, sqlite3.Error):
            reply_to_group('作品已登记，但定时计划保存失败，请稍后重新发送链接。')
            return
        name = PLATFORM_NAMES.get(platform, platform)
        if time.time() - int(metadata['create_time']) < 6*3600:
            reply_to_group(f'✅ {name}作品已加入评论监控，发布满 6 小时后开始采集。')
        else:
            reply_to_group(f'✅ {name}作品已加入评论监控，正在安排采集评论。')
        run_monitor_tick()
    except Exception as error:
        print(f'登记处理失败: {type(error).__name__}')
        reply_to_group('登记处理失败，请稍后重新发送链接。')

if __name__ == '__main__':
    print(f"=== 版本: v2.1 (带调试日志) ===")
    print(f"Token前10字符: {WECOM_TOKEN[:10]}...")
    print(f"AES Key前10字符: {WECOM_AES_KEY[:10]}...")
    print(f"Webhook前30字符: {WECOM_BOT_WEBHOOK[:30]}...")
    print(f"Dify URL: {DIFY_API_URL}")
    port = int(os.environ.get('PORT', 8080))
    print(f"=== 企微回调服务 ===")
    print(f"端口: {port}")
    print(f"Webhook: {'已配置' if WECOM_BOT_WEBHOOK else '未配置'}")
    print(f"Dify API: {'已配置' if DIFY_API_URL else '未配置'}")
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=port, debug=False)
