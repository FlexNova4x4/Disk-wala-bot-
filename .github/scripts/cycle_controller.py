#!/usr/bin/env python3
"""Call idempotent hosting webhooks for a 5h50m online / 20m offline cycle."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ONLINE_SECONDS = (5 * 60 + 50) * 60
OFFLINE_SECONDS = 20 * 60
PERIOD_SECONDS = ONLINE_SECONDS + OFFLINE_SECONDS


def parse_anchor(value: str) -> datetime:
    """Parse an ISO-8601 timestamp with an explicit timezone into UTC."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "CYCLE_ANCHOR_UTC must be an ISO-8601 timestamp with timezone, "
            "e.g. 2026-11-16T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            "CYCLE_ANCHOR_UTC must include a timezone, e.g. 2026-11-16T00:00:00Z"
        )
    return parsed.astimezone(timezone.utc)


def validate_webhook_url(value: str, setting_name: str) -> str:
    """Require HTTPS webhook URLs so secret-bearing URLs are not sent in clear text."""
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{setting_name} must be a valid HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{setting_name} must not contain embedded username/password")
    return value.strip()


def desired_state(anchor: datetime, now: datetime) -> tuple[str, float]:
    """Return the desired host state and elapsed seconds within the current cycle.

    Before the configured anchor, the desired state is offline. At the anchor
    the first online window starts. After that, 350 minutes are online and
    20 minutes are offline, repeating indefinitely.
    """
    anchor_utc = anchor.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    elapsed = (now_utc - anchor_utc).total_seconds()
    if elapsed < 0:
        return "offline", 0.0
    cycle_position = elapsed % PERIOD_SECONDS
    if cycle_position < ONLINE_SECONDS:
        return "online", cycle_position
    return "offline", cycle_position


def post_state(url: str, state: str) -> int:
    """Send an idempotent state request without logging the secret webhook URL."""
    payload = json.dumps({"desired_state": state}).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiskWalaBot-Cycle-Controller",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        # Do not print response bodies: providers may echo sensitive information.
        return int(response.status)


def main() -> int:
    anchor_raw = os.environ.get("CYCLE_ANCHOR_UTC", "").strip()
    start_raw = os.environ.get("HOST_START_WEBHOOK", "").strip()
    stop_raw = os.environ.get("HOST_STOP_WEBHOOK", "").strip()
    configured = (bool(anchor_raw), bool(start_raw), bool(stop_raw))

    if not any(configured):
        print("Cycle controller skipped: hosting secrets are not configured yet.")
        return 0
    if not all(configured):
        print(
            "Configuration error: set all three cycle secrets: "
            "CYCLE_ANCHOR_UTC, HOST_START_WEBHOOK, HOST_STOP_WEBHOOK.",
            file=sys.stderr,
        )
        return 2

    try:
        anchor = parse_anchor(anchor_raw)
        start_url = validate_webhook_url(start_raw, "HOST_START_WEBHOOK")
        stop_url = validate_webhook_url(stop_raw, "HOST_STOP_WEBHOOK")
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    state, cycle_position = desired_state(anchor, datetime.now(timezone.utc))
    target_url = start_url if state == "online" else stop_url

    try:
        status = post_state(target_url, state)
    except Exception as exc:  # avoid printing exception text, which can include secret URLs
        print(
            f"{state.upper()} webhook request failed ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 1

    print(f"{state.upper()} webhook returned HTTP {status}.")
    if cycle_position == 0 and state == "offline":
        print("Cycle has not reached CYCLE_ANCHOR_UTC; host should remain offline.")
    else:
        print(
            f"Desired state: {state.upper()}; "
            f"cycle position: {cycle_position / 60:.1f} minutes."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
