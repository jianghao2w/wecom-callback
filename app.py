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

# ===== 链接检测 =====
LINK_PATTERN = re.compile(r'https?://[^\s<>"\']+')

def extract_platform_and_id(text):
    """从消息中提取平台和内容ID"""
    links = LINK_PATTERN.findall(text)
    if not links:
        return None, None
    link = links[0]
    if 'douyin.com' in link:
        m = re.search(r'(?:video|note)/([0-9]+)', link)
        if m: return 'douyin', m.group(1)
    elif 'kuaishou.com' in link:
        m = re.search(r'short-video/([a-zA-Z0-9_-]+)', link)
        if m: return 'kuaishou', m.group(1)
    elif 'xiaohongshu.com' in link:
        m = re.search(r'(?:explore|discovery/item)/([a-zA-Z0-9]+)', link)
        if m: return 'xiaohongshu', m.group(1)
    return None, None

# ===== 触发 Dify =====
def trigger_dify(content):
    """调 Dify API 触发监控工作流"""
    if not DIFY_API_URL or not DIFY_API_KEY:
        print("⚠️ Dify API 未配置")
        return False
    body = json.dumps({
        "inputs": {"content": content},
        "response_mode": "blocking",
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
        with urllib.request.urlopen(req, timeout=60) as resp:
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
    return jsonify({"ok": True, "service": "wecom-callback"})

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
    """接收企微推送的消息"""
    try:
        signature = request.args.get('msg_signature', '')
        timestamp = request.args.get('timestamp', '')
        nonce = request.args.get('nonce', '')
        
        data = request.json or {}
        encrypt = data.get('Encrypt', '')
        
        if not encrypt:
            return "ok"
        
        # 验证签名
        calc_sig = verify_signature(WECOM_TOKEN, timestamp, nonce, encrypt)
        if calc_sig != signature:
            return "ok"
        
        # 解密消息
        msg_json = decrypt_msg(encrypt)
        msg = json.loads(msg_json)
        print(f"📩 收到消息: {msg}")
        
        # 检查是否包含链接
        content = msg.get('Content', '') or msg.get('text', {}).get('content', '')
        if not content:
            return "ok"
        
        platform, content_id = extract_platform_and_id(content)
        
        if platform and content_id:
            # 在后台线程触发 Dify（避免阻塞回调响应）
            def run():
                reply_to_group(f"✅ 已识别 {platform} 内容，正在启动监控...")
                success = trigger_dify(content)
                if success:
                    reply_to_group(f"📊 监控报告已生成，请查看 Dify")
                else:
                    reply_to_group(f"⚠️ 监控启动失败，请手动在 Dify 中运行")
            
            threading.Thread(target=run, daemon=True).start()
        else:
            # 不是链接，忽略（或者以后可以加其他指令）
            pass
        
        return "ok"
    except Exception as e:
        print(f"❌ 回调处理错误: {e}")
        return "ok"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"=== 企微回调服务 ===")
    print(f"端口: {port}")
    print(f"Webhook: {'已配置' if WECOM_BOT_WEBHOOK else '未配置'}")
    print(f"Dify API: {'已配置' if DIFY_API_URL else '未配置'}")
    app.run(host='0.0.0.0', port=port, debug=False)
