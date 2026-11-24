#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微机器人 URL 回调服务：接收群消息，检测链接，触发 Dify 监控"""
import os
import json
import hashlib
import base64
import re
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

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

def channels_resolve_id(text_in, token):
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

# ===== 触发 Dify =====
def trigger_dify(content, mode="register"):
    """调 Dify API 触发监控工作流；mode=register 表示登记触发（强制推送评论现状）"""
    if not DIFY_API_URL or not DIFY_API_KEY:
        print("⚠️ Dify API 未配置")
        return False
    body = json.dumps({
        "inputs": {"content": content, "mode": mode},
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
            result = resp.read().decode('utf-8')
            print(f"✅ Dify 触发成功: {result[:200]}")
            return True
    except Exception as e:
        print(f"❌ Dify 触发失败: {e}")
        return False

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
        
        platform, content_id = extract_platform_and_id(content)

        if platform and content_id:
            if platform == "channels":
                content_id = channels_resolve_id(content_id, TIKHUB_TOKEN)
            added, total = register_target(platform, content_id)
            plat_name = PLATFORM_NAMES.get(platform, platform)

            def run():
                if added:
                    reply_to_group(f"✅ 已登记{plat_name}内容，当前共 {total} 个监控目标")
                else:
                    reply_to_group(f"ℹ️ 该{plat_name}内容已在监控清单中，无需重复登记")
                # 触发一次 Dify（register 模式）：满6小时的作品立即回报评论现状并处理负面；不足6小时回报剩余等待时间
                trigger_dify(f"{platform} {content_id}", "register")

            threading.Thread(target=run, daemon=True).start()
        else:
            # 不是链接，忽略（或者以后可以加其他指令）
            pass
        
        return "ok"
    except Exception as e:
        print(f"❌ 回调处理错误: {e}")
        return "ok"

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
    app.run(host='0.0.0.0', port=port, debug=False)
