"""前端静态一致性检查：els 引用 / DOM id / i18n 键 / 禁用模态 API。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "views/engines/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "views/engines/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "views/engines/style.css").read_text(encoding="utf-8")
ZH = json.loads((ROOT / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8"))["views"]["engines"]
EN = json.loads((ROOT / ".astrbot-plugin/i18n/en-US.json").read_text(encoding="utf-8"))["views"]["engines"]

failures = []


def fail(msg):
    failures.append(msg)


# 1. els 字段必须来自 byId 赋值
els_block = JS[JS.index("Object.assign(els, {"):]
els_block = els_block[: els_block.index("});")]
assigned = set(re.findall(r"^\s{4}(\w+):\s*byId\(", els_block, re.M))
used = set(re.findall(r"\bels\.(\w+)", JS))
missing_def = sorted(used - assigned)
unused = sorted(assigned - used)
if missing_def:
    fail(f"els 未定义却被使用: {missing_def}")
if unused:
    fail(f"els 已定义但未使用: {unused}")

# 2. byId 的 id 必须存在于 HTML
ids = set(re.findall(r'id="([^"]+)"', HTML))
byid = set(re.findall(r'byId\("([^"]+)"\)', JS))
missing_id = sorted(byid - ids)
if missing_id:
    fail(f"HTML 缺少 id: {missing_id}")
orphan_ids = sorted(ids - byid - {"player", "notice", "banner"})
if orphan_ids:
    fail(f"HTML 中未被引用的 id: {orphan_ids}")

# 3. t("key") 必须在两份 i18n 里都有
keys = set(re.findall(r'\bt\(\s*"([a-z0-9_]+)"', JS))
missing_zh = sorted(keys - set(ZH))
missing_en = sorted(keys - set(EN))
if missing_zh:
    fail(f"zh-CN 缺少键: {missing_zh}")
if missing_en:
    fail(f"en-US 缺少键: {missing_en}")
extra_zh = sorted(set(ZH) - keys)
if extra_zh:
    fail(f"zh-CN 多余未使用的键: {extra_zh}")

# 4. 禁止 window.confirm / alert / prompt（视图 iframe 无 allow-modals）
stripped = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
for api in ("confirm", "alert", "prompt"):
    if re.search(rf"window\.{api}\s*\(", stripped) or re.search(
        rf"(?<![\w.]){api}\s*\(", stripped
    ):
        fail(f"检测到被沙箱屏蔽的 {api}() 调用")

# 5. 每个 panel 的 hidden 属性都有对应的样式保护
if "[hidden]" not in CSS:
    fail("CSS 缺少 [hidden] 保护，tab 面板可能同时显示")

# 6. 标签页与面板必须成对
tabs = set(re.findall(r'data-tab="(\w+)"', HTML))
panels = set(re.findall(r'id="tab-(\w+)"', HTML))
if tabs != panels:
    fail(f"标签与面板不匹配: tabs={sorted(tabs)} panels={sorted(panels)}")
for name in sorted(tabs):
    if f'id="tab-{name}-btn"' not in HTML:
        fail(f"缺少标签按钮 tab-{name}-btn")
    if f'id="tab-{name}"' not in HTML:
        fail(f"缺少面板 tab-{name}")

# 7. 前端声明的 TABS 必须覆盖全部标签
m = re.search(r'const TABS = \[([^\]]+)\]', JS)
declared = set(re.findall(r'"(\w+)"', m.group(1))) if m else set()
if declared != tabs:
    fail(f"TABS 与 HTML 标签不一致: {sorted(declared)} vs {sorted(tabs)}")

print(f"els 引用 {len(used)} 个，DOM id {len(ids)} 个，i18n 键 {len(keys)} 个，标签 {len(tabs)} 个")
if failures:
    print(f"\n=== {len(failures)} 项不一致 ===")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("=== 前端一致性检查全部通过 ===")
