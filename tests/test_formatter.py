from x_market_watch.formatter import format_signal
from x_market_watch.models import Author, Post, Signal


def test_format_signal_escapes_html() -> None:
    post = Post(
        id="1",
        text="<market>",
        author_id="42",
        created_at="2026-01-01T00:00:00Z",
        author=Author(id="42", username="alice", name="Alice <A>"),
        metrics={"like_count": 10, "retweet_count": 2},
    )
    signal = Signal(
        post_id="1",
        keep=True,
        importance=4,
        title="AI <breakout>",
        summary_zh="可能影响 <NVDA>",
        why_it_matters_zh="资金会重新定价。",
        tickers=["$NVDA", "$AMD"],
        tags=["AI", "美股"],
    )

    message = format_signal(signal, post)

    assert "&lt;breakout&gt;" in message
    assert "Alice &lt;A&gt;" in message
    assert "时间：2026/01/01" in message
    assert "相关股票代码：$NVDA $AMD" in message
    assert "重要性" not in message


def test_format_signal_shows_no_tickers() -> None:
    post = Post(
        id="2",
        text="market note",
        author_id="42",
        created_at=None,
        author=None,
        metrics={},
    )
    signal = Signal(
        post_id="2",
        keep=True,
        importance=3,
        title="宏观观察",
        summary_zh="市场等待数据。",
        why_it_matters_zh="影响风险偏好。",
        tickers=[],
        tags=[],
    )

    message = format_signal(signal, post)

    assert "相关股票代码：无" in message
