"""Match TikHub video metadata to an authenticated creator's complete post list."""
import json
import sys


def match_export_id(video, creator_posts, all_pages_fetched):
    if all_pages_fetched is not True:
        raise ValueError("视频号助手作品列表尚未全部读取")
    if not isinstance(video, dict) or not isinstance(creator_posts, list):
        raise ValueError("作品详情或作品列表格式错误")
    description = video.get("description")
    created = video.get("create_time")
    numeric_id = str(video.get("id") or "").strip()
    if not isinstance(description, str) or not description.strip():
        raise ValueError("作品描述缺失，无法建立可靠映射")
    if not numeric_id.isdigit() or not str(created or "").isdigit() or int(created) <= 0:
        raise ValueError("作品数字 ID 或发布时间缺失")

    matches = []
    for post in creator_posts:
        if not isinstance(post, dict):
            raise ValueError("作品列表包含无效记录")
        desc = post.get("desc")
        if not isinstance(desc, dict):
            raise ValueError("作品列表记录缺少描述")
        timestamp = post.get("createTime")
        if not str(timestamp or "").isdigit():
            raise ValueError("作品列表记录缺少发布时间")
        text = desc.get("description")
        if not isinstance(text, str):
            raise ValueError("作品列表记录缺少描述文本")
        if text == description and int(timestamp) == int(created):
            matches.append(post)

    if len(matches) != 1:
        raise ValueError("未找到唯一匹配的作品")
    export_id = matches[0].get("exportId")
    if not isinstance(export_id, str) or not export_id.strip():
        raise ValueError("匹配作品缺少 exportId")
    return {"object_id": numeric_id, "export_id": export_id}


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        output = match_export_id(payload.get("video"), payload.get("creator_posts"),
                                 payload.get("all_pages_fetched"))
        print(json.dumps(output, ensure_ascii=False))
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        sys.exit(1)
