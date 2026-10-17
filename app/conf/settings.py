"""配置。全部来自环境变量，缺省值保证"不改任何东西也能跑起来"。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class LLMSettings:
    base_url: str = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    api_key: str = os.getenv("LLM_API_KEY", "")
    # 规划器每轮都要调，默认用便宜的；低置信度时再交给强模型复核
    planner_model: str = os.getenv("LLM_PLANNER_MODEL", "qwen-flash")
    writer_model: str = os.getenv("LLM_WRITER_MODEL", "qwen-plus")
    timeout: float = _float("LLM_TIMEOUT", 60.0)
    temperature: float = _float("LLM_TEMPERATURE", 0.2)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("请填入")


@dataclass
class CommerceSettings:
    base_url: str = os.getenv("COMMERCE_BASE_URL", "http://127.0.0.1:18081")
    timeout: float = _float("COMMERCE_TIMEOUT", 8.0)


@dataclass
class DialogueSettings:
    db_path: Path = Path(os.getenv("DIALOGUE_DB", str(ROOT / "data" / "dialogue.db")))
    flow_dir: Path = Path(os.getenv("FLOW_DIR", str(ROOT / "config" / "flows")))
    kb_dir: Path = Path(os.getenv("KB_DIR", str(ROOT / "config" / "kb")))
    max_paused: int = _int("MAX_PAUSED_TASKS", 3)
    # 低于这个置信度就不猜，走澄清
    clarify_threshold: float = _float("CLARIFY_THRESHOLD", 0.55)
    history_window: int = _int("HISTORY_WINDOW", 8)


@dataclass
class ServerSettings:
    host: str = os.getenv("SERVER_HOST", "127.0.0.1")
    port: int = _int("SERVER_PORT", 18082)


@dataclass
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    commerce: CommerceSettings = field(default_factory=CommerceSettings)
    dialogue: DialogueSettings = field(default_factory=DialogueSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    debug: bool = os.getenv("DEBUG", "").lower() in {"1", "true", "yes"}


settings = Settings()
