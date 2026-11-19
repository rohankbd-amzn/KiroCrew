"""Answering a waiting-on-you card through the ordinary chat send, and dismissing one.

A card answer is one ordinary person turn sent through ``POST /api/chat``: its text is
only what the owner chose or typed, and its meta carries a claim naming the ask
(``waitingOnYouAnswer``). When the row is written, a claim that names a live ask
stamps the reserved ``meta.waitingOnYou`` and retires only that ask. Anything else
behaves like a plain message. A queued answer retires nothing until its row is written.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from waiting_on_you_helpers import (  # noqa: F401 -- fixtures are requested by name
    SLOT,
    ask,
    clock_fixture,
    make_app,
    pin,
    settle_retirements,
    state_fixture,
    store_fixture,
    user_rows,
    waiting_rows,
)

from kiro_crew.dashboard import waiting_on_you_events as events
from kiro_crew.history_schema import HUMAN_TURN_META_KEY
from kiro_crew.testing.wait import await_until

_RUN_CHAT = "kiro_crew.dashboard.chat_handlers._run_chat"
_PAUSED = "kiro_crew.crew_pause.holds_slot"
DISMISS = f"/api/chat/slots/{SLOT}/waiting-on-you/{{key}}/dismiss"


def _client(state, **kw) -> TestClient:
    return TestClient(TestServer(make_app(state, **kw)))


async def _answer(client, text: str, key: str, ask_id: str, **extra) -> tuple[int, dict]:
    """Send *text* as the card does: the plain chat send with a claim in its meta."""
    meta = {"sendId": f"wa-{key}-{len(text)}", "waitingOnYouAnswer": {"key": key, "ask_id": ask_id}}
    resp = await client.post(
        "/api/chat?ws=1", json={"slot": SLOT, "message": text, "meta": meta, **extra}
    )
    status, body = resp.status, await resp.json()
    await settle_retirements()
    return status, body


async def _drain(state) -> None:
    """Start the next queued turn, as the end of the running one does, and let it land."""
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

    slot = state._slots[SLOT]
    cfg = MagicMock()
    cfg.dashboard.merge_queued_messages = True
    with (
        patch("kiro_crew.dashboard.chat_runner._run_chat", new=AsyncMock()),
        patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg),
    ):
        slot.task = None
        await _start_next_queued_turn(state, slot)
        if slot.task is not None:
            await slot.task
    await settle_retirements()


@pytest.mark.asyncio
async def test_a_label_retires_only_its_own_ask_when_its_row_is_written(state, store):
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            status, _ = await _answer(client, "I posted it", "first", first["ask_id"])
    assert status == 200
    rows = user_rows(state)
    assert [r["content"] for r in rows] == ["I posted it"]
    assert rows[0]["meta"][HUMAN_TURN_META_KEY] is True
    assert rows[0]["meta"]["waitingOnYou"] == {"key": "first", "ask_id": first["ask_id"]}
    assert "waitingOnYouAnswer" not in rows[0]["meta"]
    assert [a["key"] for a in store.asks(SLOT)] == ["second"]
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [("first", "answered")]
    assert [r["meta"]["card"]["status"] for r in waiting_rows(state)] == ["answered"]


@pytest.mark.asyncio
async def test_the_turn_an_answer_opens_is_told_which_ask_it_answers(state, store):
    pinned = await pin(store, ask(options=()))
    first_block = asyncio.get_running_loop().create_future()

    async def capture_first_block(run_state, slot, *_args, **_kwargs):
        first_block.set_result(await events.turn_block(run_state, slot))

    with patch(_RUN_CHAT, new=capture_first_block):
        async with _client(state) as client:
            await _answer(client, "Done", "scope-override", pinned["ask_id"])
            block = await await_until(first_block, what="the answer turn's first context block")
    assert 'answers your ask "scope-override"' in block
    assert "answered by the person" in block
    assert "Pinned now" not in block


@pytest.mark.asyncio
async def test_the_turn_retries_a_retirement_the_row_could_not_write(state, store, monkeypatch):
    pinned = await pin(store, ask("first"))
    await pin(store, ask("second"))
    real_retire = store.retire
    failures = iter([True])

    async def _flaky_retire(*args, **kwargs):
        if next(failures, False):
            raise store_mod_unavailable()
        return await real_retire(*args, **kwargs)

    monkeypatch.setattr(store, "retire", _flaky_retire)
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            await _answer(client, "Hold", "first", pinned["ask_id"])
    assert [a["key"] for a in store.asks(SLOT)] == ["first", "second"]
    block = await events.turn_block(state, state._slots[SLOT])
    assert [a["key"] for a in store.asks(SLOT)] == ["second"]
    assert 'answers your ask "first"' in block


def store_mod_unavailable():
    from kiro_crew.dashboard.waiting_on_you import StoreUnavailable

    return StoreUnavailable()


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["forged", "stale", "wrong-key"])
async def test_a_claim_that_names_no_live_ask_is_refused_and_retires_nothing(state, store, why):
    """A forged, stale or mismatched claim is refused before it is sent, so no bare
    answer reaches the agent and the asks still pinned stay."""
    old = await pin(store, ask("first"))
    await pin(store, ask("second"))
    key, ask_id = "first", "wy-" + "0" * 16
    if why == "stale":
        await pin(store, {**ask("first"), "title": "Replaced"})
        ask_id = old["ask_id"]
    elif why == "wrong-key":
        key, ask_id = "second", old["ask_id"]
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            status, body = await _answer(client, "Hold", key, ask_id)
    assert (status, body.get("code")) == (409, "waiting_on_you_stale")
    assert user_rows(state) == []
    assert sorted(a["key"] for a in store.asks(SLOT)) == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replaced", "cleared"])
async def test_a_queued_answer_whose_ask_changed_retires_nothing_when_it_lands(
    state, store, change
):
    """The agent replaces or clears the ask while its answer waits in the queue: the
    answer still reaches the agent, but ends no other ask."""
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            status, _ = await _answer(client, "Hold", "first", first["ask_id"])
    assert status in (200, 202)
    if change == "replaced":
        await pin(store, {**ask("first"), "title": "Replaced"})
    else:
        await store.retire(SLOT, first["ask_id"], "cleared")
    await _drain(state)
    await settle_retirements()
    meta = user_rows(state)[0]["meta"]
    assert "waitingOnYou" not in meta and "waitingOnYouAnswer" not in meta
    expected = ["first", "second"] if change == "replaced" else ["second"]
    assert sorted(a["key"] for a in store.asks(SLOT)) == expected
    assert not any(r["reason"] == "message" for r in store.snapshot_for_turn(SLOT)["retired"])


@pytest.mark.asyncio
async def test_a_teammates_claim_retires_nothing_and_stamps_nothing(state, store):
    from kiro_crew.shared_crew.identity import local_operator_principal

    pinned = await pin(store, ask())
    teammate = local_operator_principal("teammate-ann").to_public_dict()
    claim = {"key": "scope-override", "ask_id": pinned["ask_id"]}
    state._slots[SLOT].append(
        "user",
        "Hold",
        "msg msg-u",
        meta={HUMAN_TURN_META_KEY: True, "author": teammate, "waitingOnYouAnswer": claim},
    )
    await settle_retirements()
    meta = user_rows(state)[0]["meta"]
    assert "waitingOnYou" not in meta and "waitingOnYouAnswer" not in meta
    assert [a["key"] for a in store.asks(SLOT)] == ["scope-override"]


@pytest.mark.asyncio
async def test_a_client_cannot_write_the_reserved_stamp(state, store):
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    forged = {"sendId": "wa-forged", "waitingOnYou": {"key": "first", "ask_id": first["ask_id"]}}
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat?ws=1", json={"slot": SLOT, "message": "Hold", "meta": forged}
            )
            assert resp.status == 200
    await settle_retirements()
    assert "waitingOnYou" not in user_rows(state)[0]["meta"]
    assert store.asks(SLOT) == []


@pytest.mark.asyncio
async def test_a_queued_answer_keeps_its_ask_pinned_until_the_drain_writes_its_row(state, store):
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            status, body = await _answer(client, "Hold", "first", first["ask_id"])
    assert status in (200, 202) and body.get("queued") is True
    queued = state._slots[SLOT]._queue
    assert [e["content"] for e in queued] == ["Hold"]
    assert queued[0]["meta"]["waitingOnYouAnswer"] == {"key": "first", "ask_id": first["ask_id"]}
    assert [a["key"] for a in store.asks(SLOT)] == ["first", "second"]
    await _drain(state)
    assert [a["key"] for a in store.asks(SLOT)] == ["second"]
    assert user_rows(state)[0]["meta"]["waitingOnYou"]["ask_id"] == first["ask_id"]


@pytest.mark.asyncio
async def test_a_queued_answer_claim_is_exposed_on_push_hydration_and_edit_without_other_meta(
    state, store
):
    from kiro_crew.dashboard.chat_delivery import queue_entry_view
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_queue_edit

    pinned = await pin(store, ask("first"))
    claim = {"key": "first", "ask_id": pinned["ask_id"]}
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            await _answer(client, "Hold", "first", pinned["ask_id"])

    entry = state._slots[SLOT]._queue[0]
    entry["meta"]["privateServerMeta"] = "must not cross the wire"
    push = next(
        call.args[1] for call in state.broadcast_ws.call_args_list if call.args[0] == "queue_push"
    )
    assert push["waiting_on_you_answer"] == claim
    assert push.get("meta") == {"sendId": "wa-first-4"}
    view = queue_entry_view(entry)
    assert view["waiting_on_you_answer"] == claim
    assert view.get("meta") == {"sendId": "wa-first-4"}

    app = make_app(state)
    app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
    state.broadcast_ws.reset_mock()
    async with TestClient(TestServer(app)) as client:
        response = await client.patch(
            f"/api/chat/slots/{SLOT}/queue/{entry['id']}", json={"content": "Still hold"}
        )
        assert response.status == 200, await response.text()
    edit = next(
        call.args[1] for call in state.broadcast_ws.call_args_list if call.args[0] == "queue_edit"
    )
    # Edited words differ from the option the card offered: the claim is dropped,
    # so the edit's broadcast names no ask and the card is answerable again.
    assert "waiting_on_you_answer" not in edit
    assert "meta" not in edit

    malformed = queue_entry_view(
        {
            "id": "q-bad",
            "content": "Hold",
            "meta": {
                "waitingOnYouAnswer": {"key": "first", "ask_id": "bad", "extra": "x"},
                "privateServerMeta": "must not cross the wire",
            },
        }
    )
    assert "waiting_on_you_answer" not in malformed
    assert "meta" not in malformed


@pytest.mark.asyncio
async def test_cancelling_a_queued_answer_keeps_its_ask_pinned(state, store):
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_queue_cancel

    first = await pin(store, ask("first"))
    app = make_app(state)
    app.router.add_delete("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_cancel)
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with TestClient(TestServer(app)) as client:
            await _answer(client, "Hold", "first", first["ask_id"])
            qid = state._slots[SLOT]._queue[0]["id"]
            resp = await client.delete(f"/api/chat/slots/{SLOT}/queue/{qid}")
            assert resp.status == 200, await resp.text()
    assert state._slots[SLOT]._queue == []
    await _drain(state)
    assert [a["key"] for a in store.asks(SLOT)] == ["first"]
    assert user_rows(state) == []


@pytest.mark.asyncio
async def test_a_second_answer_to_an_ask_already_answered_in_the_queue_is_refused(state, store):
    """Two windows answer one ask while the agent is busy: only the first is queued."""
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            one = await _answer(client, "Hold", "first", first["ask_id"])
            two = await _answer(client, "Ship it", "first", first["ask_id"])
    assert one[0] in (200, 202)
    assert (two[0], two[1].get("code")) == (409, "waiting_on_you_stale")
    assert len(state._slots[SLOT]._queue) == 1
    await _drain(state)
    assert [a["key"] for a in store.asks(SLOT)] == ["second"]
    assert [r["content"] for r in user_rows(state)] == ["Hold"]


@pytest.mark.asyncio
async def test_an_answer_queued_by_another_window_during_admission_is_refused(state, store):
    """The check before admission passes, then another window's answer to the same
    ask is queued while this send waits in admission: the check repeated just before
    the append refuses this one, so the queue holds one answer to the ask."""
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn
    from kiro_crew.shared_crew import admission_entry

    first = await pin(store, ask("first"))
    real_admit = admission_entry.admit_queued_send
    claim = {"waitingOnYouAnswer": {"key": "first", "ask_id": first["ask_id"]}}

    async def admit_after_another_window(*args, **kwargs):
        queue_for_next_turn(state, state._slots[SLOT], "Hold", waiting_on_you=claim)
        return await real_admit(*args, **kwargs)

    with (
        patch(_RUN_CHAT, new=AsyncMock()),
        patch(_PAUSED, return_value=True),
        patch.object(admission_entry, "admit_queued_send", new=admit_after_another_window),
    ):
        async with _client(state) as client:
            status, body = await _answer(client, "Ship it", "first", first["ask_id"])
    assert (status, body.get("code")) == (409, "waiting_on_you_stale")
    assert [item["content"] for item in state._slots[SLOT]._queue] == ["Hold"]


@pytest.mark.asyncio
async def test_an_answer_whose_row_landed_blocks_a_second_before_its_retirement_saves(state, store):
    """The first answer's row is written and its retirement is still being saved: a
    second answer to the same ask is refused, and a second row cannot claim it."""
    from kiro_crew.shared_crew.identity import local_operator_principal

    first = await pin(store, ask("first"))
    claim = {"waitingOnYouAnswer": {"key": "first", "ask_id": first["ask_id"]}}
    owner = local_operator_principal("local-app").to_public_dict()
    owner_row = {HUMAN_TURN_META_KEY: True, "author": owner, **claim}
    stamped = events.on_live_user_row(state, SLOT, dict(owner_row))
    assert stamped.get("waitingOnYou", {}).get("ask_id") == first["ask_id"]
    assert events.stale_answer_reason(state, state._slots[SLOT], claim)
    again = events.on_live_user_row(state, SLOT, dict(owner_row))
    assert "waitingOnYou" not in again
    await settle_retirements()
    assert store.asks(SLOT) == []
    assert not events._ANSWERING


@pytest.mark.asyncio
async def test_a_second_answer_during_an_immediate_answers_admission_is_refused(state, store):
    """The agent is idle, so the first answer is sent at once; while it waits in
    admission a second answer to the same ask arrives: only one row is written."""
    from kiro_crew.shared_crew import admission_entry

    first = await pin(store, ask("first"))
    real_admit = admission_entry.admit_chat_turn
    seen: list[tuple[int, dict]] = []

    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:

            async def admit_while_another_window_answers(*args, **kwargs):
                if not seen:
                    seen.append(await _answer(client, "Ship it", "first", first["ask_id"]))
                return await real_admit(*args, **kwargs)

            with patch.object(
                admission_entry, "admit_chat_turn", new=admit_while_another_window_answers
            ):
                status, _ = await _answer(client, "Hold", "first", first["ask_id"])
    assert status == 200
    assert [(code, body.get("code")) for code, body in seen] == [(409, "waiting_on_you_stale")]
    assert [row["content"] for row in user_rows(state)] == ["Hold"]
    assert not events._IN_FLIGHT


@pytest.mark.asyncio
async def test_a_card_answer_never_takes_the_responsive_path(state, store):
    """Even when the slot would take a responsive send, an answer goes the normal way
    so its row carries the claim and names the ask."""
    from kiro_crew.dashboard import responsive_chat

    first = await pin(store, ask("first"))
    with (
        patch(_RUN_CHAT, new=AsyncMock()),
        patch.object(responsive_chat, "eligible", return_value=True),
        patch.object(responsive_chat, "accept", new=AsyncMock()) as accept,
    ):
        async with _client(state) as client:
            status, _ = await _answer(client, "Hold", "first", first["ask_id"], responsive=True)
    assert status == 200
    accept.assert_not_called()
    assert user_rows(state)[0]["meta"]["waitingOnYou"]["ask_id"] == first["ask_id"]


@pytest.mark.asyncio
async def test_a_plain_message_queued_before_an_answer_leaves_its_ask_for_the_answer(state, store):
    """Owner queues a plain message, then answers a card: the plain row retires the
    other asks, and the answer's own row retires and names its ask."""
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            plain = await client.post(
                "/api/chat?ws=1",
                json={"slot": SLOT, "message": "hey", "meta": {"sendId": "plain-1"}},
            )
            assert plain.status in (200, 202), await plain.text()
            await _answer(client, "Hold", "first", first["ask_id"])
    await _drain(state)
    await settle_retirements()
    assert [a["key"] for a in store.asks(SLOT)] == ["first"]
    await _drain(state)
    await settle_retirements()
    rows = user_rows(state)
    assert [r["content"] for r in rows] == ["hey", "Hold"]
    assert rows[1]["meta"]["waitingOnYou"]["ask_id"] == first["ask_id"]
    assert store.asks(SLOT) == []


@pytest.mark.asyncio
async def test_an_answer_is_refused_while_the_store_is_still_loading(state, store):
    first = await pin(store, ask("first"))
    with (
        patch(_RUN_CHAT, new=AsyncMock()) as run,
        patch.object(type(store), "loaded", new=property(lambda _self: False)),
        patch.object(type(store), "warm", new=AsyncMock()),
    ):
        async with _client(state) as client:
            status, body = await _answer(client, "Hold", "first", first["ask_id"])
    assert (status, body.get("code")) == (409, "waiting_on_you_stale")
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_retirement_keeps_the_ask_marked_answered(state, store):
    from kiro_crew.dashboard.waiting_on_you import StoreUnavailable

    first = await pin(store, ask("first"))
    with (
        patch(_RUN_CHAT, new=AsyncMock()),
        patch.object(type(store), "retire", new=AsyncMock(side_effect=StoreUnavailable())),
    ):
        async with _client(state) as client:
            one = await _answer(client, "Hold", "first", first["ask_id"])
            two = await _answer(client, "Ship it", "first", first["ask_id"])
    assert one[0] == 200
    assert (two[0], two[1].get("code")) == (409, "waiting_on_you_stale")
    assert [r["content"] for r in user_rows(state)] == ["Hold"]
    events._ANSWERING.clear()


@pytest.mark.asyncio
async def test_a_teammates_claim_does_not_change_delivery(state, store):
    """A claim from anyone but the owner is dropped before it can change delivery, so
    a claim naming no live ask is not even refused: it is never looked at."""
    await pin(store, ask("first"))
    with (
        patch(_RUN_CHAT, new=AsyncMock()),
        patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ),
    ):
        async with _client(state) as client:
            status, body = await _answer(client, "Hold", "first", "wy-" + "1" * 16)
    assert status == 200, body
    assert "waitingOnYouAnswer" not in (user_rows(state)[0].get("meta") or {})


@pytest.mark.asyncio
async def test_editing_a_queued_answer_turns_it_into_an_ordinary_message(state, store):
    from kiro_crew.dashboard.chat_delivery import waiting_on_you_answer_view

    first = await pin(store, ask("first"))
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            await _answer(client, "Hold", "first", first["ask_id"])
    slot = state._slots[SLOT]
    qid = slot._queue[0]["id"]
    assert slot.queue_edit_by_id(qid, "Hold until Monday", directive_user_origin=True)
    assert "waitingOnYouAnswer" not in (slot._queue[0].get("meta") or {})
    assert not waiting_on_you_answer_view(slot._queue[0].get("meta"))


@pytest.mark.asyncio
async def test_an_answer_to_an_ask_no_longer_pinned_is_refused_before_it_is_sent(state, store):
    first = await pin(store, ask("first"))
    await store.retire(SLOT, first["ask_id"], "answered")
    with patch(_RUN_CHAT, new=AsyncMock()) as run:
        async with _client(state) as client:
            status, body = await _answer(client, "Proceed", "first", first["ask_id"])
    assert (status, body.get("code")) == (409, "waiting_on_you_stale")
    assert user_rows(state) == []
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_stop_that_discards_the_queue_keeps_the_ask_pinned(state, store):
    first = await pin(store, ask("first"))
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            await _answer(client, "Hold", "first", first["ask_id"])
    # A hard Stop takes the queue and empties it (``turn_stop``); no row is written.
    state._slots[SLOT]._queue.clear()
    await _drain(state)
    assert [a["key"] for a in store.asks(SLOT)] == ["first"]


@pytest.mark.asyncio
async def test_two_queued_answers_that_share_a_label_each_name_their_own_ask(state, store):
    first = await pin(store, ask("first"))
    second = await pin(store, ask("second"))
    with patch(_RUN_CHAT, new=AsyncMock()), patch(_PAUSED, return_value=True):
        async with _client(state) as client:
            await _answer(client, "Hold", "first", first["ask_id"])
            await _answer(client, "Hold", "second", second["ask_id"])
    assert len(state._slots[SLOT]._queue) == 2
    await _drain(state)
    await _drain(state)
    stamps = [r["meta"]["waitingOnYou"]["key"] for r in user_rows(state)]
    assert stamps == ["first", "second"]
    assert store.asks(SLOT) == []
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [
        ("first", "answered"),
        ("second", "answered"),
    ]


@pytest.mark.asyncio
async def test_a_card_answer_never_steers_into_a_running_turn(state, store):
    pinned = await pin(store, ask())
    steer = AsyncMock()
    with (
        patch(_RUN_CHAT, new=AsyncMock()),
        patch(_PAUSED, return_value=True),
        patch("kiro_crew.dashboard.chat_handlers.steer_into_running_turn", new=steer),
        patch.object(type(state._slots[SLOT]), "turn_running", True),
    ):
        async with _client(state) as client:
            status, body = await _answer(
                client, "Hold", "scope-override", pinned["ask_id"], steer=True
            )
    assert steer.await_count == 0
    assert body.get("queued") is True, (status, body)
    assert [a["key"] for a in store.asks(SLOT)] == ["scope-override"]


@pytest.mark.asyncio
async def test_a_typed_send_still_reads_its_first_word_as_a_command(state, store):
    with patch(_RUN_CHAT, new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post("/api/chat?ws=1", json={"slot": SLOT, "message": "/clear"})
    assert resp.status == 200
    assert "_commands_off" not in run.await_args.kwargs


@pytest.mark.asyncio
async def test_a_dismissal_removes_the_ask_and_keeps_a_retired_record(state, store):
    pinned = await pin(store, ask())
    async with _client(state) as client:
        resp = await client.post(
            DISMISS.format(key="scope-override"), json={"ask_id": pinned["ask_id"]}
        )
        first = (resp.status, await resp.json())
        again = await client.post(
            DISMISS.format(key="scope-override"), json={"ask_id": pinned["ask_id"]}
        )
        second = (again.status, (await again.json())["code"])
    assert first == (200, {"ok": True})
    assert second == (409, "cleared")
    assert store.asks(SLOT) == []
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [("scope-override", "dismissed")]
    assert [r["meta"]["card"]["status"] for r in waiting_rows(state)] == ["dismissed"]


@pytest.mark.asyncio
async def test_an_app_token_is_refused_on_the_dismiss_route(state, store):
    pinned = await pin(store, ask())
    async with _client(state, app_name="some-app") as client:
        resp = await client.post(
            DISMISS.format(key="scope-override"), json={"ask_id": pinned["ask_id"]}
        )
    assert resp.status == 403
    assert len(store.asks(SLOT)) == 1


@pytest.mark.asyncio
async def test_a_caller_who_is_not_the_owner_is_refused_a_dismissal(state, store):
    pinned = await pin(store, ask())
    async with _client(state, user="someone-else") as client:
        resp = await client.post(
            DISMISS.format(key="scope-override"), json={"ask_id": pinned["ask_id"]}
        )
    assert resp.status == 403
    assert len(store.asks(SLOT)) == 1


@pytest.mark.asyncio
async def test_every_dismiss_refusal_names_its_contract_code_and_a_sentence(state, store):
    pinned = await pin(store, ask())
    async with _client(state) as client:
        cases = {
            "invalid_answer": await client.post(
                DISMISS.format(key="scope-override"),
                data="[1]",
                headers={"Content-Type": "application/json"},
            ),
            "cleared": await client.post(
                DISMISS.format(key="never-pinned"), json={"ask_id": pinned["ask_id"]}
            ),
        }
        await pin(store, {**ask(), "title": "replaced"})
        cases["superseded"] = await client.post(
            DISMISS.format(key="scope-override"), json={"ask_id": pinned["ask_id"]}
        )
        bodies = {code: (resp.status, await resp.json()) for code, resp in cases.items()}
    for code, (status, body) in bodies.items():
        assert status in (400, 409), code
        assert body["ok"] is False and body["code"] == code
        assert isinstance(body["error"], str) and body["error"].strip(), code
