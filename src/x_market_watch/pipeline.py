from __future__ import annotations

import logging

from x_market_watch.formatter import format_signal
from x_market_watch.llm import LLMAnalyzer
from x_market_watch.models import Post
from x_market_watch.oauth1 import OAuth1Signer
from x_market_watch.settings import Settings
from x_market_watch.state import StateStore
from x_market_watch.telegram import TelegramClient
from x_market_watch.x_client import XClient

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_store = StateStore(settings.state_path)
        self.x_client = XClient(
            str(settings.x_bearer_token),
            str(settings.x_api_base),
            oauth1_signer=_build_oauth1_signer(settings),
        )
        self.analyzer = LLMAnalyzer(
            api_key=settings.llm_api_key,
            api_base=str(settings.llm_api_base),
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        self.telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)

    def close(self) -> None:
        self.x_client.close()
        self.analyzer.close()
        self.telegram.close()

    def run_once(self, dry_run: bool = False) -> int:
        state = self.state_store.load()
        posts = self.x_client.fetch_list_posts(
            list_id=self.settings.x_list_id,
            max_results=self.settings.x_max_results,
            max_pages=self.settings.x_max_pages,
            stop_after_id=state.last_seen_id,
        )
        if state.last_seen_id:
            posts = [post for post in posts if int(post.id) > int(state.last_seen_id)]
        posts = self._sort_newest_payload_chronologically(posts)
        if not posts:
            logger.info("No new posts found")
            return 0

        signals = self.analyzer.analyze(posts)
        posts_by_id = {post.id: post for post in posts}
        kept = [
            signal
            for signal in signals
            if signal.keep and signal.importance >= self.settings.llm_min_importance
        ]

        sent_count = 0
        for signal in kept:
            post = posts_by_id.get(signal.post_id)
            if post is None:
                continue
            message = format_signal(signal, post)
            if dry_run:
                logger.info("Dry run message:\n%s", message)
            else:
                self.telegram.send_message(message)
            sent_count += 1

        state.last_seen_id = max(posts, key=lambda item: int(item.id)).id
        if not dry_run:
            self.state_store.save(state)
        logger.info("Processed %s posts, sent %s Telegram messages", len(posts), sent_count)
        return sent_count

    @staticmethod
    def _sort_newest_payload_chronologically(posts: list[Post]) -> list[Post]:
        return sorted(posts, key=lambda item: int(item.id))


def _build_oauth1_signer(settings: Settings) -> OAuth1Signer | None:
    if settings.x_auth_mode.lower() != "oauth1":
        return None

    required = {
        "X_API_KEY": settings.x_api_key,
        "X_API_KEY_SECRET": settings.x_api_key_secret,
        "X_ACCESS_TOKEN": settings.x_access_token,
        "X_ACCESS_TOKEN_SECRET": settings.x_access_token_secret,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing OAuth 1.0a settings: {', '.join(missing)}")

    return OAuth1Signer(
        api_key=settings.x_api_key or "",
        api_key_secret=settings.x_api_key_secret or "",
        access_token=settings.x_access_token or "",
        access_token_secret=settings.x_access_token_secret or "",
    )
