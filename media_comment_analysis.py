"""Fetch WeChat comment images and classify selected frames using a vision model."""
import base64
import io
import json
import urllib.parse
import urllib.request
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_BYTES = 4 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 12_000_000
ALLOWED_HOSTS = ('mmbiz.qpic.cn', 'wxapp.tc.qq.com', 'snsvideo.hk.wechat.com')
PROMPT = ('判断这条评论中的表情或图片，结合评论文字和父评论语境。图片按时间顺序展示，'
          '先描述画面及文字，再判定 positive、neutral、negative 或 unknown。'
          '明确讽刺、质疑、催促、辱骂或贬损可判 negative；含义不清不要猜测，判 unknown。'
          '只返回 JSON：{"description":"简短中文描述","sentiment":"positive|neutral|negative|unknown"}。')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def media_urls(raw):
    info = raw.get('contentInfo') if isinstance(raw, dict) else None
    if not isinstance(info, dict): return []
    urls=[]
    def scan(value, depth=0):
        if depth > 6: return
        if isinstance(value, list):
            for child in value[:10]: scan(child, depth+1)
        elif isinstance(value, dict):
            for key in ('url', 'imageUrl', 'image_url', 'thumbUrl', 'thumb_url'):
                value_url=value.get(key)
                if isinstance(value_url,str):
                    try:
                        p=urllib.parse.urlsplit(value_url)
                        if p.scheme in ('http','https') and p.hostname in ALLOWED_HOSTS and not p.username and not p.password and p.port in (None,80,443):
                            secure=urllib.parse.urlunsplit(('https',p.hostname,p.path,p.query,''))
                            if secure not in urls:urls.append(secure)
                    except ValueError:
                        pass
            for key, child in value.items():
                if key not in ('url','imageUrl','image_url','thumbUrl','thumb_url','encryptUrl','externUrl'):
                    scan(child,depth+1)
    selected=[]
    groups=[v for v in (info.get('emoticonInfos'), info.get('imageInfos')) if isinstance(v,list)]
    for asset in (item for group in groups for item in group):
        urls.clear();scan(asset)
        if urls and urls[0] not in selected:selected.append(urls[0])
    return selected[:8]

def download_frames(url, opener=None):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('图片地址不受信任')
    opener=opener or urllib.request.build_opener(NoRedirect()).open
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'image/*'})
    with opener(req,timeout=20) as response:
        data=response.read(MAX_BYTES+1)
    if not data or len(data)>MAX_BYTES: raise ValueError('图片大小无效')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ('PNG','JPEG','GIF','WEBP'): raise ValueError('图片格式不受支持')
            n=int(getattr(im,'n_frames',1)); indices=sorted(set((0,n//3,2*n//3,n-1))) if n>1 else [0]
            frames=[]
            for i in indices:
                im.seek(i); frame=ImageOps.exif_transpose(im.convert('RGB'));frame.thumbnail((512,512))
                out=io.BytesIO();frame.save(out,format='PNG',optimize=True);frames.append(out.getvalue())
            return frames

def analyze_image(url, api_url, api_key, model='deepseek-flash', context='', parent='', opener=urllib.request.urlopen):
    if not api_url.startswith('https://') or not api_key or not model:raise ValueError('视觉模型配置不完整')
    frames=download_frames(url)
    content=[{'type':'text','text':'评论文字：'+str(context)[:1000]+'\n父评论：'+str(parent)[:1000]}]
    content += [{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(frame).decode('ascii')}} for frame in frames]
    payload={'model':model,'thinking':{'type':'disabled'},'temperature':0,'max_tokens':500,
             'response_format':{'type':'json_object'},'messages':[{'role':'system','content':PROMPT},{'role':'user','content':content}]}
    req=urllib.request.Request(api_url,data=json.dumps(payload,ensure_ascii=False).encode(),method='POST',headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'})
    with opener(req,timeout=90) as response:result=json.load(response)
    parsed=json.loads(result['choices'][0]['message']['content']);sentiment=parsed.get('sentiment');description=parsed.get('description')
    if sentiment not in ('positive','neutral','negative','unknown') or not isinstance(description,str) or not description.strip():raise ValueError('视觉模型结果无效')
    return {'description':description.strip()[:300],'sentiment':sentiment,'frame_count':len(frames)}
