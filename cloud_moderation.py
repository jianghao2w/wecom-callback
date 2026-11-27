"""Video Channels moderation in a persistent, operator-authorized browser."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from approval_store import ApprovalStore


PLATFORM = 'https://channels.weixin.qq.com'
HOME = PLATFORM + '/platform/interaction/comment?isImageMode=0'
API = PLATFORM + '/micro/interaction/cgi-bin/mmfinderassistant-bin'
FIELDS = ('_log_finder_uin', '_log_finder_id', 'rawKeyBuff', 'pluginSessionId', 'scene', 'reqScene')
LOGIN_TTL = 300


class LoginRequired(Exception):
    pass


class MatchError(Exception):
    pass


class ChannelsBrowser:
    def __init__(self, context):
        self.page = context.pages[0] if context.pages else context.new_page()
        self.page.set_default_timeout(6000)
        self.auth = None
        self.headers = None
        self.generation = 0
        self.page.on('response', self._read_auth)

    def _read_auth(self, response):
        url = urlsplit(response.url)
        if url.hostname != 'channels.weixin.qq.com' or not url.path.endswith('/auth/get_auth_info'):
            return
        self.auth = None
        self.generation += 1
        try:
            result = response.json()
            if result.get('errCode') != 0 or 'authType' not in (result.get('data') or {}):
                return
            request = response.request
            fields = request.post_data_json
            auth = {key: fields[key] for key in FIELDS}
            headers = request.headers
            self.headers = {key: headers[key] for key in ('x-wechat-uin', 'finger-print-device-id') if key in headers}
            self.auth = auth
        except Exception:
            return

    def open(self):
        self.auth = None
        self.page.goto(HOME, wait_until='domcontentloaded', timeout=20000)

    def qr(self):
        page_url = urlsplit(self.page.url)
        if page_url.hostname != 'channels.weixin.qq.com' or page_url.path != '/login.html':
            return None
        for frame in self.page.frames:
            location = urlsplit(frame.url)
            if location.scheme != 'https' or location.hostname != 'open.weixin.qq.com' or location.path != '/connect/qrconnect':
                continue
            image = frame.locator('img.js_qrcode_img:visible')
            if image.count() == 1 and image.evaluate('(e) => e.complete && e.naturalWidth >= 100'):
                return image.screenshot(timeout=2000)
        return None

    def post(self, endpoint, values):
        if self.auth is None or urlsplit(self.page.url).hostname != 'channels.weixin.qq.com':
            raise LoginRequired
        body = {**self.auth, **values, 'timestamp': str(int(time.time() * 1000))}
        result = self.page.evaluate('''async ({url, headers, body}) => {
            const response = await fetch(url, {method:'POST', redirect:'error',
                headers:{'content-type':'application/json', ...headers}, body:JSON.stringify(body),
                signal:AbortSignal.timeout(15000)});
            return response.json();
        }''', {'url': API+endpoint, 'headers': self.headers, 'body': body})
        if result.get('errCode') == 300330:
            self.auth = None
            raise LoginRequired
        if result.get('errCode') != 0 or not isinstance(result.get('data'), dict):
            raise MatchError
        return result['data']

    def _pages(self, endpoint, args, rows_key, flag, response_cursor, request_cursor):
        cursor = None
        seen = set()
        for _ in range(200):
            data = self.post(endpoint, {**args, **({request_cursor: cursor} if cursor else {})})
            rows = data.get(rows_key)
            if not isinstance(rows, list) or data.get(flag) not in (0, 1):
                raise MatchError
            yield from rows
            if not data[flag]:
                return
            cursor = data.get(response_cursor)
            key = json.dumps(cursor, sort_keys=True)
            if not cursor or key in seen:
                raise MatchError
            seen.add(key)
        raise MatchError

    def export_id(self, video):
        posts = self._pages('/post/post_list', {'pageSize': 10, 'onlyUnread': False,
            'needAllCommentCount': True, 'userpageType': 13, 'forMcn': False},
            'list', 'continueFlag', 'lastBuff', 'lastBuff')
        matches = [post for post in posts if (post.get('desc') or {}).get('description') == video['description']
                   and str(post.get('createTime')) == str(video['create_time']) and post.get('exportId')]
        if len(matches) != 1:
            raise MatchError
        return matches[0]['exportId']

    def verify_comments(self, export_id, comments):
        parents = {str(item.get('parent_comment_id') or '') for item in comments}
        root = None
        for parent in sorted(parents | {''}):
            args = {'lastBuff': '', 'exportId': export_id, 'commentSelection': False, 'forMcn': False}
            if parent:
                if root is None or parent not in root:
                    raise MatchError
                args['rootCommentId'] = parent
            rows = {}
            for row in self._pages('/comment/comment_list', args, 'comment', 'downContinueFlag', 'lastBuff', 'lastBuff'):
                comment_id = str(row.get('commentId') or '')
                if not comment_id or comment_id in rows:
                    raise MatchError
                rows[comment_id] = row
            if not parent:
                root = rows
            for item in comments:
                if str(item.get('parent_comment_id') or '') != parent:
                    continue
                row = rows.get(item['comment_id'])
                if not row:
                    raise MatchError
                if item.get('media_only') is True:
                    author = item.get('author')
                    if not author or author == '匿名' or row.get('commentNickname') != author or row.get('commentContent') != '[动画表情]':
                        raise MatchError
                elif row.get('commentContent') != item['content']:
                    raise MatchError

    def delete(self, export_id, comment_id):
        result = self.post('/comment/del_comment', {'exportId': export_id, 'commentId': comment_id})
        if (result.get('baseResp') or {}).get('errcode') != 0:
            raise MatchError


LOGIN_PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>视频号云端登录</title><main><h1>视频号云端登录</h1><p id="status" role="status">正在加载</p><img id="qr" hidden alt="视频号助手官方登录二维码"></main>
<script nonce="NONCE">
const key=location.hash.slice(1);
history.replaceState(null,'',location.pathname);
const statusEl=document.querySelector('#status'), qr=document.querySelector('#qr');
async function update(){
  try {
    const r=await fetch('/cloud-login/qr',{method:'POST',headers:{'Authorization':'Bearer '+key}});
    if(!r.ok){qr.hidden=true;statusEl.textContent='验证入口已失效，请重新申请。';return;}
    const d=await r.json();
    statusEl.textContent='目标视频号：'+d.account+' · '+d.message;
    qr.hidden=!d.png;
    if(d.png)qr.src='data:image/png;base64,'+d.png;
    else qr.removeAttribute('src');
    if(d.status!=='ready')setTimeout(update,1500);
  } catch {qr.hidden=true;statusEl.textContent='暂时无法连接，正在重试。';setTimeout(update,3000);}
}
update();
</script></html>'''


class CloudModeration:
    def __init__(self, path, account, anchor_id, origin, metadata, notice):
        self.store = ApprovalStore(path)
        self.profile = Path(path).parent / 'channels-browser'
        self.account, self.anchor_id = account, anchor_id
        self.origin, self.metadata, self.notice = origin, metadata, notice
        self.lock = threading.Lock()
        self.status = 'idle'
        self.challenge = None
        self.png = None
        self.wake = threading.Event()
        self.reset = False
        self.active = self.store.cloud_enabled()

    def issue_login(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.challenge = (hashlib.sha256(token.encode()).digest(), time.monotonic() + LOGIN_TTL)
            self.png = None
            self.status = 'opening'
            self.reset = True
        self.wake.set()
        return self.origin + '/cloud-login#' + token

    def qr_response(self, token):
        with self.lock:
            challenge = self.challenge
            if not challenge or time.monotonic() >= challenge[1] or not hmac.compare_digest(
                hashlib.sha256(token.encode()).digest(), challenge[0]
            ):
                return None
            messages = {'opening': '正在打开微信扫码页。', 'scan': '请用微信扫码并确认登录。',
                'ready': '登录已验证。', 'wrong_account': '未能核验目标账号，请联系管理员。',
                'unavailable': '暂时无法打开视频号助手。'}
            return {'account': self.account, 'status': self.status, 'message': messages.get(self.status, '等待扫码。'),
                    'png': base64.b64encode(self.png).decode() if self.png and self.status == 'scan' else None}

    def publish_status(self, status, png=None):
        with self.lock:
            if self.reset:
                return
            self.status, self.png = status, png

    def process(self, browser, batch_id):
        batch = self.store.claim_cloud(batch_id)
        if not batch:
            return
        comments = batch['payload']
        completed = self.store.cloud_deleted(batch_id)
        remaining = [item for item in comments if item['comment_id'] not in completed]
        try:
            video_id = comments[0]['object_id']
            video = self.metadata(video_id)
            if not video or str(video.get('id')) != video_id or video.get('account_name') not in ('', None, self.account):
                raise MatchError
            if (len({item['comment_id'] for item in comments}) != len(comments)
                or any(item['platform'] != 'channels' or item['object_id'] != video_id for item in comments)):
                raise MatchError
            if remaining:
                export_id = browser.export_id(video)
                browser.verify_comments(export_id, remaining)
        except LoginRequired:
            self.store.wait_cloud(batch_id)
            raise
        except Exception:
            self.store.finish(batch_id, {'status': 'rejected', 'deleted': completed})
            self.notice(f'云端无法核验批次 {batch_id} 的作品或评论，已停止。')
            return
        for item in remaining:
            comment_id = item['comment_id']
            self.store.begin_cloud_item(batch_id, comment_id)
            try:
                browser.delete(export_id, comment_id)
            except LoginRequired:
                self.store.wait_cloud(batch_id, comment_id)
                raise
            except Exception:
                self.store.finish(batch_id, {'status': 'unknown', 'deleted': completed, 'uncertain': comment_id})
                self.notice(f'云端批次 {batch_id} 已确认删除 {len(completed)} 条。另有一条结果不明确，请核对后台。')
                return
            self.store.complete_cloud_item(batch_id, comment_id)
            completed.append(comment_id)
        self.store.finish(batch_id, {'status': 'completed', 'deleted': completed})
        self.notice(f'云端删除完成：批次 {batch_id} 已删除 {len(completed)} 条。')

    def loop(self):
        import fcntl
        from playwright.sync_api import sync_playwright

        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.profile.parent / 'channels-browser.lock').open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if self.store.recover_cloud():
                try:
                    self.notice('云端执行曾中断，结果不明的批次已停止，请核对视频号后台。')
                except Exception as error:
                    print(f'云端通知发送失败: {type(error).__name__}', flush=True)
            with sync_playwright() as pw:
                context = None
                browser = None
                verified = None
                opened_at = 0
                wrong_account = False
                needs_notice = self.active
                while True:
                    with self.lock:
                        challenge, status = self.challenge, self.status
                        reset, self.reset = self.reset, False
                    pending_login = challenge and status != 'ready' and time.monotonic() < challenge[1]
                    if not self.active and not pending_login:
                        self.wake.wait(5)
                        self.wake.clear()
                        continue
                    try:
                        if browser is None:
                            context = pw.chromium.launch_persistent_context(str(self.profile), headless=True)
                            browser = ChannelsBrowser(context)
                            reset = True
                        if reset:
                            if wrong_account:
                                context.clear_cookies()
                                wrong_account = False
                            verified = None
                            browser.open()
                            opened_at = time.monotonic()
                        browser.page.wait_for_timeout(900)
                        if browser.auth is not None and verified != browser.generation:
                            anchor = self.metadata(self.anchor_id)
                            if not anchor or anchor.get('account_name') != self.account:
                                raise MatchError
                            try:
                                browser.export_id(anchor)
                            except MatchError:
                                self.publish_status('wrong_account')
                                wrong_account = True
                                browser.auth = None
                                continue
                            verified = browser.generation
                            self.publish_status('ready')
                            needs_notice = True
                            if self.active:
                                self.store.resume_cloud()
                        if browser.auth is None and pending_login:
                            qr = browser.qr()
                            if qr:
                                self.publish_status('scan', qr)
                        if not self.active:
                            continue
                        batches = self.store.confirmed_batches()
                        if browser.auth is not None and verified == browser.generation and batches:
                            if time.monotonic() - opened_at > 60:
                                browser.open()
                                opened_at = time.monotonic()
                                continue
                            self.process(browser, batches[0])
                        elif (browser.auth is None and needs_notice and not pending_login and
                              time.monotonic() - opened_at > 8 and (batches or self.store.cloud_waiting())):
                            needs_notice = False
                            link = self.issue_login()
                            self.notice(f'目标视频号「{self.account}」需要重新验证，待删除任务已暂停。请在 5 分钟内打开官方二维码：{link}')
                    except LoginRequired:
                        browser.auth = None
                        verified = None
                        opened_at = 0
                    except Exception as error:
                        print(f'云端浏览器暂不可用: {type(error).__name__}', flush=True)
                        self.publish_status('unavailable')
                        if context:
                            try:
                                context.close()
                            except Exception:
                                pass
                        context = browser = None
                        if self.store.recover_cloud():
                            try:
                                self.notice('云端执行曾中断，结果不明的批次已停止，请核对视频号后台。')
                            except Exception:
                                pass
                        self.wake.wait(10)
