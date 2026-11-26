"""DeepSeek adapter for batched comment classification."""
import json
import urllib.request
from full_comment_analysis import analyze_all

RULES = ('你是评论分析助手。对每条评论分别分类：positive表示明确赞美、感谢、支持或鼓励；'
         'neutral表示普通提问、陈述、合理质疑、温和批评或含义不明；negative仅限明确辱骂、人身攻击、'
         '恶意诽谤、威胁、垃圾广告或明显具有攻击性的强烈贬损。存疑归neutral。'
         '输入编号必须全部各出现一次。返回JSON对象：{"classifications":[{"number":1,'
         '"sentiment":"positive","reason":""}],"overall":"本批中文总结"}。'
         'sentiment只能是positive、neutral或negative，negative给出简短原因。')

class DeepSeekBatch:
    def __init__(self, url, api_key, model, opener=urllib.request.urlopen):
        if not url.startswith('https://') or not api_key or not model:
            raise ValueError('模型接口配置不完整')
        self.url, self.api_key, self.model, self.opener = url, api_key, model, opener

    def _request(self, instructions, data):
        payload={'model':self.model,'temperature':0,'max_tokens':6000,
                 'response_format':{'type':'json_object'},'messages':[
                     {'role':'system','content':instructions},
                     {'role':'user','content':json.dumps(data,ensure_ascii=False)}]}
        request=urllib.request.Request(self.url,data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
                  headers={'Content-Type':'application/json','Authorization':'Bearer '+self.api_key},method='POST')
        with self.opener(request,timeout=90) as response:
            result=json.load(response)
        return json.loads(result['choices'][0]['message']['content'])

    def analyze(self, comments):
        def batch(rows):
            return self._request(RULES,{'comments':[{'number':r['number'],'content':str(r.get('content') or '')} for r in rows]})
        def summary(summaries, counts):
            result=self._request('根据所有分批总结与分类计数，返回JSON：{"overall":"中文总体总结"}。不要编造信息。',
                                 {'batch_summaries':summaries,'counts':counts})
            return result.get('overall')
        available=[row for row in comments if not row.get('content_unavailable')]
        result=analyze_all(available,batch,summary)
        unavailable=len(comments)-len(available)
        result['unavailable_count']=unavailable
        if unavailable and not available:
            result['no_comments']=False
            result['overall']='评论内容暂无法获取，无法判断情绪。'
        return result
