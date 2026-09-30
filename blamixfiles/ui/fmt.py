"""Small display helpers."""
from __future__ import annotations

import time


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(t: float) -> str:
    if not t:
        return ""
    lt = time.localtime(t)
    if lt.tm_year == time.localtime().tm_year:
        return time.strftime("%d %b %H:%M", lt)
    return time.strftime("%d %b %Y", lt)


def human_speed(bps: float) -> str:
    return f"{human_size(bps)}/s" if bps > 0 else ""
