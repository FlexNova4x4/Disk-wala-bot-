#!/usr/bin/env python3
"""Call idempotent hosting-provider webhooks for a 5h ON / 20m OFF cycle."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen

PERIOD_SECONDS = (5 * 60 + 20) * 60
ONLINE_SECONDS = 5 * 60 * 60


def parse_anchor(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("CYCLE_ANCHOR_UTC must include a timezone, e.g. 2026-11-16T00:00:00Z")
    return parsed.astimezone(timezone.utc)


def main() -> int:
    anchor_raw = os.environ.get("CYCLE_ANCHOR_UTC", "").strip()
    start_url = os.environ.get("HOST_START_WEBHOOK", "").strip()
    stop_url = os.environ.get("HOST_STOP_WEBHOOK", "").strip()
    if not anchor_raw or not start_url or not stop_url:
        print("Missing CYCLE_ANCHOR_UTC, HOST_START_WEBHOOK, or HOST_STOP_WEBHOOK secret.", file=sys.stderr)
        return 2

    try:
        anchor = parse_anchor(anchor_raw)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    phase = (now - anchor).total_seconds() % PERIOD_SECONDS
    online = phase < ONLINE_SECONDS
    target = start_url if online else stop_url
    action = "START" if online else "STOP"

    # The provider endpoint must be idempotent because GitHub's scheduled
    # workflow runs every five minutes and can be delayed or repeated.
    request = Request(
        target,
        data=json.dumps({"desired_state": "online" if online else "offline"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "DiskWalaBot-Cycle-Controller"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(500).decode("utf-8", errors="replace")
            print(f"{action} webhook returned HTTP {response.status}: {body}")
    except Exception as exc:
        print(f"{action} webhook failed: {exc}", file=sys.stderr)
        return 1

    print(f"Phase: {'ONLINE' if online else 'OFFLINE'}; cycle minute={phase / 60:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
