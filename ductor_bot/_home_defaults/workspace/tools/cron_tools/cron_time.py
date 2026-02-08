#!/usr/bin/env python3
"""Show current time in configured timezone and common IANA zones.

Helps the agent verify timezone configuration and determine the correct
IANA timezone for a user based on their country or city.

Usage:
    python tools/cron_tools/cron_time.py
    python tools/cron_tools/cron_time.py --zone "America/New_York"
    python tools/cron_tools/cron_time.py --zones "Europe/Berlin,America/New_York,Asia/Tokyo"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from _shared import read_user_timezone

_COMMON_ZONES = [
    "Europe/Berlin",
    "Europe/London",
    "Europe/Paris",
    "Europe/Zurich",
    "Europe/Vienna",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Australia/Sydney",
]


def _format_tz(tz_name: str, now_utc: datetime) -> dict | None:
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return None
    local = now_utc.astimezone(tz)
    return {
        "zone": tz_name,
        "time": local.strftime("%H:%M"),
        "date": local.strftime("%Y-%m-%d"),
        "utc_offset": local.strftime("%z"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Show current time in IANA timezones")
    parser.add_argument("--zone", help="Single IANA timezone to check")
    parser.add_argument("--zones", help="Comma-separated IANA timezones")
    args = parser.parse_args()

    now_utc = datetime.now(UTC)
    config_tz = read_user_timezone()

    result: dict = {
        "utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "user_timezone_configured": config_tz or None,
    }

    if config_tz:
        info = _format_tz(config_tz, now_utc)
        if info:
            result["user_time"] = info
        else:
            result["user_timezone_error"] = f"Invalid timezone: {config_tz}"
    else:
        result["user_timezone_hint"] = (
            "No user_timezone set in config.json. "
            "Ask the user where they are and set it. "
            "Example: edit config.json -> \"user_timezone\": \"Europe/Berlin\""
        )

    if args.zone:
        info = _format_tz(args.zone, now_utc)
        if info:
            result["requested_zone"] = info
        else:
            result["error"] = f"Unknown timezone: {args.zone}"
            print(json.dumps(result))
            sys.exit(1)
    elif args.zones:
        zones = [z.strip() for z in args.zones.split(",") if z.strip()]
        result["requested_zones"] = [
            info for z in zones if (info := _format_tz(z, now_utc)) is not None
        ]
    else:
        result["common_zones"] = [
            info for z in _COMMON_ZONES if (info := _format_tz(z, now_utc)) is not None
        ]

    print(json.dumps(result))


if __name__ == "__main__":
    main()
