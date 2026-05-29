from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from x_market_watch.models import Post, Signal


@dataclass(frozen=True)
class StoredSignal:
    id: str
    post_id: str
    title: str
    summary_zh: str
    why_it_matters_zh: str
    author: str
    url: str
    created_at: str | None
    saved_at: str
    importance: int
    tickers: list[str]
    tags: list[str]
    dry_run: bool

    @classmethod
    def from_signal(cls, signal: Signal, post: Post, dry_run: bool) -> StoredSignal:
        author = post.author.display if post.author else post.author_id
        return cls(
            id=f"{post.id}:{int(dry_run)}",
            post_id=post.id,
            title=signal.title,
            summary_zh=signal.summary_zh,
            why_it_matters_zh=signal.why_it_matters_zh,
            author=author,
            url=post.url,
            created_at=post.created_at,
            saved_at=datetime.now(UTC).isoformat(),
            importance=signal.importance,
            tickers=signal.tickers,
            tags=signal.tags,
            dry_run=dry_run,
        )


class SignalHistoryStore:
    def __init__(self, path: Path, limit: int = 200) -> None:
        self.path = path
        self.limit = limit

    def load(self) -> list[StoredSignal]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [StoredSignal(**item) for item in payload if isinstance(item, dict)]

    def add_many(self, signals: list[StoredSignal]) -> None:
        if not signals:
            return
        existing = self.load()
        merged_by_id = {item.id: item for item in existing}
        for signal in signals:
            merged_by_id[signal.id] = signal
        merged = sorted(
            merged_by_id.values(),
            key=lambda item: item.saved_at,
            reverse=True,
        )[: self.limit]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([asdict(item) for item in merged], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
