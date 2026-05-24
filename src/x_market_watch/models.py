from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Author:
    id: str
    username: str
    name: str | None = None
    verified: bool | None = None

    @property
    def display(self) -> str:
        if self.name and self.username:
            return f"{self.name} (@{self.username})"
        if self.username:
            return f"@{self.username}"
        return self.id


@dataclass(frozen=True)
class Post:
    id: str
    text: str
    author_id: str
    created_at: str | None
    author: Author | None
    metrics: dict[str, int]

    @property
    def url(self) -> str:
        username = self.author.username if self.author and self.author.username else "i"
        return f"https://x.com/{username}/status/{self.id}"


@dataclass(frozen=True)
class Signal:
    post_id: str
    keep: bool
    importance: int
    title: str
    summary_zh: str
    why_it_matters_zh: str
    tickers: list[str]
    tags: list[str]
