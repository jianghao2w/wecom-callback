#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, hashlib, base64, re, threading
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

app = Flask(__name__)

WECOM_TOKEN = os.environ.get("WECOM_CALLBACK_TOKEN", "")
WECOM_AES_KEY = os.environ.get("WECOM_CALLBACK_AES_KEY", "")
WECOM_BOT_WEBHOOK = os.environ.get("WECOM_BOT_WEBHOOK", "")
DIFY_API_URL = os.environ.get("DIFY_API_URL", "")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")

def decrypt_msg(encrypt_text):
    key = base64.b64decode(WECOM_AES_KEY + "=")
    data = base64.b64decode(encrypt_text)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    pad_len = padded[-1]
    content = padded[:-pad_len]
    msg_len = int.from_bytes(content[16:20], 'big')
    return content[20:20+msg_len].decode('utf-8')

def verify_signature(token, timestamp, nonce, encrypt):
    sort_list = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(sort_list).encode('utf-8')).hexdigest()

LINK_PATTERN = re.compile(r'https?://[^\s<>"\']+')

def extract_platform_and_id(text):
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

def trigger_dify(content):
    if not DIFY_API_URL or not DIFY_API_KEY:
        print("Dify API not configured")
        return False
    body = json.dumps({"inputs": {"content": content}, "response_mode": "blocking"}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(DIFY_API_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + DIFY_API_KEY}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print("Dify triggered OK")
            return True
    except Exception as e:
        print("Dify trigger failed: " + str(e))
        return False

def reply_to_group(message):
    if not WECOM_BOT_WEBHOOK:
        return
    body = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(WECOM_BOT_WEBHOOK, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("Group reply OK")
    except Exception as e:
        print("Group reply failed: " + str(e))

@app.route('/healthz', methods=['GET'])
def health():
    return jsonify({"ok": True, "version": "v2.1"})

@app.route('/wecom', methods=['GET'])
def wecom_verify():
    signature = request.args.get('msg_signature', '')
    timestamp = request.args.get('timestamp', '')
    nonce = request.args.get('nonce', '')
    echostr = request.args.get('echostr', '')
    calc_sig = verify_signature(WECOM_TOKEN, timestamp, nonce, echostr)
    if calc_sig != signature:
        print("VERIFY FAIL: got=" + signature + " calc=" + calc_sig)
        return "signature error", 403
    return decrypt_msg(echostr)

@app.route('/wecom', methods=['POST'])
def wecom_callback():
    try:
        signature = request.args.get('msg_signature', '')
        timestamp = request.args.get('timestamp', '')
        nonce = request.args.get('nonce', '')
        raw_body = request.get_data()
        print("RAW: " + str(raw_body[:300]))

        encrypt = ""
        try:
            root = ET.fromstring(raw_body)
            e = root.find('Encrypt')
            if e is not None and e.text:
                encrypt = e.text.strip()
        except:
            pass

        if not encrypt:
            print("NO ENCRYPT FIELD")
            return "ok"

        calc_sig = verify_signature(WECOM_TOKEN, timestamp, nonce, encrypt)
        if calc_sig != signature:
            print("SIG FAIL: got=" + signature + " calc=" + calc_sig)
            return "ok"

        try:
            msg_json = decrypt_msg(encrypt)
            print("DECRYPTED: " + msg_json[:300])
        except Exception as e:
            print("DECRYPT FAIL: " + str(e))
            return "ok"

        msg = {}
        try:
            root = ET.fromstring(msg_json)
            for child in root:
                msg[child.tag] = (child.text or '').strip()
        except:
            pass

        print("PARSED: " + str(msg))
        content = msg.get('Content', '')
        if not content:
            return "ok"

        platform, content_id = extract_platform_and_id(content)
        print("PLATFORM: " + str(platform) + " ID: " + str(content_id))

        if platform and content_id:
            def run():
                reply_to_group("OK " + platform + " monitoring...")
                trigger_dify(content)
            threading.Thread(target=run, daemon=True).start()

        return "ok"
    except Exception as e:
        print("ERROR: " + str(e))
        return "ok"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print("=== v2.1 started ===")
    print("Token: " + WECOM_TOKEN[:8] + "...")
    print("AES: " + WECOM_AES_KEY[:8] + "...")
    print("Webhook: " + WECOM_BOT_WEBHOOK[:30] + "...")
    print("DifyURL: " + DIFY_API_URL)
    app.run(host='0.0.0.0', port=port, debug=False)
