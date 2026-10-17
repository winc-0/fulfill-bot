"""状态持久化。

选 SQLite 而不是 MySQL：这是单进程 Demo，状态读写是每轮一次的小 KV，
SQLite 足够且让仓库"clone 下来直接跑"。真上线时把这张表换成 Redis 就行，
接口只有 get/save 两个方法。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from app.dialogue.state import DialogueState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogue_states (
    sender_id  TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class StateRepo:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, sender_id: str) -> DialogueState:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT state_json FROM dialogue_states WHERE sender_id = ?", (sender_id,)
            ).fetchone()
        if not row:
            return DialogueState(sender_id=sender_id)
        return DialogueState.from_dict(json.loads(row["state_json"]))

    def save(self, state: DialogueState) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO dialogue_states (sender_id, state_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(sender_id) DO UPDATE SET state_json = excluded.state_json, "
                "updated_at = excluded.updated_at",
                (state.sender_id, state.dumps(), datetime.now().isoformat(timespec="seconds")),
            )

    def reset(self, sender_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM dialogue_states WHERE sender_id = ?", (sender_id,))
