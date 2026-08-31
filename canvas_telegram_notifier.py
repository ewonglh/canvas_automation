#!/usr/bin/env python3
"""Notify a Telegram chat when Canvas assignments become visible.

The script uses only Python's standard library so it can run directly in
GitHub Actions.  Credentials are read from environment variables; no
credentials are stored in the repository.

Required environment variables:
  CANVAS_BASE_URL       Canvas instance URL, for example
                        https://school.instructure.com
  CANVAS_API_TOKEN      Canvas personal access token
  TELEGRAM_BOT_TOKEN    Telegram bot token
  TELEGRAM_CHAT_ID      Telegram chat or channel ID

Optional environment variables:
  CANVAS_COURSE_IDS     Comma-separated course IDs.  If omitted, active
                        courses for the Canvas token are discovered.
  CANVAS_STATE_FILE     State path; defaults to canvas_quiz_state.json.
  CANVAS_INTERVAL_SECONDS
                        Polling interval for continuous mode; defaults to 300.

The normal GitHub Actions invocation is:

    python canvas_telegram_notifier.py --once

State is updated only after a notification succeeds.  An assignment that is
published and unlocked, then later becomes locked and is unlocked again, is
treated as visible again and will generate a new notification.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("canvas_telegram_notifier")
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_INTERVAL_SECONDS = 300
STATE_VERSION = 2
STATE_KEY = "assignments"


class ApiError(RuntimeError):
    """An expected remote API failure with a safe-to-log message."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _parse_course_ids(value: str | None) -> list[str]:
    if not value:
        return []
    result: list[str] = []
    for item in value.split(","):
        course_id = item.strip()
        if course_id and course_id not in result:
            result.append(course_id)
    return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_canvas_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        LOGGER.warning("Ignoring unparseable Canvas date: %s", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_true(value: Any) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.lower() == "true")


def _is_false(value: Any) -> bool:
    return value is False or value == 0 or (isinstance(value, str) and value.lower() == "false")


def is_assignment_visible(assignment: dict[str, Any], now: datetime | None = None) -> bool:
    """Return whether the assignment appears available to the Canvas API user."""

    if _is_false(assignment.get("published")):
        return False
    if _is_true(assignment.get("locked_for_user")):
        return False
    if str(assignment.get("workflow_state", "")).lower() in {"unpublished", "deleted", "locked"}:
        return False

    current = now or _utc_now()
    unlock_at = _parse_canvas_datetime(assignment.get("unlock_at"))
    if unlock_at and current < unlock_at:
        return False

    lock_at = _parse_canvas_datetime(assignment.get("lock_at"))
    if lock_at and current >= lock_at:
        return False

    return True


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    description: str,
) -> tuple[Any, Any]:
    body = None
    request_headers = dict(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            raw = response.read()
            decoded = json.loads(raw.decode("utf-8")) if raw else None
            return decoded, response.headers
    except HTTPError as exc:
        # Do not include str(exc): the Telegram URL contains the bot token.
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise ApiError(f"{description} returned HTTP {exc.code}{suffix}") from None
    except URLError as exc:
        raise ApiError(f"Could not reach {description}: {exc.reason}") from None
    except json.JSONDecodeError:
        raise ApiError(f"{description} returned invalid JSON") from None


def _canvas_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "canvas-telegram-notifier/1.0",
    }


def _paginated_canvas_get(
    first_url: str,
    *,
    token: str,
    description: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    url: str | None = first_url
    pages = 0
    while url:
        pages += 1
        if pages > 100:
            raise ApiError(f"{description} exceeded the 100-page safety limit")
        data, headers = _request_json(
            url,
            headers=_canvas_headers(token),
            description=description,
        )
        if not isinstance(data, list):
            raise ApiError(f"{description} returned an unexpected response")
        records.extend(item for item in data if isinstance(item, dict))
        url = _next_page_url(headers.get("Link", ""))
    return records


def _next_page_url(link_header: str) -> str | None:
    for link in link_header.split(","):
        parts = [part.strip() for part in link.split(";")]
        if not parts or not parts[0].startswith("<") or not parts[0].endswith(">"):
            continue
        relation = " ".join(parts[1:])
        if 'rel="next"' in relation or "rel=\"next\"" in relation:
            return parts[0][1:-1]
    return None


def fetch_assignments(
    canvas_base_url: str,
    canvas_token: str,
    course_ids: list[str],
) -> list[dict[str, Any]]:
    """Fetch courses and their Canvas assignments."""

    base_url = canvas_base_url.rstrip("/")
    if course_ids:
        courses = []
        for course_id in course_ids:
            encoded_id = quote(course_id, safe="")
            data, _ = _request_json(
                f"{base_url}/api/v1/courses/{encoded_id}",
                headers=_canvas_headers(canvas_token),
                description=f"Canvas course {course_id}",
            )
            if not isinstance(data, dict):
                raise ApiError(f"Canvas course {course_id} returned an unexpected response")
            courses.append(data)
    else:
        courses = _paginated_canvas_get(
            f"{base_url}/api/v1/courses?enrollment_state=active&per_page=100",
            token=canvas_token,
            description="Canvas course list",
        )

    result: list[dict[str, Any]] = []
    for course in courses:
        if "id" not in course:
            continue
        course_id = str(course["id"])
        encoded_id = quote(course_id, safe="")
        assignments = _paginated_canvas_get(
            f"{base_url}/api/v1/courses/{encoded_id}/assignments?per_page=100",
            token=canvas_token,
            description=f"Canvas assignments for course {course_id}",
        )
        course_name = str(course.get("name") or course.get("course_code") or course_id)
        for assignment in assignments:
            result.append(
                {
                    "course_id": course_id,
                    "course_name": course_name,
                    "assignment": assignment,
                }
            )
    return result


def _assignment_url(canvas_base_url: str, course_id: str, assignment: dict[str, Any]) -> str:
    supplied_url = assignment.get("html_url") or assignment.get("url")
    if isinstance(supplied_url, str) and supplied_url.strip():
        return supplied_url.strip()
    assignment_id = quote(str(assignment.get("id", "")), safe="")
    return f"{canvas_base_url.rstrip('/')}/courses/{quote(course_id, safe='')}/assignments/{assignment_id}"


def _assignment_key(course_id: str, assignment: dict[str, Any]) -> str:
    return f"{course_id}:{assignment.get('id')}"


def _snapshot(
    *,
    canvas_base_url: str,
    course_id: str,
    course_name: str,
    assignment: dict[str, Any],
    visible: bool,
    previous: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    entry = dict(previous)
    entry.update(
        {
            "course_id": course_id,
            "course_name": course_name,
            "assignment_id": str(assignment.get("id", "")),
            "title": str(assignment.get("name") or assignment.get("title") or "Untitled assignment"),
            "url": _assignment_url(canvas_base_url, course_id, assignment),
            "due_at": assignment.get("due_at"),
            "visible": visible,
        }
    )
    if visible and not previous.get("visible", False):
        entry["became_visible_at"] = now
    if not visible and previous.get("visible", False):
        entry["became_hidden_at"] = now
    return entry


def _display_due_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = _parse_canvas_datetime(value)
    if not parsed:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _telegram_message(record: dict[str, Any], canvas_base_url: str) -> str:
    course_name = html.escape(str(record["course_name"]))
    title = html.escape(str(record["title"]))
    message = ["📝 <b>New Canvas assignment is visible</b>", "", f"<b>Course:</b> {course_name}", f"<b>Assignment:</b> {title}"]
    due = _display_due_date(record.get("due_at"))
    if due:
        message.append(f"<b>Due:</b> {html.escape(due)}")
    url = record.get("url") or canvas_base_url
    message.append(f'🔗 <a href="{html.escape(str(url), quote=True)}">Open assignment</a>')
    return "\n".join(message)


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> None:
    # Keep the token in the URL only; never log this URL or include it in an
    # exception message.
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data, _ = _request_json(
        url,
        headers={"Accept": "application/json"},
        method="POST",
        payload={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        description="Telegram sendMessage",
    )
    if not isinstance(data, dict) or data.get("ok") is not True:
        detail = data.get("description") if isinstance(data, dict) else "unexpected response"
        raise ApiError(f"Telegram sendMessage failed: {detail}")


def _load_state(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return {"version": STATE_VERSION, STATE_KEY: {}}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read state file {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"State file {path} has an invalid format")
    # Keep any old quiz data intact but never use it for assignment polling.
    # This lets an existing state file migrate without losing historical data.
    if STATE_KEY not in data:
        data[STATE_KEY] = {}
    if not isinstance(data[STATE_KEY], dict):
        raise ValueError(f"State file {path} has an invalid format")
    data["version"] = STATE_VERSION
    return data, True


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def poll_once(
    *,
    canvas_base_url: str,
    canvas_token: str,
    telegram_bot_token: str,
    telegram_chat_id: str,
    course_ids: list[str],
    state_path: Path,
    dry_run: bool = False,
) -> int:
    now_datetime = _utc_now()
    now = _timestamp(now_datetime)
    state, state_file_exists = _load_state(state_path)
    original_state = json.loads(json.dumps(state))
    assignments_state = state.setdefault(STATE_KEY, {})

    records = fetch_assignments(canvas_base_url, canvas_token, course_ids)
    records.sort(key=lambda item: (str(item["course_name"]).lower(), str(item["assignment"].get("name", item["assignment"].get("title", ""))).lower(), str(item["assignment"].get("id", ""))))

    fetched_keys: set[str] = set()
    selected_course_ids: set[str] = set()
    pending_notifications: list[dict[str, Any]] = []
    for item in records:
        course_id = str(item["course_id"])
        course_name = str(item["course_name"])
        assignment = item["assignment"]
        if "id" not in assignment:
            LOGGER.warning("Skipping a Canvas assignment without an ID in course %s", course_id)
            continue
        selected_course_ids.add(course_id)
        key = _assignment_key(course_id, assignment)
        fetched_keys.add(key)
        previous = assignments_state.get(key, {})
        if not isinstance(previous, dict):
            previous = {}
        visible = is_assignment_visible(assignment, now_datetime)
        if visible and not previous.get("visible", False):
            pending_notifications.append(
                {
                    "key": key,
                    "course_id": course_id,
                    "course_name": course_name,
                    "title": str(assignment.get("name") or assignment.get("title") or "Untitled assignment"),
                    "due_at": assignment.get("due_at"),
                    "url": _assignment_url(canvas_base_url, course_id, assignment),
                    "assignment": assignment,
                    "previous": previous,
                }
            )
        elif not dry_run:
            assignments_state[key] = _snapshot(
                canvas_base_url=canvas_base_url,
                course_id=course_id,
                course_name=course_name,
                assignment=assignment,
                visible=visible,
                previous=previous,
                now=now,
            )

    if dry_run:
        for notification in pending_notifications:
            LOGGER.info("DRY RUN: would notify for %s / %s", notification["course_name"], notification["title"])
        LOGGER.info("DRY RUN: found %d assignment records, %d newly visible", len(records), len(pending_notifications))
        return len(pending_notifications)

    failures = 0
    for notification in pending_notifications:
        message = _telegram_message(notification, canvas_base_url)
        try:
            send_telegram_message(telegram_bot_token, telegram_chat_id, message)
        except ApiError as exc:
            failures += 1
            LOGGER.error("Could not notify for %s / %s: %s", notification["course_name"], notification["title"], exc)
            continue
        assignments_state[notification["key"]] = _snapshot(
            canvas_base_url=canvas_base_url,
            course_id=notification["course_id"],
            course_name=notification["course_name"],
            assignment=notification["assignment"],
            visible=True,
            previous=notification["previous"],
            now=now,
        )
        LOGGER.info("Notified for %s / %s", notification["course_name"], notification["title"])

    # An assignment that disappeared from a successfully fetched course is no longer
    # considered visible.  If it is later returned by Canvas, it can notify
    # again.  Never do this when no course was successfully discovered.
    if selected_course_ids:
        for key, entry in list(assignments_state.items()):
            if not isinstance(entry, dict) or str(entry.get("course_id")) not in selected_course_ids:
                continue
            if key not in fetched_keys and entry.get("visible", False):
                entry = dict(entry)
                entry["visible"] = False
                entry["became_hidden_at"] = now
                assignments_state[key] = entry

    if state != original_state or not state_file_exists:
        _write_state(state_path, state)
        LOGGER.info("Saved notification state to %s", state_path)

    if failures:
        raise ApiError(f"{failures} Telegram notification(s) failed")
    LOGGER.info("Poll complete: %d assignment records, %d newly visible", len(records), len(pending_notifications))
    return len(pending_notifications)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one poll and exit")
    parser.add_argument("--dry-run", action="store_true", help="Poll Canvas without sending Telegram messages or changing state")
    parser.add_argument("--state-file", type=Path, help="Override CANVAS_STATE_FILE")
    parser.add_argument("--interval", type=int, help="Continuous-mode interval in seconds")
    return parser


def _run(args: argparse.Namespace) -> int:
    canvas_base_url = _required_env("CANVAS_BASE_URL").rstrip("/")
    canvas_token = _required_env("CANVAS_API_TOKEN")
    course_ids = _parse_course_ids(os.environ.get("CANVAS_COURSE_IDS"))
    state_path = args.state_file or Path(os.environ.get("CANVAS_STATE_FILE", "canvas_quiz_state.json"))
    interval = args.interval or int(os.environ.get("CANVAS_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS))
    if interval <= 0:
        raise ValueError("Polling interval must be positive")

    telegram_bot_token = ""
    telegram_chat_id = ""
    if not args.dry_run:
        telegram_bot_token = _required_env("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = _required_env("TELEGRAM_CHAT_ID")

    while True:
        poll_once(
            canvas_base_url=canvas_base_url,
            canvas_token=canvas_token,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            course_ids=course_ids,
            state_path=state_path,
            dry_run=args.dry_run,
        )
        if args.once:
            return 0
        time.sleep(interval)


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        return _run(_build_parser().parse_args(argv))
    except (ApiError, ValueError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
