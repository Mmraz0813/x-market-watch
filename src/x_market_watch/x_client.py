from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from x_market_watch.models import Author, Post
from x_market_watch.oauth1 import OAuth1Signer

logger = logging.getLogger(__name__)


class XClient:
    def __init__(
        self,
        bearer_token: str,
        api_base: str,
        oauth1_signer: OAuth1Signer | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._oauth1_signer = oauth1_signer
        self._client = httpx.Client(
            base_url=self._api_base,
            headers={} if oauth1_signer else {"Authorization": f"Bearer {bearer_token}"},
            timeout=30,
        )

    def close(self) -> None:
        self._client.close()

    def fetch_list_posts(
        self,
        list_id: str,
        max_results: int,
        max_pages: int = 1,
        stop_after_id: str | None = None,
    ) -> list[Post]:
        posts: list[Post] = []
        pagination_token: str | None = None

        for _page in range(max_pages):
            params: dict[str, str | int] = {
                "max_results": max_results,
                "tweet.fields": "created_at,author_id,public_metrics,lang",
                "expansions": "author_id",
                "user.fields": "name,username,verified",
            }
            if pagination_token:
                params["pagination_token"] = pagination_token

            payload = self._request_list_page(list_id, params)
            authors = self._parse_authors(payload.get("includes", {}).get("users", []))
            page_posts = [
                self._parse_post(item, authors)
                for item in payload.get("data", [])
                if item.get("id") and item.get("text")
            ]
            posts.extend(page_posts)

            if stop_after_id and any(int(post.id) <= int(stop_after_id) for post in page_posts):
                break

            pagination_token = payload.get("meta", {}).get("next_token")
            if not pagination_token:
                break

        logger.info("Fetched %s posts from X list %s", len(posts), list_id)
        return posts

    def _request_list_page(
        self,
        list_id: str,
        params: dict[str, str | int],
    ) -> dict[str, object]:
        response = self._get(f"/lists/{list_id}/tweets", params)
        if response.status_code == 401:
            raise RuntimeError(
                "X API authentication failed. For private lists, make sure the token has user "
                "context/OAuth2 permissions for the account that can read this list."
            )
        if response.status_code == 403:
            raise RuntimeError(
                "X API permission denied. Check your app access level, list ownership, and whether "
                "your private list requires a user-context token."
            )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors")
        if errors and not payload.get("data"):
            details = "; ".join(
                str(error.get("detail") or error.get("title") or error)
                for error in errors
                if isinstance(error, Mapping)
            )
            raise RuntimeError(f"X API returned no data: {details}")
        return payload

    def fetch_raw_list_page(self, list_id: str, max_results: int) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "max_results": max_results,
            "tweet.fields": "created_at,author_id,public_metrics,lang",
            "expansions": "author_id",
            "user.fields": "name,username,verified",
        }
        response = self._get(f"/lists/{list_id}/tweets", params)
        response.raise_for_status()
        return response.json()

    def _get(self, path: str, params: dict[str, str | int]) -> httpx.Response:
        headers = {}
        if self._oauth1_signer:
            headers["Authorization"] = self._oauth1_signer.authorization_header(
                "GET",
                f"{self._api_base}{path}",
                params,
            )
        return self._client.get(path, params=params, headers=headers)

    @staticmethod
    def _parse_authors(users: list[Mapping[str, object]]) -> dict[str, Author]:
        authors: dict[str, Author] = {}
        for user in users:
            user_id = str(user.get("id", ""))
            if not user_id:
                continue
            authors[user_id] = Author(
                id=user_id,
                username=str(user.get("username", "")),
                name=str(user["name"]) if user.get("name") else None,
                verified=bool(user["verified"]) if "verified" in user else None,
            )
        return authors

    @staticmethod
    def _parse_post(item: Mapping[str, object], authors: dict[str, Author]) -> Post:
        author_id = str(item.get("author_id", ""))
        metrics = item.get("public_metrics") or {}
        if not isinstance(metrics, Mapping):
            metrics = {}
        safe_metrics = {
            str(key): int(value)
            for key, value in metrics.items()
            if isinstance(value, int)
        }
        return Post(
            id=str(item["id"]),
            text=str(item["text"]),
            author_id=author_id,
            created_at=str(item["created_at"]) if item.get("created_at") else None,
            author=authors.get(author_id),
            metrics=safe_metrics,
        )
