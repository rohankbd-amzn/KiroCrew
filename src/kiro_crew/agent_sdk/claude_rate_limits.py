"""Claude Code's 5-hour and 7-day plan windows, as its ACP adapter reports them.

claude-agent-acp forwards each Claude Agent SDK ``rate_limit_event`` on a ``usage_update``
frame, under ``_meta["_claude/rateLimit"]``. The SDK sends that event only for a Claude
subscription sign-in, and only after a model turn. Its ``unifiedWindows`` object carries both
windows, each as a ``utilization`` fraction of the limit and a ``resetsAt`` time in epoch
seconds. A payload without ``unifiedWindows`` carries at most one window, the one its
``rateLimitType`` names, and only when it also carries ``utilization``. claude-agent-acp
0.94.0 forwards the event under this key, and the claude CLI that its SDK 0.3.303 ships
validates ``unifiedWindows.five_hour`` and ``seven_day`` with this shape. No live capture
carries the key. Each malformed field is dropped, and a payload with no valid field
records nothing.

The frame is read wherever a ``usage_update`` is folded, with no harness gate: the key is the
adapter's own name, so no other harness sends it. The newest value of each window is kept in
this process's memory, because the reading belongs to the account and not to one session. A
gateway restart drops it until the next turn reports one. No credential is read and no
account endpoint is called.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

#: The ``_meta`` key claude-agent-acp puts the SDK's ``rate_limit_info`` under.
_META_KEY = "_claude/rateLimit"

#: The SDK's window names, mapped to the names the usage route uses for every provider.
_WINDOWS = {"five_hour": "five_hour", "seven_day": "weekly"}

#: The ``status`` values the SDK documents. Anything else is dropped rather than shown.
_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})


def _number(value: object) -> float | None:
    """*value* as a float when it is a finite JSON number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int past float range
        return None
    return number if math.isfinite(number) else None


def _window(utilization: object, resets_at: object) -> dict[str, float | None] | None:
    """One window from the SDK's fields, or ``None`` when it carries no usable utilization."""
    fraction = _number(utilization)
    if fraction is None or fraction < 0:
        return None
    # The SDK reports a fraction of the limit; the route reports percent, as Codex does.
    percent = round(fraction * 100, 2)
    if not math.isfinite(percent):  # JSON has no Infinity, so the route could not answer
        return None
    return {
        "used_percent": percent,
        "resets_at": _reset_time(resets_at),
    }


def _reset_time(value: object) -> float | None:
    """*value* as an epoch-seconds reset time, or ``None`` when it is not a positive number."""
    reset = _number(value)
    return reset if reset is not None and reset > 0 else None


def parse_rate_limit(
    update: object,
) -> tuple[dict[str, dict[str, float | None]], str | None, float | None]:
    """The windows, the status and the status's reset time one ``usage_update`` carries.

    All three are empty when the frame carries no reading. The status's reset time is the
    payload's top-level ``resetsAt``: the status describes the limit that resets then, so it
    says nothing once that time has passed. The SDK marks that field optional, so without it
    the status ends with the window ``rateLimitType`` names, else with the last window to reset.

    Pure: validation happens here, so a malformed frame degrades to "no reading" and never
    raises in the frame loop.
    """
    meta = update.get("_meta") if isinstance(update, dict) else None
    info = meta.get(_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return {}, None, None
    windows: dict[str, dict[str, float | None]] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for source, name in _WINDOWS.items():
            raw = unified.get(source)
            if isinstance(raw, dict):
                window = _window(raw.get("utilization"), raw.get("resetsAt"))
                if window is not None:
                    windows[name] = window
    else:
        rate_type = info.get("rateLimitType")
        named = _WINDOWS.get(rate_type) if isinstance(rate_type, str) else None
        if named is not None:
            window = _window(info.get("utilization"), info.get("resetsAt"))
            if window is not None:
                windows[named] = window
    status = info.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        return windows, None, None
    reset = _reset_time(info.get("resetsAt"))
    if reset is None:
        reset = _status_window_reset(windows, info.get("rateLimitType"))
    return windows, status, reset


def _status_window_reset(
    windows: dict[str, dict[str, float | None]], rate_type: object
) -> float | None:
    """The reset of the window *rate_type* names, else the last reset among *windows*."""
    named = windows.get(_WINDOWS.get(rate_type, "")) if isinstance(rate_type, str) else None
    if named is not None and named["resets_at"] is not None:
        return named["resets_at"]
    resets = [w["resets_at"] for w in windows.values()]
    known = [r for r in resets if r is not None]
    return max(known) if known else None


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class _Store:
    """The newest value of each window, and the newest status, with the time each arrived.

    A frame that carries one window replaces only that window, so an older payload shape that
    names one window at a time cannot erase the other.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, dict[str, float | None]] = {}
        self._captured: dict[str, float] = {}
        self._status: str | None = None
        self._status_at: float | None = None
        self._status_resets_at: float | None = None

    def note(self, update: object, now: float) -> None:
        windows, status, status_resets_at = parse_rate_limit(update)
        if not windows and status is None:
            return
        with self._lock:
            for name, window in windows.items():
                self._windows[name] = window
                self._captured[name] = now
            if status is not None:
                self._status = status
                self._status_at = now
                self._status_resets_at = status_resets_at

    def _live_status(self, now: float) -> tuple[str | None, float | None]:
        """The newest status and its reset time, or nothing once that reset has passed."""
        reset = self._status_resets_at
        if self._status is None or (reset is not None and reset <= now):
            return None, None
        return self._status, reset

    def read(self, now: float) -> dict[str, Any] | None:
        with self._lock:
            status, status_resets_at = self._live_status(now)
            if not self._windows and status is None:
                return None
            reading: dict[str, Any] = {}
            for name in _WINDOWS.values():
                window = self._windows.get(name)
                if window is None:
                    reading[name] = None
                    continue
                resets_at = window["resets_at"]
                reading[name] = {
                    "used_percent": window["used_percent"],
                    "resets_at": resets_at,
                    # The SDK names its windows but reports no length for them.
                    "window_minutes": None,
                    "stale": resets_at is not None and resets_at <= now,
                    "captured_at": _iso(self._captured[name]),
                }
            stamps = list(self._captured.values())
            if status is not None and self._status_at is not None:
                stamps.append(self._status_at)
            reading["status"] = status
            reading["status_resets_at"] = status_resets_at
            reading["captured_at"] = _iso(max(stamps))
            return reading


_STORE = _Store()


def note_usage_update(update: object) -> None:
    """Keep the plan windows a ``usage_update`` carries; a frame without them changes nothing."""
    _STORE.note(update, time.time())


def read_claude_rate_limits() -> dict[str, Any] | None:
    """The newest windows Claude Code reported in this process, or ``None`` when there are none.

    ``five_hour`` and ``weekly`` match the Codex reading's keys and fields. Each window also
    has its own ``captured_at``, because a frame can update one window alone. ``stale`` is
    true once a window's reset time has passed. ``status`` is the SDK's newest ``status``,
    and ``status_resets_at`` the reset it belongs to; both are ``None`` once that reset has
    passed, so a ``rejected`` limit does not outlive its window.
    """
    return _STORE.read(time.time())
