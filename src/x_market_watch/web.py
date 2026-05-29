from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from x_market_watch.history import SignalHistoryStore
from x_market_watch.pipeline import Pipeline, PipelineCancelled
from x_market_watch.settings import Settings
from x_market_watch.state import StateStore

logger = logging.getLogger(__name__)

ENV_PATH = Path(os.environ.get("X_MARKET_WATCH_ENV_PATH", ".env"))
SECRET_ENV_KEYS = {
    "X_BEARER_TOKEN",
    "X_API_KEY",
    "X_API_KEY_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
    "LLM_API_KEY",
    "TELEGRAM_BOT_TOKEN",
}
EDITABLE_ENV_FIELDS = [
    "X_AUTH_MODE",
    "X_BEARER_TOKEN",
    "X_API_KEY",
    "X_API_KEY_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
    "X_LIST_ID",
    "X_API_BASE",
    "X_MAX_RESULTS",
    "X_MAX_PAGES",
    "LLM_API_KEY",
    "LLM_API_BASE",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MIN_IMPORTANCE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "POLL_INTERVAL_SECONDS",
    "STATE_PATH",
    "SIGNAL_HISTORY_PATH",
    "SIGNAL_HISTORY_LIMIT",
    "LOG_LEVEL",
    "WEB_HOST",
    "WEB_PORT",
    "WEB_AUTO_POLL",
]


@dataclass
class JobState:
    running: bool = False
    dry_run: bool = True
    status: str = "idle"
    stop_requested: bool = False
    sent_count: int | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[str] = field(default_factory=list)


class WebController:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._job = JobState()
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    def status_payload(self) -> dict[str, Any]:
        settings = self.settings
        state_store = StateStore(settings.state_path)
        state = state_store.load()
        state_path = settings.state_path
        state_mtime = _path_mtime(state_path)
        with self._lock:
            job = self._serialize_job_locked()

        return {
            "app": {
                "name": "X Market Watch",
                "now": _now_iso(),
            },
            "settings": {
                "x_auth_mode": settings.x_auth_mode,
                "x_list_id": settings.x_list_id,
                "x_api_base": str(settings.x_api_base),
                "x_max_results": settings.x_max_results,
                "x_max_pages": settings.x_max_pages,
                "llm_api_base": str(settings.llm_api_base),
                "llm_model": settings.llm_model,
                "llm_min_importance": settings.llm_min_importance,
                "telegram_chat_id": _mask(settings.telegram_chat_id),
                "poll_interval_seconds": settings.poll_interval_seconds,
                "web_auto_poll": settings.web_auto_poll,
                "state_path": str(state_path),
            },
            "checks": {
                "x_credentials": _x_credentials_ready(settings),
                "llm_api_key": _looks_configured(settings.llm_api_key),
                "telegram": _looks_configured(settings.telegram_bot_token)
                and _looks_configured(settings.telegram_chat_id),
            },
            "state": {
                "last_seen_id": state.last_seen_id,
                "state_file_exists": state_path.exists(),
                "updated_at": state_mtime,
            },
            "job": job,
        }

    def signals_payload(self) -> dict[str, Any]:
        store = SignalHistoryStore(
            self.settings.signal_history_path,
            limit=self.settings.signal_history_limit,
        )
        return {"signals": [signal.__dict__ for signal in store.load()]}

    def env_payload(self) -> dict[str, Any]:
        values = _read_env_values(ENV_PATH)
        return {
            "env_path": str(ENV_PATH),
            "fields": [
                {
                    "key": key,
                    "value": "" if key in SECRET_ENV_KEYS else self._effective_env_value(key),
                    "masked": _mask(values.get(key, "")) if key in SECRET_ENV_KEYS else "",
                    "configured": _looks_configured(
                        values.get(key)
                        if key in SECRET_ENV_KEYS
                        else self._effective_env_value(key)
                    ),
                    "secret": key in SECRET_ENV_KEYS,
                }
                for key in EDITABLE_ENV_FIELDS
            ],
        }

    def save_env(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._job.running:
                raise RuntimeError("任务运行中，先等本轮结束再修改配置。")

        normalized: dict[str, str] = {}
        for key in EDITABLE_ENV_FIELDS:
            if key not in updates:
                continue
            value = str(updates.get(key, "")).strip()
            if not value:
                continue
            normalized[key] = value

        previous = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else None
        _update_env_file(ENV_PATH, normalized)
        try:
            self.settings = Settings()
        except Exception:
            if previous is None:
                ENV_PATH.unlink(missing_ok=True)
            else:
                ENV_PATH.write_text(previous, encoding="utf-8")
            raise
        if self.settings.web_auto_poll:
            self.start_scheduler()
        else:
            self.stop_scheduler()
        return self.env_payload()

    def start_scheduler(self) -> None:
        if not self.settings.web_auto_poll:
            logger.info("Web auto polling is disabled")
            return
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._scheduler_stop.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=5)

    def _scheduler_loop(self) -> None:
        logger.info(
            "Web auto polling enabled; interval=%s seconds",
            self.settings.poll_interval_seconds,
        )
        while not self._scheduler_stop.is_set():
            if not self.settings.web_auto_poll:
                logger.info("Web auto polling is disabled")
            elif _runtime_ready(self.settings):
                started, _job = self.start_run(dry_run=False, source="auto")
                if started:
                    self._wait_for_current_job()
            else:
                logger.info("Web auto polling is waiting for required settings")

            interval = max(1, int(self.settings.poll_interval_seconds))
            self._scheduler_stop.wait(interval)

    def _wait_for_current_job(self) -> None:
        while not self._scheduler_stop.wait(1):
            with self._lock:
                if not self._job.running:
                    return

    def _effective_env_value(self, key: str) -> str:
        attr_name = key.lower()
        if not hasattr(self.settings, attr_name):
            return ""
        value = getattr(self.settings, attr_name)
        return str(value) if value is not None else ""

    def job_payload(self) -> dict[str, Any]:
        with self._lock:
            return self._serialize_job_locked()

    def request_stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._job.running:
                return self._serialize_job_locked()
            self._job.stop_requested = True
            self._job.status = "stopping"
            self._job.logs.append("Stop requested. Waiting for the current safe checkpoint.")
            return self._serialize_job_locked()

    def start_run(self, dry_run: bool, source: str = "manual") -> tuple[bool, dict[str, Any]]:
        with self._lock:
            if self._job.running:
                return False, self._serialize_job_locked()
            self._job = JobState(
                running=True,
                dry_run=dry_run,
                status="running",
                started_at=_now_iso(),
                logs=[f"Started {'dry run' if dry_run else 'live run'} ({source})."],
            )

        thread = threading.Thread(target=self._run_pipeline, args=(dry_run,), daemon=True)
        thread.start()
        with self._lock:
            return True, self._serialize_job_locked()

    def _run_pipeline(self, dry_run: bool) -> None:
        handler = _MemoryLogHandler(self)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(message)s"))
        app_logger = logging.getLogger("x_market_watch")
        app_logger.addHandler(handler)
        previous_level = app_logger.level
        if previous_level > logging.INFO or previous_level == logging.NOTSET:
            app_logger.setLevel(logging.INFO)

        pipeline = Pipeline(self.settings)
        try:
            sent_count = pipeline.run_once(dry_run=dry_run, cancel_check=self._stop_requested)
        except PipelineCancelled as exc:
            self.add_log(str(exc))
            with self._lock:
                self._job.running = False
                self._job.status = "stopped"
                self._job.finished_at = _now_iso()
        except Exception as exc:  # noqa: BLE001 - surface the real pipeline error in the UI.
            logger.exception("Web-triggered pipeline run failed")
            self.add_log(f"ERROR - {exc}")
            with self._lock:
                self._job.running = False
                self._job.status = "failed"
                self._job.error = str(exc)
                self._job.finished_at = _now_iso()
        else:
            with self._lock:
                self._job.running = False
                self._job.status = "completed"
                self._job.sent_count = sent_count
                self._job.finished_at = _now_iso()
                self._job.logs.append(f"Completed. Sent {sent_count} Telegram message(s).")
        finally:
            pipeline.close()
            app_logger.removeHandler(handler)
            app_logger.setLevel(previous_level)

    def add_log(self, message: str) -> None:
        with self._lock:
            self._job.logs.append(message)
            self._job.logs = self._job.logs[-80:]

    def _stop_requested(self) -> bool:
        with self._lock:
            return self._job.stop_requested

    def _serialize_job_locked(self) -> dict[str, Any]:
        return {
            "running": self._job.running,
            "dry_run": self._job.dry_run,
            "status": self._job.status,
            "stop_requested": self._job.stop_requested,
            "sent_count": self._job.sent_count,
            "error": self._job.error,
            "started_at": self._job.started_at,
            "finished_at": self._job.finished_at,
            "logs": list(self._job.logs),
        }


class _MemoryLogHandler(logging.Handler):
    def __init__(self, controller: WebController) -> None:
        super().__init__()
        self.controller = controller

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.controller.add_log(self.format(record))
        except Exception:
            self.handleError(record)


def run_web_server(settings: Settings, host: str = "127.0.0.1", port: int = 8787) -> None:
    controller = WebController(settings)

    class RequestHandler(_WebRequestHandler):
        pass

    RequestHandler.controller = controller
    server = ThreadingHTTPServer((host, port), RequestHandler)
    logger.info("Web console listening on http://%s:%s", host, port)
    controller.start_scheduler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Web console stopped")
    finally:
        controller.stop_scheduler()
        server.server_close()


class _WebRequestHandler(BaseHTTPRequestHandler):
    controller: WebController

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler.
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._send_json(self.controller.status_payload())
            return
        if path == "/api/job":
            self._send_json(self.controller.job_payload())
            return
        if path == "/api/signals":
            self._send_json(self.controller.signals_payload())
            return
        if path == "/api/env":
            self._send_json(self.controller.env_payload())
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler.
        path = urlparse(self.path).path
        if path == "/api/env":
            try:
                payload = self.controller.save_env(self._read_json())
            except Exception as exc:  # noqa: BLE001 - return configuration errors to the UI.
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            else:
                self._send_json(payload)
            return

        if path == "/api/run":
            payload = self._read_json()
            dry_run = bool(payload.get("dry_run", True))
            started, job = self.controller.start_run(dry_run=dry_run)
            status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
            self._send_json({"started": started, "job": job}, status=status)
            return

        if path == "/api/stop":
            self._send_json({"job": self.controller.request_stop()})
            return

        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("HTTP %s", format % args)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, content: str, content_type: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)


def _path_mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "***" if value else ""
    return f"{value[:3]}***{value[-3:]}"


def _looks_configured(value: str | None) -> bool:
    return bool(value and value.strip() and value.strip() != "replace_me")


def _x_credentials_ready(settings: Settings) -> bool:
    if settings.x_auth_mode.lower() == "oauth1":
        return all(
            _looks_configured(value)
            for value in [
                settings.x_api_key,
                settings.x_api_key_secret,
                settings.x_access_token,
                settings.x_access_token_secret,
            ]
        )
    return _looks_configured(settings.x_bearer_token)


def _runtime_ready(settings: Settings) -> bool:
    return (
        _x_credentials_ready(settings)
        and _looks_configured(settings.x_list_id)
        and settings.x_list_id != "1234567890"
        and _looks_configured(settings.llm_api_key)
        and _looks_configured(settings.telegram_bot_token)
        and _looks_configured(settings.telegram_chat_id)
    )


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote_env_value(value.strip())
    return values


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            output.append(raw_line)
            continue
        key = raw_line.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={_format_env_value(remaining.pop(key))}")
        else:
            output.append(raw_line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        for key in EDITABLE_ENV_FIELDS:
            if key in remaining:
                output.append(f"{key}={_format_env_value(remaining[key])}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _format_env_value(value: str) -> str:
    cleaned = value.replace("\n", "").replace("\r", "")
    if any(char.isspace() for char in cleaned) or "#" in cleaned:
        return json.dumps(cleaned, ensure_ascii=False)
    return cleaned


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>X Market Watch</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f5f7;
      --panel: rgba(255, 255, 255, 0.82);
      --panel-strong: #ffffff;
      --text: #1d1d1f;
      --muted: #6e6e73;
      --line: rgba(0, 0, 0, 0.10);
      --blue: #0071e3;
      --green: #30d158;
      --orange: #ff9f0a;
      --red: #ff453a;
      --shadow: 0 18px 60px rgba(0, 0, 0, 0.10);
      --radius: 8px;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.90), rgba(245, 245, 247, 0.96)),
        var(--bg);
      color: var(--text);
    }

    button,
    input {
      font: inherit;
    }

    select,
    textarea {
      font: inherit;
    }

    .shell {
      display: grid;
      grid-template-columns: 268px minmax(0, 1fr);
      min-height: 100vh;
    }

    .sidebar {
      padding: 28px 18px;
      border-right: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.64);
      backdrop-filter: blur(22px);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 8px 26px;
    }

    .mark {
      width: 42px;
      height: 42px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      color: white;
      background: linear-gradient(135deg, #1d1d1f, #3a3a3c 42%, #0071e3);
      box-shadow: 0 12px 24px rgba(0, 113, 227, 0.22);
    }

    .brand h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.15;
      letter-spacing: 0;
    }

    .brand p {
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .nav {
      display: grid;
      gap: 6px;
    }

    .nav-item {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 40px;
      padding: 0 10px;
      color: #424245;
      border-radius: 8px;
      font-size: 14px;
      cursor: pointer;
    }

    .nav-item.active {
      background: rgba(0, 113, 227, 0.10);
      color: var(--blue);
    }

    .main {
      padding: 28px clamp(18px, 4vw, 48px) 40px;
    }

    .view {
      display: none;
    }

    .view.active {
      display: grid;
    }

    .signal-list {
      overflow: auto;
      display: grid;
      gap: 10px;
      padding: 16px 18px 18px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      max-height: 360px;
    }

    .signal-bubble {
      border: 1px solid rgba(0, 0, 0, 0.08);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.72);
      padding: 10px 11px;
      cursor: pointer;
    }

    .signal-bubble.active {
      border-color: rgba(0, 113, 227, 0.45);
      background: rgba(0, 113, 227, 0.08);
    }

    .signal-bubble strong {
      display: block;
      font-size: 13px;
      line-height: 1.35;
      margin-bottom: 6px;
    }

    .signal-bubble p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .signal-meta {
      color: var(--muted);
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      font-size: 11px;
      margin-top: 8px;
    }

    .topbar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 24px;
    }

    .eyebrow {
      color: var(--muted);
      font-size: 13px;
      margin: 0 0 8px;
    }

    h2 {
      margin: 0;
      font-size: clamp(30px, 4vw, 52px);
      line-height: 1.02;
      letter-spacing: 0;
    }

    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .button {
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 40px;
      padding: 0 14px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--text);
      background: rgba(255, 255, 255, 0.76);
      cursor: pointer;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
    }

    .button.primary {
      background: var(--blue);
      color: white;
      border-color: var(--blue);
    }

    .button.danger {
      background: rgba(255, 69, 58, 0.10);
      color: #b42318;
      border-color: rgba(255, 69, 58, 0.26);
    }

    .button:disabled {
      opacity: 0.52;
      cursor: not-allowed;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 14px;
    }

    .view:not(.active) {
      display: none;
    }

    .panel {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      overflow: hidden;
    }

    .panel-header {
      min-height: 52px;
      padding: 16px 18px 10px;
      border-bottom: 1px solid rgba(0, 0, 0, 0.06);
    }

    .panel-header h3 {
      margin: 0;
      font-size: 16px;
      letter-spacing: 0;
    }

    .panel-header p {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 12px;
    }

    .span-8 {
      grid-column: span 8;
    }

    .span-4 {
      grid-column: span 4;
    }

    .span-12 {
      grid-column: span 12;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      border-bottom: 1px solid rgba(0, 0, 0, 0.06);
    }

    .metric {
      padding: 18px;
      min-height: 104px;
      border-right: 1px solid rgba(0, 0, 0, 0.06);
    }

    .metric:last-child {
      border-right: 0;
    }

    .metric label,
    .field label {
      color: var(--muted);
      display: block;
      font-size: 12px;
      margin-bottom: 8px;
    }

    .metric strong {
      display: block;
      font-size: 30px;
      line-height: 1;
      letter-spacing: 0;
      word-break: break-word;
    }

    .metric span,
    .field span {
      color: #424245;
      font-size: 13px;
      line-height: 1.35;
    }

    .fields {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1px;
      background: rgba(0, 0, 0, 0.06);
    }

    .field {
      background: rgba(255, 255, 255, 0.62);
      padding: 16px 18px;
      min-height: 78px;
    }

    .field span {
      display: block;
      word-break: break-word;
    }

    .checklist {
      padding: 8px 0;
    }

    .check {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 54px;
      padding: 0 18px;
      border-bottom: 1px solid rgba(0, 0, 0, 0.06);
    }

    .check:last-child {
      border-bottom: 0;
    }

    .pill {
      min-width: 58px;
      text-align: center;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      color: #0f5132;
      background: rgba(48, 209, 88, 0.18);
    }

    .pill.warn {
      color: #7a3f00;
      background: rgba(255, 159, 10, 0.18);
    }

    .pill.fail {
      color: #8a1f17;
      background: rgba(255, 69, 58, 0.16);
    }

    .console {
      height: 318px;
      overflow: auto;
      padding: 16px 18px;
      background: #171719;
      color: #f5f5f7;
      font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.65;
      white-space: pre-wrap;
    }

    .console.tall {
      height: 520px;
    }

    .signal-detail {
      padding: 18px;
      display: grid;
      gap: 14px;
    }

    .message {
      max-width: 860px;
      border-radius: 8px;
      padding: 14px 16px;
      line-height: 1.65;
      background: var(--panel-strong);
      border: 1px solid rgba(0, 0, 0, 0.08);
    }

    .message.ai {
      background: rgba(0, 113, 227, 0.08);
      border-color: rgba(0, 113, 227, 0.18);
    }

    .message h4 {
      margin: 0 0 8px;
      font-size: 16px;
    }

    .message p {
      margin: 0;
      color: #333336;
      font-size: 14px;
    }

    .tag-row {
      display: flex;
      gap: 7px;
      flex-wrap: wrap;
    }

    .chip {
      border-radius: 999px;
      background: rgba(0, 0, 0, 0.06);
      color: #424245;
      padding: 5px 9px;
      font-size: 12px;
    }

    .settings-form {
      padding: 18px;
      display: grid;
      gap: 18px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }

    .form-field {
      display: grid;
      gap: 7px;
    }

    .form-field label {
      color: var(--muted);
      font-size: 12px;
    }

    .form-field input,
    .form-field select {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.82);
      color: var(--text);
      padding: 0 11px;
      outline: none;
    }

    .form-field small {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.4;
    }

    .form-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }

    .empty {
      color: #a1a1a6;
    }

    .status-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      display: inline-block;
      margin-right: 7px;
      background: var(--green);
      box-shadow: 0 0 0 4px rgba(48, 209, 88, 0.16);
    }

    .status-dot.running {
      background: var(--orange);
      box-shadow: 0 0 0 4px rgba(255, 159, 10, 0.16);
    }

    .status-dot.failed {
      background: var(--red);
      box-shadow: 0 0 0 4px rgba(255, 69, 58, 0.15);
    }

    svg {
      width: 17px;
      height: 17px;
      flex: 0 0 auto;
    }

    @media (max-width: 860px) {
      .shell {
        grid-template-columns: 1fr;
      }

      .sidebar {
        border-right: 0;
        border-bottom: 1px solid var(--line);
        padding: 18px;
      }

      .nav {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }

      .topbar {
        display: grid;
      }

      .toolbar {
        justify-content: stretch;
      }

      .button {
        flex: 1;
        justify-content: center;
      }

      .span-8,
      .span-4 {
        grid-column: span 12;
      }

      .metrics,
      .fields,
      .signal-list,
      .form-grid {
        grid-template-columns: 1fr;
      }

      .metric {
        border-right: 0;
        border-bottom: 1px solid rgba(0, 0, 0, 0.06);
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M4 16.5 9 11l4 3.5 7-8" stroke="currentColor" stroke-width="2.2"
              stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M5 20h14" stroke="currentColor" stroke-width="2.2"
              stroke-linecap="round"/>
          </svg>
        </div>
        <div>
          <h1>X Market Watch</h1>
          <p>私人市场信号台</p>
        </div>
      </div>
      <nav class="nav" aria-label="主导航">
        <div class="nav-item active" data-view="dashboardView">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M4 13h6V4H4v9Zm10 7h6V4h-6v16ZM4 20h6v-3H4v3Z"
              stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
          </svg>
          仪表盘
        </div>
        <div class="nav-item" data-view="logsView">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M6 7h12M6 12h12M6 17h7" stroke="currentColor"
              stroke-width="2" stroke-linecap="round"/>
          </svg>
          运行日志
        </div>
        <div class="nav-item" data-view="settingsView">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M12 3v3m0 12v3M4.9 4.9 7 7m10 10 2.1 2.1M3 12h3"
              stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            <path d="M18 12h3M4.9 19.1 7 17m10-10 2.1-2.1"
              stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          </svg>
          设置
        </div>
      </nav>
    </aside>

    <main class="main">
      <div class="topbar">
        <div>
          <p class="eyebrow" id="now">同步中</p>
          <h2>让 X 列表自动变成可读的投资信号。</h2>
        </div>
        <div class="toolbar">
          <button class="button" id="refreshBtn" title="刷新状态">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M20 11a8 8 0 0 0-14.4-4.8L4 8m0 0V3m0 5h5"
                stroke="currentColor" stroke-width="2" stroke-linecap="round"
                stroke-linejoin="round"/>
              <path d="M4 13a8 8 0 0 0 14.4 4.8L20 16m0 0v5m0-5h-5"
                stroke="currentColor" stroke-width="2" stroke-linecap="round"
                stroke-linejoin="round"/>
            </svg>
            刷新
          </button>
          <button class="button" id="dryRunBtn" title="只分析，不推送 Telegram">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="m8 5 11 7-11 7V5Z" stroke="currentColor" stroke-width="2"
                stroke-linejoin="round"/>
            </svg>
            Dry run
          </button>
          <button class="button primary" id="liveRunBtn" title="真实运行并推送 Telegram">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="m21 3-6.5 18-3.7-7.8L3 9.5 21 3Z" stroke="currentColor"
                stroke-width="2" stroke-linejoin="round"/>
            </svg>
            正式运行
          </button>
          <button class="button danger" id="stopRunBtn" title="停止当前任务">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M8 8h8v8H8V8Z" stroke="currentColor" stroke-width="2"
                stroke-linejoin="round"/>
            </svg>
            停止
          </button>
        </div>
      </div>

      <section class="grid view active" id="dashboardView" aria-label="控制台">
        <div class="panel span-8">
          <div class="panel-header">
            <h3><span class="status-dot" id="jobDot"></span><span id="jobTitle">待命</span></h3>
            <p id="jobSub">可以先用 dry-run 验证 X、LLM 和摘要逻辑。</p>
          </div>
          <div class="metrics">
            <div class="metric">
              <label>最近推送</label>
              <strong id="sentCount">-</strong>
              <span>当前任务发送到 Telegram 的消息数</span>
            </div>
            <div class="metric">
              <label>Last seen ID</label>
              <strong id="lastSeen">-</strong>
              <span>用于避免重复处理的 X 推文编号</span>
            </div>
            <div class="metric">
              <label>轮询间隔</label>
              <strong id="pollInterval">-</strong>
              <span>daemon 模式使用这个节奏自动检查</span>
            </div>
          </div>
          <div class="fields">
            <div class="field">
              <label>X 认证模式</label>
              <span id="authMode">-</span>
            </div>
            <div class="field">
              <label>X List ID</label>
              <span id="listId">-</span>
            </div>
            <div class="field">
              <label>模型</label>
              <span id="model">-</span>
            </div>
            <div class="field">
              <label>重要性阈值</label>
              <span id="importance">-</span>
            </div>
          </div>
        </div>

        <div class="panel span-4">
          <div class="panel-header">
            <h3>配置检查</h3>
            <p>这里只显示是否已配置，不展示密钥。</p>
          </div>
          <div class="checklist">
            <div class="check">
              <span>X 凭证</span>
              <span class="pill" id="xCheck">-</span>
            </div>
            <div class="check">
              <span>LLM API Key</span>
              <span class="pill" id="llmCheck">-</span>
            </div>
            <div class="check">
              <span>Telegram</span>
              <span class="pill" id="tgCheck">-</span>
            </div>
          </div>
        </div>

        <div class="panel span-12">
          <div class="panel-header">
            <h3>AI 信号流</h3>
            <p>这里展示 AI 整理和翻译后的关注推文。点击一条可以展开详情。</p>
          </div>
          <div class="signal-list" id="signalList">
            <div class="signal-bubble">
              <strong>暂无整理内容</strong>
              <p>运行一次 dry-run 或正式任务后，这里会出现 AI 翻译和整理的推文。</p>
            </div>
          </div>
        </div>
      </section>

      <section class="grid view" id="signalsView" aria-label="AI 信号详情">
        <div class="panel span-12">
          <div class="panel-header">
            <h3>AI 整理内容</h3>
            <p>左侧选择一条信号，这里会按聊天消息的形式展开翻译和分析。</p>
          </div>
          <div class="signal-detail" id="signalDetail"></div>
        </div>
      </section>

      <section class="grid view" id="logsView" aria-label="运行日志">
        <div class="panel span-12">
          <div class="panel-header">
            <h3>运行日志</h3>
            <p>这里显示最近一次网页触发任务的日志。</p>
          </div>
          <div class="console tall" id="consoleFull"><span class="empty">等待任务开始。</span></div>
        </div>
      </section>

      <section class="grid view" id="settingsView" aria-label="设置">
        <div class="panel span-12">
          <div class="panel-header">
            <h3>环境配置</h3>
            <p>修改 `.env` 后立即用于下一次运行。密钥不会明文回显，留空代表不改。</p>
          </div>
          <form class="settings-form" id="settingsForm">
            <div class="form-grid" id="settingsFields"></div>
            <div class="form-actions">
              <button class="button" type="button" id="reloadSettingsBtn">重新读取</button>
              <button class="button primary" type="submit">保存设置</button>
            </div>
          </form>
        </div>
      </section>
    </main>
  </div>

  <script>
    const state = { polling: null, signals: [], selectedSignalId: null };

    const $ = (id) => document.getElementById(id);

    function fmtDate(value) {
      if (!value) return "-";
      return new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short"
      }).format(new Date(value));
    }

    function yesNo(node, ok) {
      node.textContent = ok ? "已就绪" : "待配置";
      node.className = `pill ${ok ? "" : "warn"}`;
    }

    function compactId(value) {
      if (!value) return "-";
      return value.length > 8 ? `${value.slice(0, 3)}...${value.slice(-3)}` : value;
    }

    async function refresh() {
      const [statusResponse, signalsResponse] = await Promise.all([
        fetch("/api/status"),
        fetch("/api/signals")
      ]);
      const data = await statusResponse.json();
      const signalData = await signalsResponse.json();
      state.signals = signalData.signals || [];
      render(data);
      renderSignals();
    }

    async function startRun(dryRun) {
      setButtons(true);
      await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dry_run: dryRun })
      });
      await refresh();
    }

    async function stopRun() {
      $("stopRunBtn").disabled = true;
      await fetch("/api/stop", { method: "POST" });
      await refresh();
    }

    async function loadSettings() {
      const response = await fetch("/api/env");
      const data = await response.json();
      renderSettings(data.fields || []);
    }

    async function saveSettings(event) {
      event.preventDefault();
      const formData = new FormData($("settingsForm"));
      const payload = {};
      for (const [key, value] of formData.entries()) {
        payload[key] = value;
      }
      const response = await fetch("/api/env", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) {
        alert(data.error || "保存失败");
        return;
      }
      renderSettings(data.fields || []);
      await refresh();
    }

    function render(data) {
      $("now").textContent = `状态刷新于 ${fmtDate(data.app.now)}`;
      $("sentCount").textContent = data.job.sent_count ?? "-";
      $("lastSeen").textContent = compactId(data.state.last_seen_id);
      $("pollInterval").textContent = `${data.settings.poll_interval_seconds}s`;
      $("authMode").textContent = data.settings.x_auth_mode;
      $("listId").textContent = data.settings.x_list_id;
      $("model").textContent = data.settings.llm_model;
      $("importance").textContent = `${data.settings.llm_min_importance} / 5`;

      yesNo($("xCheck"), data.checks.x_credentials);
      yesNo($("llmCheck"), data.checks.llm_api_key);
      yesNo($("tgCheck"), data.checks.telegram);

      const dot = $("jobDot");
      const dotState = [
        "status-dot",
        data.job.running ? "running" : "",
        data.job.status === "failed" ? "failed" : ""
      ].filter(Boolean).join(" ");
      dot.className = dotState;
      $("jobTitle").textContent = titleForJob(data.job);
      $("jobSub").textContent = subtitleForJob(data.job);
      renderLogs(data.job.logs);
      setButtons(data.job.running);
    }

    function renderSignals() {
      const list = $("signalList");
      if (!state.signals.length) {
        list.innerHTML = `
          <div class="signal-bubble">
            <strong>暂无整理内容</strong>
            <p>运行一次 dry-run 或正式任务后，这里会出现 AI 翻译和整理的推文。</p>
          </div>`;
        renderSignalDetail(null);
        return;
      }

      if (!state.selectedSignalId) {
        state.selectedSignalId = state.signals[0].id;
      }
      list.innerHTML = state.signals.map((signal) => `
        <div class="signal-bubble ${signal.id === state.selectedSignalId ? "active" : ""}"
          data-signal-id="${escapeAttr(signal.id)}">
          <strong>${escapeHtml(signal.title)}</strong>
          <p>${escapeHtml(signal.summary_zh)}</p>
          <div class="signal-meta">
            <span>${escapeHtml(signal.author)}</span>
            <span>${signal.dry_run ? "dry-run" : "live"}</span>
          </div>
        </div>`).join("");

      list.querySelectorAll("[data-signal-id]").forEach((node) => {
        node.addEventListener("click", () => {
          state.selectedSignalId = node.dataset.signalId;
          renderSignals();
          showView("signalsView");
        });
      });
      renderSignalDetail(state.signals.find((signal) => signal.id === state.selectedSignalId));
    }

    function renderSignalDetail(signal) {
      const target = $("signalDetail");
      if (!target) return;
      if (!signal) {
        target.innerHTML = `
          <div class="message">
            <h4>等待第一条 AI 信号</h4>
            <p>点击 Dry run 或正式运行后，AI 整理翻译的内容会像聊天消息一样出现在这里。</p>
          </div>`;
        return;
      }
      const tags = [...(signal.tickers || []), ...(signal.tags || [])];
      target.innerHTML = `
        <div class="message">
          <h4>${escapeHtml(signal.author)}</h4>
          <p>${fmtDate(signal.created_at)} · 重要性 ${signal.importance} / 5</p>
        </div>
        <div class="message ai">
          <h4>${escapeHtml(signal.title)}</h4>
          <p>${escapeHtml(signal.summary_zh)}</p>
        </div>
        <div class="message">
          <h4>为什么重要</h4>
          <p>${escapeHtml(signal.why_it_matters_zh)}</p>
        </div>
        <div class="tag-row">
          ${tags.map((tag) => `<span class="chip">${escapeHtml(tag)}</span>`).join("")}
          <a class="chip" href="${escapeAttr(signal.url)}" target="_blank" rel="noreferrer">原文</a>
        </div>`;
    }

    function renderSettings(fields) {
      $("settingsFields").innerHTML = fields.map((field) => {
        const label = fieldLabel(field.key);
        const type = field.secret ? "password" : inputType(field.key);
        const value = field.secret ? "" : field.value;
        const placeholder = field.secret && field.configured ? field.masked : "";
        const help = field.secret
          ? "已配置时会加密显示。留空代表不修改，输入新值代表覆盖。"
          : fieldHelp(field.key);
        if (field.key === "X_AUTH_MODE") {
          return `
            <div class="form-field">
              <label>${label}</label>
              <select name="${field.key}">
                <option value="bearer" ${field.value === "bearer" ? "selected" : ""}>bearer</option>
                <option value="oauth1" ${field.value === "oauth1" ? "selected" : ""}>oauth1</option>
              </select>
              <small>${help}</small>
            </div>`;
        }
        return `
          <div class="form-field">
            <label>${label}</label>
            <input name="${field.key}" type="${type}" value="${escapeAttr(value)}"
              placeholder="${escapeAttr(placeholder)}" autocomplete="off" />
            <small>${help}</small>
          </div>`;
      }).join("");
    }

    function titleForJob(job) {
      if (job.status === "stopping") return "正在停止";
      if (job.running) return job.dry_run ? "Dry-run 运行中" : "正式运行中";
      if (job.status === "stopped") return "已停止";
      if (job.status === "failed") return "任务失败";
      if (job.status === "completed") return "任务完成";
      return "待命";
    }

    function subtitleForJob(job) {
      if (job.status === "stopping") return "已收到停止请求，正在等待安全停止点。";
      if (job.running) return `开始于 ${fmtDate(job.started_at)}`;
      if (job.status === "stopped") return `停止于 ${fmtDate(job.finished_at)}`;
      if (job.status === "failed") return job.error || "请查看日志定位错误。";
      if (job.status === "completed") return `完成于 ${fmtDate(job.finished_at)}`;
      return "可以先用 dry-run 验证 X、LLM 和摘要逻辑。";
    }

    function renderLogs(logs) {
      const consoleEl = $("console");
      const consoleFull = $("consoleFull");
      if (!logs || logs.length === 0) {
        if (consoleEl) consoleEl.innerHTML = `<span class="empty">等待任务开始。</span>`;
        consoleFull.innerHTML = `<span class="empty">等待任务开始。</span>`;
        return;
      }
      const text = logs.join("\n");
      if (consoleEl) consoleEl.textContent = text;
      consoleFull.textContent = text;
      if (consoleEl) consoleEl.scrollTop = consoleEl.scrollHeight;
      consoleFull.scrollTop = consoleFull.scrollHeight;
    }

    function setButtons(running) {
      $("dryRunBtn").disabled = running;
      $("liveRunBtn").disabled = running;
      $("stopRunBtn").disabled = !running;
    }

    function showView(viewId) {
      document.querySelectorAll(".view").forEach((view) => {
        view.classList.toggle("active", view.id === viewId);
      });
      document.querySelectorAll(".nav-item").forEach((item) => {
        item.classList.toggle("active", item.dataset.view === viewId);
      });
    }

    function fieldLabel(key) {
      const labels = {
        X_AUTH_MODE: "X 认证模式",
        X_BEARER_TOKEN: "X Bearer Token",
        X_API_KEY: "X API Key",
        X_API_KEY_SECRET: "X API Key Secret",
        X_ACCESS_TOKEN: "X Access Token",
        X_ACCESS_TOKEN_SECRET: "X Access Token Secret",
        X_LIST_ID: "X List ID",
        X_API_BASE: "X API Base",
        X_MAX_RESULTS: "每页推文数",
        X_MAX_PAGES: "最多分页数",
        LLM_API_KEY: "LLM API Key",
        LLM_API_BASE: "LLM API Base",
        LLM_MODEL: "LLM 模型",
        LLM_TIMEOUT_SECONDS: "LLM 超时秒数",
        LLM_MIN_IMPORTANCE: "推送重要性阈值",
        TELEGRAM_BOT_TOKEN: "Telegram Bot Token",
        TELEGRAM_CHAT_ID: "Telegram Chat ID",
        POLL_INTERVAL_SECONDS: "轮询间隔秒数",
        STATE_PATH: "状态文件路径",
        SIGNAL_HISTORY_PATH: "信号历史路径",
        SIGNAL_HISTORY_LIMIT: "信号历史条数",
        LOG_LEVEL: "日志级别",
        WEB_HOST: "Web Host",
        WEB_PORT: "Web Port",
        WEB_AUTO_POLL: "Web 自动轮询"
      };
      return labels[key] || key;
    }

    function fieldHelp(key) {
      if (key === "POLL_INTERVAL_SECONDS") return "daemon 模式自动检查间隔，例如 300 或 1800。";
      if (key === "WEB_HOST") return "本机访问用 127.0.0.1；云端外部访问用 0.0.0.0。";
      if (key === "WEB_PORT") return "修改端口后需要重启 web 命令才会生效。";
      if (key === "WEB_AUTO_POLL") return "true 表示打开网页控制台时也按轮询间隔自动运行。";
      return "保存后从下一次运行开始生效。";
    }

    function inputType(key) {
      return key.endsWith("_SECONDS") || key.endsWith("_PAGES") || key.endsWith("_RESULTS")
        || key.endsWith("_PORT") || key.endsWith("_LIMIT") || key === "LLM_MIN_IMPORTANCE"
        ? "number"
        : "text";
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function escapeAttr(value) {
      return escapeHtml(value);
    }

    $("refreshBtn").addEventListener("click", refresh);
    $("dryRunBtn").addEventListener("click", () => startRun(true));
    $("liveRunBtn").addEventListener("click", () => startRun(false));
    $("stopRunBtn").addEventListener("click", stopRun);
    $("reloadSettingsBtn").addEventListener("click", loadSettings);
    $("settingsForm").addEventListener("submit", saveSettings);
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.addEventListener("click", () => showView(item.dataset.view));
    });

    loadSettings();
    refresh();
    state.polling = setInterval(refresh, 2500);
  </script>
</body>
</html>
"""
