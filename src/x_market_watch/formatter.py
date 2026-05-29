from __future__ import annotations

from datetime import datetime
from html import escape

from x_market_watch.models import Post, Signal


def format_signal(signal: Signal, post: Post) -> str:
    author = post.author.display if post.author else post.author_id
    tags = " ".join(f"#{escape(tag)}" for tag in signal.tags)
    tickers = " ".join(escape(ticker) for ticker in signal.tickers)
    return "\n".join(
        part
        for part in [
            f"<b>{escape(signal.title)}</b>",
            f"时间：{_format_date(post.created_at)}" if post.created_at else "",
            f"来源：{escape(author)}",
            f"相关股票代码：{tickers}" if tickers else "相关股票代码：无",
            f"内容：{escape(signal.summary_zh)}",
            f"为什么重要：{escape(signal.why_it_matters_zh)}",
            tags,
            escape(post.url),
        ]
        if part
    )


def _format_date(created_at: str | None) -> str:
    if not created_at:
        return ""
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return created_at
    return parsed.strftime("%Y/%m/%d %H:%M")
