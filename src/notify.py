"""ntfy.sh notifications via a plain HTTPS POST.

Success fires LAST, only after Notion writes land. Failure also fires, so a
broken run surfaces instead of silently draining credit.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10


def _post(topic: str, message: str, title: str, priority: str, tags: str) -> None:
    if not topic:
        log.warning("No ntfy topic configured; skipping notification: %s", message)
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("ntfy POST failed: %s", exc)


def notify_success(topic: str, new_count: int) -> None:
    _post(topic, f"Job run done: {new_count} new roles in Notion",
          title="Jobber", priority="default", tags="briefcase")


def notify_failure(topic: str, detail: str) -> None:
    _post(topic, f"Job run FAILED: {detail}",
          title="Jobber", priority="high", tags="warning")
