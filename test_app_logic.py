# -*- coding: utf-8 -*-
"""wecom-callback 登记链路逻辑测试（不启动 Flask、不发真实网络请求）"""
import importlib.util
import os
import sys
import tempfile

APP = r"C:\Users\EDY\.zcode\workspace\draft-c3ed6b78-5670-4113-af9a-1adc0a5b1141\wecom-callback\app.py"

spec = importlib.util.spec_from_file_location("wecom_app", APP)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# point storage at a temp file; mock short-link expansion (no real network)
mod.TARGETS_FILE = os.path.join(tempfile.mkdtemp(), "targets.json")
FINAL = "https://www.douyin.com/video/7345678901234567890"
mod.expand_short = lambda url, _f=FINAL: _f

failed = 0

def check(name, got, want):
    global failed
    ok = got == want
    if not ok:
        failed += 1
    print(f"{'PASS' if ok else 'FAIL'} {name}: got={got!r} want={want!r}")

# --- extract_platform_and_id: message text forms ---
check("消息含抖音完整链",
      mod.extract_platform_and_id("看看这个 https://www.douyin.com/video/7345678901234567890 挺好"),
      ("douyin", "7345678901234567890"))
check("消息含视频号链接",
      mod.extract_platform_and_id("登记 https://channels.weixin.qq.com/web/pages/feed/abc123def 谢谢"),
      ("channels", "abc123def"))
check("消息含抖音短链",
      mod.extract_platform_and_id("https://v.douyin.com/iRXXof9/"),
      ("douyin", "7345678901234567890"))
check("消息是 channel+ID 文本",
      mod.extract_platform_and_id("channel 1470000000000000000"),
      ("channels", "1470000000000000000"))
check("消息是中文平台名",
      mod.extract_platform_and_id("抖音 7345678901234567890"),
      ("douyin", "7345678901234567890"))
check("无法识别的消息",
      mod.extract_platform_and_id("今天天气不错"),
      (None, None))
check("非监控链接忽略",
      mod.extract_platform_and_id("看这个 https://www.baidu.com/s?wd=x"),
      (None, None))

# --- register / load roundtrip ---
check("首次登记", mod.register_target("channels", "abc123def"), (True, 1))
check("重复登记去重", mod.register_target("channels", "abc123def"), (False, 1))
check("第二个目标", mod.register_target("douyin", "7345678901234567890"), (True, 2))

data = mod.load_targets()
check("清单内容", [(t["platform"], t["id"]) for t in data],
      [("channels", "abc123def"), ("douyin", "7345678901234567890")])
check("登记带时间戳", all(isinstance(t.get("added_at"), int) for t in data), True)

print(("\nALL PASSED" if failed == 0 else f"\n{failed} FAILED"))
sys.exit(1 if failed else 0)
