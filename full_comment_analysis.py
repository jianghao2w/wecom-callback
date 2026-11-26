"""Analyze all top-level comments in bounded batches, failing closed on missing results."""

def analyze_all(comments, analyze_batch, summarize, batch_size=120):
    if not isinstance(comments, list) or batch_size <= 0:
        raise ValueError('评论数据或分批大小无效')
    numbers = [row.get('number') for row in comments]
    if any(not isinstance(n, int) or n <= 0 for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError('评论编号缺失或重复')
    counts = {'positive': 0, 'neutral': 0, 'negative': 0}
    negatives, summaries = [], []
    for start in range(0, len(comments), batch_size):
        batch = comments[start:start + batch_size]
        response = analyze_batch(batch)
        items = response.get('classifications') if isinstance(response, dict) else None
        if not isinstance(items, list):
            raise ValueError('分批分析结果缺失')
        expected = {row['number'] for row in batch}
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('评论分类格式错误')
            number, category = item.get('number'), item.get('sentiment')
            if number not in expected or number in seen or category not in counts:
                raise ValueError('评论分类编号或类型错误')
            seen.add(number)
            counts[category] += 1
            if category == 'negative':
                negatives.append({'number': number, 'reason': str(item.get('reason') or '')[:60]})
        if seen != expected:
            raise ValueError('分批分析未覆盖全部评论')
        summaries.append(str(response.get('overall') or '').strip())
    overall = summarize(summaries, counts) if comments else ''
    if not isinstance(overall, str) or (comments and not overall.strip()):
        raise ValueError('全量总结生成失败')
    return {'no_comments': not comments, 'overall': overall.strip(), 'negatives': negatives,
            'positive_count': counts['positive'], 'neutral_count': counts['neutral'],
            'analyzed_count': len(comments), 'classification_valid': True,
            'themes': [], 'urgent': [], 'response_suggestions': []}
