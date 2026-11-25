"""Video-account deletion primitives. Call only for an approved, resolved batch."""
import json
import time
import urllib.request
from channels_id_mapping import match_export_id

BASE = 'https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin'
SESSION_FIELDS = ('token', '_log_finder_uin', '_log_finder_id', 'rawKeyBuff',
                  'pluginSessionId', 'scene', 'reqScene')


class ChannelsError(Exception):
    pass


def post_json(session, endpoint, fields, opener=urllib.request.urlopen):
    if not session.get('cookie') or any(k not in session for k in SESSION_FIELDS):
        raise ChannelsError('视频号登录信息不完整')
    payload = {k: session[k] for k in SESSION_FIELDS}
    payload.update(fields)
    payload['timestamp'] = str(int(time.time() * 1000))
    headers = {'Content-Type': 'application/json', 'Cookie': session['cookie'],
               'User-Agent': session.get('user_agent') or 'Mozilla/5.0'}
    if session.get('wechat_uin'):
        headers['X-WECHAT-UIN'] = session['wechat_uin']
    if session.get('device_id'):
        headers['finger-print-device-id'] = session['device_id']
    req = urllib.request.Request(BASE + endpoint, data=json.dumps(payload).encode('utf-8'),
                                 headers=headers, method='POST')
    with opener(req, timeout=15) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ChannelsError('视频号返回格式不正确')
    return result


def creator_posts(session, opener=urllib.request.urlopen, max_pages=200):
    posts, last, seen = [], None, set()
    for _ in range(max_pages):
        fields = {'pageSize': 10, 'onlyUnread': False, 'needAllCommentCount': True,
                  'userpageType': 13, 'forMcn': False}
        if last:
            fields['lastBuff'] = last
        result = post_json(session, '/post/post_list', fields, opener)
        data = result.get('data')
        if result.get('errCode') != 0 or not isinstance(data, dict) or not isinstance(data.get('list'), list):
            raise ChannelsError('视频号作品列表读取失败')
        posts.extend(data['list'])
        more = data.get('continueFlag')
        if more in (0, False):
            return posts
        if more not in (1, True):
            raise ChannelsError('作品分页状态未知')
        last = data.get('lastBuff')
        key = json.dumps(last, sort_keys=True)
        if not last or key in seen:
            raise ChannelsError('作品分页游标异常')
        seen.add(key)
    raise ChannelsError('作品列表未读取完整')


def delete_comment(session, export_id, comment_id, opener=urllib.request.urlopen):
    if not isinstance(export_id, str) or not export_id.strip() or not str(comment_id or '').strip():
        raise ChannelsError('作品或评论标识缺失')
    result = post_json(session, '/comment/del_comment',
                       {'exportId': export_id, 'commentId': str(comment_id)}, opener)
    data = result.get('data')
    base = data.get('baseResp') if isinstance(data, dict) else None
    if result.get('errCode') == 0 and isinstance(base, dict) and base.get('errcode') == 0:
        return 'deleted'
    raise ChannelsError('删除未确认成功')


def verified_comments(session, export_id, candidates, opener=urllib.request.urlopen, max_pages=200):
    """Resolve approved IDs against the creator account, including exact text."""
    expected = {}
    for item in candidates:
        ident = str(item.get('comment_id') or '').strip()
        content = item.get('content')
        if not ident or not isinstance(content, str) or not content:
            raise ChannelsError('待删评论缺少标识或内容')
        if ident in expected:
            raise ChannelsError('待删评论标识重复')
        expected[ident] = content
    if not expected:
        raise ChannelsError('待删评论为空')
    found, last, seen = {}, '', set()
    for _ in range(max_pages):
        result = post_json(session, '/comment/comment_list',
                           {'lastBuff': last, 'exportId': export_id,
                            'commentSelection': False, 'forMcn': False}, opener)
        data = result.get('data')
        if result.get('errCode') != 0 or not isinstance(data, dict) or not isinstance(data.get('comment'), list):
            raise ChannelsError('作品评论列表读取失败')
        for row in data['comment']:
            ident = str(row.get('commentId') or '') if isinstance(row, dict) else ''
            if ident in expected:
                if ident in found or row.get('commentContent') != expected[ident]:
                    raise ChannelsError('评论标识或内容匹配异常')
                found[ident] = row
        more = data.get('downContinueFlag')
        if more in (0, False):
            if len(found) != len(expected):
                raise ChannelsError('部分待删评论未找到')
            return found
        if more not in (1, True):
            raise ChannelsError('评论分页状态未知')
        last = data.get('lastBuff')
        if not last or last in seen:
            raise ChannelsError('评论分页游标异常')
        seen.add(last)
    raise ChannelsError('评论列表未读取完整')


def delete_verified_batch(session, video, candidates, opener=urllib.request.urlopen):
    """Execute only after the caller has atomically claimed a confirmed batch."""
    if not isinstance(candidates, list) or not candidates:
        raise ChannelsError('待删评论为空')
    object_id = str(video.get('id') or '') if isinstance(video, dict) else ''
    if any(not isinstance(c, dict) or c.get('platform') != 'channels' or
           str(c.get('object_id') or '') != object_id for c in candidates):
        raise ChannelsError('待删评论和作品不匹配')
    posts = creator_posts(session, opener)
    try:
        export_id = match_export_id(video, posts, True)['export_id']
    except ValueError as error:
        raise ChannelsError('无法唯一确认作品') from error
    verified_comments(session, export_id, candidates, opener)
    deleted = []
    for i, item in enumerate(candidates):
        try:
            delete_comment(session, export_id, item['comment_id'], opener)
        except Exception:
            # The server may have acted before the connection failed: stop here.
            return {'status': 'unknown', 'deleted': deleted,
                    'uncertain': str(item['comment_id']),
                    'not_attempted': [str(c['comment_id']) for c in candidates[i + 1:]]}
        deleted.append(str(item['comment_id']))
    return {'status': 'completed', 'deleted': deleted}
