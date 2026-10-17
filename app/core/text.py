"""文本工具：模板渲染、条件表达式求值、运单号抽取。

条件表达式**不用 eval**：YAML 是配置文件，能执行代码的配置是安全隐患。
这里只支持三种形态，其它一律当作 False：
    slots.get('x')
    context.get('x')
    slots.get('x') == '某个值'
"""

from __future__ import annotations

import re

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")
_COND_GET = re.compile(r"^(slots|context)\.get\(\s*['\"]([\w]+)['\"]\s*\)$")
_COND_EQ = re.compile(
    r"^(slots|context)\.get\(\s*['\"]([\w]+)['\"]\s*\)\s*==\s*['\"](.*)['\"]$"
)

# 运单号：YD + 11 位数字（与本项目种子数据一致），另兼容纯数字 12~20 位
TRACKING_NO = re.compile(r"\b(YD\d{11}|\d{12,20})\b")


def _lookup(path: str, slots: dict, context: dict):
    parts = path.split(".")
    if len(parts) != 2:
        return ""
    scope, key = parts
    if scope == "slots":
        return slots.get(key, "")
    if scope == "context":
        return context.get(key, "")
    return ""


def render(template: str, slots: dict, context: dict | None = None) -> str:
    """把 {{ slots.x }} 换成实际值；缺失的占位替换成空串，不抛异常。"""
    context = context or {}
    return _PLACEHOLDER.sub(lambda m: str(_lookup(m.group(1), slots, context) or ""), template)


def eval_condition(expr: str, slots: dict, context: dict) -> bool:
    expr = (expr or "").strip()
    if not expr:
        return False

    m = _COND_EQ.match(expr)
    if m:
        scope, key, expected = m.groups()
        actual = (slots if scope == "slots" else context).get(key)
        return str(actual) == expected

    m = _COND_GET.match(expr)
    if m:
        scope, key = m.groups()
        return bool((slots if scope == "slots" else context).get(key))

    return False


def find_tracking_number(text: str) -> str:
    m = TRACKING_NO.search(text or "")
    return m.group(1).upper() if m else ""


def normalize(text: str) -> str:
    """全角转半角、去多余空白，运单号匹配前先过一遍。"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()
