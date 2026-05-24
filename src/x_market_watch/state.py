from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class State:
    last_seen_id: str | None = None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return State(last_seen_id=payload.get("last_seen_id"))

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"last_seen_id": state.last_seen_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
