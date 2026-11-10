"""Claude Code's plan windows, read from the ``_claude/rateLimit`` meta on ``usage_update``."""

from __future__ import annotations

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _make_runtime, _register
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.usage as usage_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import METHOD_SESSION_UPDATE, AcpPromptStats, JsonRpcMessage
from kiro_crew.agent_sdk import claude_rate_limits

#: 2100-01-01 and 2100-01-08, reset times that have not passed.
_FIVE_HOUR_RESET = 4102444800
_WEEKLY_RESET = 4103049600


def _info(**fields: object) -> dict:
    return {"status": "allowed", "rateLimitType": "five_hour", **fields}


def _unified(five_hour: float, seven_day: float) -> dict:
    return {
        "five_hour": {"utilization": five_hour, "resetsAt": _FIVE_HOUR_RESET},
        "seven_day": {"utilization": seven_day, "resetsAt": _WEEKLY_RESET},
    }


def _frame(info: object) -> dict:
    return {
        "sessionUpdate": "usage_update",
        "used": 10,
        "size": 100,
        "_meta": {"_claude/rateLimit": info},
    }


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> claude_rate_limits._Store:
    fresh = claude_rate_limits._Store()
    monkeypatch.setattr(claude_rate_limits, "_STORE", fresh)
    return fresh


def test_unified_windows_give_both_windows_in_percent(store: claude_rate_limits._Store) -> None:
    store.note(_frame(_info(unifiedWindows=_unified(0.24, 0.13))), now=1000.0)

    reading = store.read(now=1000.0)

    assert reading == {
        "five_hour": {
            "used_percent": 24.0,
            "resets_at": _FIVE_HOUR_RESET,
            "window_minutes": None,
            "stale": False,
            "captured_at": "1970-01-01T00:16:40Z",
        },
        "weekly": {
            "used_percent": 13.0,
            "resets_at": _WEEKLY_RESET,
            "window_minutes": None,
            "stale": False,
            "captured_at": "1970-01-01T00:16:40Z",
        },
        "status": "allowed",
        # No top-level resetsAt, so the status ends with the window rateLimitType names.
        "status_resets_at": _FIVE_HOUR_RESET,
        "captured_at": "1970-01-01T00:16:40Z",
    }


def test_a_one_window_frame_keeps_the_other_window(store: claude_rate_limits._Store) -> None:
    store.note(_frame(_info(unifiedWindows=_unified(0.24, 0.13))), now=1000.0)
    store.note(
        _frame(_info(status="allowed_warning", utilization=0.91, resetsAt=_FIVE_HOUR_RESET)),
        now=2000.0,
    )

    reading = store.read(now=2000.0)

    assert reading is not None
    assert reading["five_hour"]["used_percent"] == 91.0
    assert reading["five_hour"]["captured_at"] == "1970-01-01T00:33:20Z"
    assert reading["weekly"]["used_percent"] == 13.0
    assert reading["weekly"]["captured_at"] == "1970-01-01T00:16:40Z"
    assert reading["status"] == "allowed_warning"
    assert reading["captured_at"] == "1970-01-01T00:33:20Z"


def test_a_window_past_its_reset_is_stale(store: claude_rate_limits._Store) -> None:
    store.note(_frame(_info(unifiedWindows=_unified(0.5, 0.5))), now=1000.0)

    reading = store.read(now=_FIVE_HOUR_RESET + 1)

    assert reading is not None
    assert reading["five_hour"]["stale"] is True
    assert reading["weekly"]["stale"] is False


def test_a_status_says_nothing_once_its_reset_has_passed(store: claude_rate_limits._Store) -> None:
    store.note(
        _frame(_info(status="rejected", resetsAt=5000, unifiedWindows=_unified(1.0, 0.4))),
        now=1000.0,
    )

    before = store.read(now=4999.0)
    after = store.read(now=5000.0)

    assert before is not None
    assert (before["status"], before["status_resets_at"]) == ("rejected", 5000.0)
    assert after is not None
    assert (after["status"], after["status_resets_at"]) == (None, None)
    # The windows are still there, judged by their own reset times.
    assert after["five_hour"]["used_percent"] == 100.0


@pytest.mark.parametrize(
    ("rate_type", "reset"),
    [
        ("seven_day", _WEEKLY_RESET),
        ("five_hour", _FIVE_HOUR_RESET),
        ("seven_day_opus", _WEEKLY_RESET),
    ],
)
def test_a_status_without_its_own_reset_ends_with_its_window(
    store: claude_rate_limits._Store, rate_type: str, reset: int
) -> None:
    store.note(
        _frame(
            _info(status="rejected", rateLimitType=rate_type, unifiedWindows=_unified(0.4, 1.0))
        ),
        now=1000.0,
    )

    before = store.read(now=reset - 1)
    after = store.read(now=reset)

    assert before is not None
    assert (before["status"], before["status_resets_at"]) == ("rejected", reset)
    assert after is not None
    assert (after["status"], after["status_resets_at"]) == (None, None)


def test_an_expired_status_alone_is_no_reading(store: claude_rate_limits._Store) -> None:
    store.note(_frame(_info(status="rejected", rateLimitType="overage", resetsAt=5000)), now=1000.0)

    assert store.read(now=6000.0) is None


def test_a_status_alone_is_a_reading_with_no_windows(store: claude_rate_limits._Store) -> None:
    store.note(_frame(_info(status="rejected", rateLimitType="seven_day_opus")), now=1000.0)

    reading = store.read(now=1000.0)

    assert reading is not None
    assert reading["five_hour"] is None
    assert reading["weekly"] is None
    assert reading["status"] == "rejected"


@pytest.mark.parametrize(
    "update",
    [
        {"sessionUpdate": "usage_update", "used": 10, "size": 100},
        {"sessionUpdate": "usage_update", "_meta": "not a dict"},
        _frame("not a dict"),
        _frame({"unifiedWindows": {"five_hour": {"utilization": "0.5", "resetsAt": 1}}}),
        _frame({"unifiedWindows": {"five_hour": {"utilization": True, "resetsAt": 1}}}),
        _frame({"unifiedWindows": {"five_hour": {"utilization": float("nan")}}}),
        _frame({"unifiedWindows": {"five_hour": {"utilization": -0.1}}}),
        # Past float range: float() raises OverflowError on the int.
        _frame({"unifiedWindows": {"five_hour": {"utilization": 10**400}}}),
        # Finite, but its percent is inf, which JSON cannot carry.
        _frame({"unifiedWindows": {"five_hour": {"utilization": 1e307}}}),
        _frame({"status": "unheard_of", "rateLimitType": "five_hour"}),
        # Not hashable: a set membership test on it would raise in the frame loop.
        _frame({"status": [], "rateLimitType": "five_hour"}),
        _frame({"status": {"a": 1}}),
        _frame({"rateLimitType": "five_hour"}),
        "not a dict",
    ],
)
def test_a_frame_without_a_usable_reading_records_nothing(
    store: claude_rate_limits._Store, update: object
) -> None:
    store.note(update, now=1000.0)

    assert store.read(now=1000.0) is None


@pytest.mark.parametrize("resets_at", ["soon", 10**400])
def test_an_unusable_reset_time_is_none(
    store: claude_rate_limits._Store, resets_at: object
) -> None:
    unified = {"five_hour": {"utilization": 0.5, "resetsAt": resets_at}}
    store.note(_frame(_info(unifiedWindows=unified)), now=1000.0)

    reading = store.read(now=1000.0)

    assert reading is not None
    assert reading["five_hour"]["resets_at"] is None
    assert reading["five_hour"]["stale"] is False


def test_the_direct_client_path_records_the_reading(store: claude_rate_limits._Store) -> None:
    client = AcpClient.__new__(AcpClient)  # no process is spawned
    client.last_prompt_stats = AcpPromptStats()
    client._spawn_start_token = None

    class _Msg:
        params = {"update": _frame(_info(unifiedWindows=_unified(0.3, 0.6)))}

    client._track_usage_update(_Msg())  # type: ignore[arg-type]

    reading = claude_rate_limits.read_claude_rate_limits()
    assert reading is not None
    assert reading["five_hour"]["used_percent"] == 30.0
    # The context meter still reads the same frame.
    assert client.last_prompt_stats.context_used_tokens == 10


def test_the_shared_runtime_path_records_the_reading(store: claude_rate_limits._Store) -> None:
    rt, _, _ = _make_runtime()
    queues = _register(rt, "sA")
    handle = AcpSessionHandle("sA", queues["sA"], rt)
    msg = JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={"sessionId": "sA", "update": _frame(_info(unifiedWindows=_unified(0.4, 0.7)))},
    )

    assert handle._handle_update(msg) == []

    reading = claude_rate_limits.read_claude_rate_limits()
    assert reading is not None
    assert reading["weekly"]["used_percent"] == 70.0
    assert handle.last_prompt_stats.context_used_tokens == 10


@pytest.mark.asyncio
async def test_the_usage_route_reads_claude_outside_its_cache(
    store: claude_rate_limits._Store, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def _snapshot() -> tuple[dict, bool]:
        return usage_mod._empty_session_summary(), False

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(usage_mod, "_CACHE", {})
    monkeypatch.setattr(usage_mod, "_CACHE_TS", 0.0)
    monkeypatch.setattr(usage_mod, "_usage_sessions_snapshot", _snapshot)
    app = web.Application()
    app.router.add_get("/api/usage/kiro", usage_mod.api_kiro_usage)

    async with TestClient(TestServer(app)) as client:
        first = await (await client.get("/api/usage/kiro")).json()
        claude_rate_limits.note_usage_update(_frame(_info(unifiedWindows=_unified(0.2, 0.1))))
        # Inside the response cache's lifetime: only the Claude reading may move.
        second = await (await client.get("/api/usage/kiro")).json()

    assert first["claude"] is None
    assert second["claude"]["five_hour"]["used_percent"] == 20.0
    assert second["sessions"] == first["sessions"]
