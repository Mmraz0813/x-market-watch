from __future__ import annotations

import argparse
import json
import logging
import time

from dotenv import load_dotenv

from x_market_watch.pipeline import Pipeline, _build_oauth1_signer
from x_market_watch.settings import Settings
from x_market_watch.web import run_web_server
from x_market_watch.x_client import XClient


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="x-market-watch")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run-once", "daemon", "debug-x", "web"],
        default="run-once",
        help="Run once, keep polling forever, inspect X, or start the web console.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze but do not send Telegram messages.",
    )
    args = parser.parse_args()

    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    if args.command == "debug-x":
        debug_x(settings)
        return

    if args.command == "web":
        run_web_server(settings, host=settings.web_host, port=settings.web_port)
        return

    pipeline = Pipeline(settings)
    try:
        if args.command == "run-once":
            pipeline.run_once(dry_run=args.dry_run)
            return

        while True:
            try:
                pipeline.run_once(dry_run=args.dry_run)
            except Exception:
                logging.exception("Pipeline run failed")
            time.sleep(settings.poll_interval_seconds)
    finally:
        pipeline.close()


def debug_x(settings: Settings) -> None:
    client = XClient(
        str(settings.x_bearer_token),
        str(settings.x_api_base),
        oauth1_signer=_build_oauth1_signer(settings),
    )
    try:
        payload = client.fetch_raw_list_page(settings.x_list_id, settings.x_max_results)
    finally:
        client.close()

    summary = {
        "meta": payload.get("meta"),
        "errors": payload.get("errors"),
        "data_count": len(payload.get("data", [])),
        "includes_users_count": len(payload.get("includes", {}).get("users", [])),
        "first_posts": [
            {
                "id": item.get("id"),
                "author_id": item.get("author_id"),
                "created_at": item.get("created_at"),
                "text_preview": str(item.get("text", ""))[:160],
            }
            for item in payload.get("data", [])[:5]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
