"""从模型输出里抠 JSON。

不用 find('{')+rfind('}')——模型常在 JSON 后面再解释一段话，那样会把尾巴吃进来。
这里做花括号配平扫描，并跳过字符串内部的括号。
"""

from __future__ import annotations

import json
from typing import Any


def extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()

    # 优先剥 ```json 围栏
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{"):
                parsed = _scan(candidate)
                if parsed is not None:
                    return parsed

    return _scan(text)


def _scan(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : i + 1]
                    try:
                        value = json.loads(chunk)
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = text.find("{", start + 1)
    return None
