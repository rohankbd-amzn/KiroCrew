"""What follows a change to the waiting-on-you store, and what changes it besides a route.

Every change is told the same way once it is on disk: one display-only ``card`` row
(surface ``waiting``) in the slot's transcript, so the history shows when an ask came
and went, and the ordinary slots push, which carries the new ``waiting_on_you`` list.
There is no frame of its own: the slot's ``waiting_on_you`` goes out as a
``slot_patch``, and in every full ``slots`` list.

Besides the routes, four things change the store:

* a live row the OWNER typed (``meta.human``, authored by the owner). A card answer is
  one: the card sends its label or note through the ordinary chat send with a claim
  naming its ask (``meta.waitingOnYouAnswer``). When the row is written, a claim that
  names a live ask stamps the reserved ``meta.waitingOnYou`` on it and retires only
  that ask. Any other owner row retires every ask, because the server cannot tell
  which one it answers;
* an edit-resend, which is the owner writing, retires every ask;
* deleting a session removes its records;
* the expiry sweep, every :data:`~kiro_crew.dashboard.waiting_on_you.SWEEP_INTERVAL_SECONDS`.

A rewind or regenerate leaves the asks pinned: the agent still sees them in its block
and clears any that has stopped applying. The agent hears of every change through
:func:`turn_block`, once per retired record.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Awaitable, Callable, Coroutine

from aiohttp import web

from kiro_crew.dashboard import waiting_on_you as store_mod
from kiro_crew.waiting_on_you_contract import ASK_ID_RE, valid_key

logger = logging.getLogger(__name__)

#: Row-meta key of a row that answered an ask: ``{key, ask_id}``, checked against the
#: live asks when the row was written. Reserved (``chat_handlers.RESERVED_ROW_META_KEYS``),
#: so only :func:`on_live_user_row` writes it.
WAITING_ON_YOU_META_KEY = "waitingOnYou"
#: Send-meta key a card answer's claim rides in: ``{key, ask_id}``. Any caller may
#: write it, so it is only a claim until the row is written.
ANSWER_CLAIM_META_KEY = "waitingOnYouAnswer"
#: (slot key, ask id) of answers whose row is written but whose retirement is still
#: being saved. Set and read on the event loop with no await between, so a second
#: answer to the same ask sees the first as soon as its row lands.
_ANSWERING: set[tuple[str, str]] = set()


def answer_claim(user_meta: Any) -> dict[str, Any]:
    """``{waitingOnYouAnswer: {key, ask_id}}`` from a send's meta when well formed, else ``{}``.

    The queue and the sub-agent hold carry it to the row the drain writes.
    """
    claim = user_meta.get(ANSWER_CLAIM_META_KEY) if isinstance(user_meta, dict) else None
    if not isinstance(claim, dict):
        return {}
    key, ask_id = claim.get("key"), claim.get("ask_id")
    if not valid_key(key) or not isinstance(ask_id, str) or not ASK_ID_RE.fullmatch(ask_id):
        return {}
    return {ANSWER_CLAIM_META_KEY: {"key": key, "ask_id": ask_id}}


#: The status a ``waiting`` card row records, by what happened.
ROW_PINNED = "pinned"
ROW_ANSWERED = "answered"
ROW_DISMISSED = "dismissed"
ROW_CLEARED = "cleared"
ROW_EXPIRED = "expired"
ROW_RETIRED = "retired"

_ROW_VERBS = {
    ROW_PINNED: "Pinned",
    ROW_ANSWERED: "Answered",
    ROW_DISMISSED: "Dismissed",
    ROW_CLEARED: "Cleared",
    ROW_EXPIRED: "Expired",
    ROW_RETIRED: "Closed",
}

_SWEEP_TASKS: set[asyncio.Task[None]] = set()
_RETIRE_TASKS: set[asyncio.Task[None]] = set()


async def publish(state: Any, slot_key: str, status: str, asks: list[dict[str, Any]]) -> None:
    """Record *asks* as one ``waiting`` row each and push the slots. Never raises."""
    if asks:
        try:
            from kiro_crew.dashboard.chat_cards import load_conversation_window, record_waiting

            await load_conversation_window(state, {"slot_key": slot_key})
            for ask in asks:
                record_waiting(state, slot_key, status, _ROW_VERBS[status], ask)
        except Exception:
            logger.warning("could not record a waiting-on-you row", exc_info=True)
    push(state, slot_key)


def push(state: Any, slot_key: str) -> None:
    """Tell every tab: a ``slot_patch`` of the slot's ``waiting_on_you``, which also
    sends the full list to a tab that cannot apply a patch. Never raises."""
    try:
        patcher = getattr(state, "push_slot_patch", None)
        if callable(patcher):
            patcher(slot_key, ("waiting_on_you",))
            return
        pusher = getattr(state, "push_slots_update", None)
        if callable(pusher):
            pusher()
    except Exception:
        logger.debug("slots push after a waiting-on-you change failed", exc_info=True)


def retires_asks(meta: dict[str, Any] | None) -> bool:
    """Whether a live user row with *meta* is the owner writing in the chat.

    Only a row a person typed (``meta.human``) whose server-stamped author is the
    owner. A teammate's message, a channel row with an unknown sender and every
    machine row (no ``meta.human``) retire nothing.
    """
    from kiro_crew.history_schema import HUMAN_TURN_META_KEY

    if not isinstance(meta, dict) or meta.get(HUMAN_TURN_META_KEY) is not True:
        return False
    from kiro_crew.shared_crew.attribution import message_author
    from kiro_crew.shared_crew.owner import is_owner_principal

    return is_owner_principal(message_author(meta))


def _live_claim(store: store_mod.WaitingStore, slot_key: str, claim: Any) -> dict[str, str]:
    """``{key, ask_id}`` when *claim* names an ask pinned on *slot_key* now, else ``{}``."""
    named = answer_claim({ANSWER_CLAIM_META_KEY: claim}).get(ANSWER_CLAIM_META_KEY)
    if not named or (slot_key, named["ask_id"]) in _ANSWERING:
        return {}
    for live in store.asks(slot_key):
        if live["key"] == named["key"] and live["ask_id"] == named["ask_id"]:
            return dict(named)
    return {}


def stale_answer_reason(state: Any, slot: Any, user_meta: Any) -> str:
    """Why a card answer send must be refused before it is sent or queued, or ''.

    A card answer whose ask is not pinned, or whose ask already has an answer
    waiting in the queue (another window answered first), would reach the agent as a
    bare "Proceed" or "Done" naming nothing, which the agent could act on twice. The
    check runs on the event loop before the send is admitted, so two sends cannot
    both pass it. The caller loads the store first (:func:`warm_store`); one still
    not loaded refuses the answer, since nothing can say which ask it names.
    """
    named = answer_claim(user_meta).get(ANSWER_CLAIM_META_KEY)
    if not named or state is None:
        return ""
    store = store_mod.store_for(state)
    slot_key = str(getattr(slot, "key", "") or "")
    if not store.loaded:
        return "the asks are still loading; try again"
    if not _live_claim(store, slot_key, named):
        return "this ask is no longer pinned"
    for item in list(getattr(slot, "_queue", None) or []):
        queued = answer_claim(item.get("meta") if isinstance(item, dict) else None)
        if queued.get(ANSWER_CLAIM_META_KEY, {}).get("ask_id") == named["ask_id"]:
            return "an answer to this ask is already queued"
    return ""


async def warm_store(state: Any) -> None:
    """Load the asks before a card answer is checked against them."""
    if state is not None:
        await store_mod.store_for(state).warm()


#: (slot key, ask id) of card answers a send request is carrying right now, from the
#: first check until the request returns. Taken with no await after the check, so of
#: two sends answering one ask only the first proceeds, whatever path each then takes
#: (sent now, queued, held). Once the request returns, the queue entry or
#: ``_ANSWERING`` (the row was written) carries the answer instead.
_IN_FLIGHT: set[tuple[str, str]] = set()
#: Where a send request keeps what :func:`reserve_answer` took, for its release.
RESERVATION_REQUEST_KEY = "waiting_on_you_reservation"


def reserve_answer(slot: Any, user_meta: Any) -> tuple[str, str] | None:
    """Take *user_meta*'s claimed ask for this request; None when another send holds it."""
    named = answer_claim(user_meta).get(ANSWER_CLAIM_META_KEY)
    if not named:
        return None
    reservation = (str(getattr(slot, "key", "") or ""), named["ask_id"])
    if reservation in _IN_FLIGHT:
        return None
    _IN_FLIGHT.add(reservation)
    return reservation


def release_answer(reservation: tuple[str, str] | None) -> None:
    """Give back what :func:`reserve_answer` took, when its request returns."""
    if reservation is not None:
        _IN_FLIGHT.discard(reservation)


def on_live_user_row(state: Any, slot_key: str, meta: dict[str, Any] | None) -> Any:
    """The live-append hook: what the user row about to be written does to the asks.

    Returns the row's meta. The claim never stays on the row: an owner row whose claim
    names a live ask is stamped ``meta.waitingOnYou`` and retires that ask alone; an
    owner row whose well-formed claim names an ask already gone retires nothing; any
    other owner row retires every ask. Rows by anyone else retire nothing. The check
    reads memory, so the stamp is on the row as it is written; the store change is
    scheduled, written, then told.
    """
    if not isinstance(meta, dict):
        return meta
    claim = None
    if ANSWER_CLAIM_META_KEY in meta or WAITING_ON_YOU_META_KEY in meta:
        meta = dict(meta)
        claim = meta.pop(ANSWER_CLAIM_META_KEY, None)
        meta.pop(WAITING_ON_YOU_META_KEY, None)
    if state is None or not retires_asks(meta):
        return meta
    store = store_mod.store_for(state)
    answered = _live_claim(store, slot_key, claim)
    if answered:
        meta[WAITING_ON_YOU_META_KEY] = answered
        _ANSWERING.add((slot_key, answered["ask_id"]))
        _schedule(_retire_answered(state, slot_key, answered["ask_id"]))
    elif answer_claim({ANSWER_CLAIM_META_KEY: claim}):
        # A card answer whose ask is gone (answered from another surface, cleared,
        # replaced) answers nothing now, and retires nothing else either: it was a
        # click on one card, not the owner writing to the agent.
        return meta
    elif not store.loaded or store.asks(slot_key):
        _schedule(_retire_for_message(state, slot_key, _answers_on_the_way(state, slot_key)))
    return meta


def _answers_on_the_way(state: Any, slot_key: str) -> frozenset[str]:
    """Ask ids with an answer queued, being sent, or written but not yet retired."""
    ids = {ask_id for key, ask_id in _IN_FLIGHT | _ANSWERING if key == slot_key}
    slot = (getattr(state, "_slots", None) or {}).get(slot_key)
    for item in list(getattr(slot, "_queue", None) or []):
        queued = answer_claim(item.get("meta") if isinstance(item, dict) else None)
        if queued:
            ids.add(queued[ANSWER_CLAIM_META_KEY]["ask_id"])
    return frozenset(ids)


def _schedule(work: Coroutine[Any, Any, None]) -> None:
    try:
        task = asyncio.get_running_loop().create_task(work)
    except RuntimeError:
        work.close()
        return
    _RETIRE_TASKS.add(task)
    task.add_done_callback(_RETIRE_TASKS.discard)


async def _retire_answered(state: Any, slot_key: str, ask_id: str) -> None:
    """Retire the one ask a row answered. Never raises; the turn it opens retries."""
    try:
        gone = await store_mod.store_for(state).retire(slot_key, ask_id, store_mod.REASON_ANSWERED)
    except Exception:
        # Still marked answered: the ask stays pinned on disk, but a second answer
        # must not reach it before the turn's own retry lands.
        logger.warning("could not retire the answered ask of %s", slot_key, exc_info=True)
        return
    _ANSWERING.discard((slot_key, ask_id))
    if gone:
        await publish(state, slot_key, ROW_ANSWERED, [gone])


async def _retire_for_message(
    state: Any, slot_key: str, keep: frozenset[str] = frozenset()
) -> None:
    store = store_mod.store_for(state)
    try:
        gone = await store.retire_all(slot_key, store_mod.REASON_MESSAGE, keep=keep)
    except Exception:
        logger.warning("could not retire the asks of %s after a message", slot_key, exc_info=True)
        return
    if gone:
        await publish(state, slot_key, ROW_RETIRED, gone)


async def forget_slot(state: Any, slot_key: str) -> None:
    """Remove the records of a deleted session. Never raises."""
    try:
        if await store_mod.store_for(state).forget(slot_key):
            push(state, slot_key)
    except Exception:
        logger.warning("could not remove the asks of deleted %s", slot_key, exc_info=True)


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def retiring_on_success(handler: Handler) -> Handler:
    """*handler* (the edit-resend route) that retires the slot's asks when it succeeds.

    An edit-resend is the owner writing, as a new message is, but its row is written
    on a scratch slot with no author, so the live-append hook never sees it.
    """

    @functools.wraps(handler)
    async def _route(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.handlers.source_providers import (
            is_host_owner_dashboard_request,
        )

        resp = await handler(request)
        slot_key = request.match_info.get("slot", "")
        if 200 <= resp.status < 300 and slot_key and is_host_owner_dashboard_request(request):
            await _retire_for_message(request.app["state"], slot_key)
        return resp

    return _route


def forgetting_on_success(handler: Handler) -> Handler:
    """*handler* (``DELETE /api/sessions/{key}``) that removes the session's asks.

    The slot key is read before the delete, while the session's tab still exists to
    name it.
    """

    @functools.wraps(handler)
    async def _route(request: web.Request) -> web.StreamResponse:
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key

        session_key = request.match_info.get("key", "")
        candidates = {dashboard_slot_key(session_key), session_key.removeprefix("dashboard:")}
        resp = await handler(request)
        if 200 <= resp.status < 300:
            for slot_key in candidates - {""}:
                await forget_slot(request.app["state"], slot_key)
        return resp

    return _route


# ── the expiry sweep ──


async def sweep_once(state: Any) -> None:
    """Expire what is due, then tell each slot. Never raises."""
    try:
        expired = await store_mod.store_for(state).sweep()
    except Exception:
        logger.warning("waiting-on-you sweep failed", exc_info=True)
        return
    for slot_key, asks in expired.items():
        await publish(state, slot_key, ROW_EXPIRED, asks)


async def _sweep_loop(state: Any) -> None:
    from kiro_crew import shutdown_event

    store = store_mod.store_for(state)
    await store.warm()
    for slot_key in store.slots_with_asks():
        push(state, slot_key)
    while not shutdown_event.is_set():
        await sweep_once(state)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=store_mod.SWEEP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def startup(app: web.Application) -> None:
    """The gateway's startup hook: load the store and start the sweep, then return."""
    task = asyncio.ensure_future(_sweep_loop(app["state"]))
    _SWEEP_TASKS.add(task)
    task.add_done_callback(_SWEEP_TASKS.discard)


async def cleanup(_app: web.Application) -> None:
    tasks = list(_SWEEP_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ── what the agent hears ──


def _answered(slot: Any) -> dict[str, str]:
    """``{key, ask_id}`` of the ask the person row that opens this turn answered, if any.

    Only display-only ``card`` rows are passed over. Any other row that is not the
    person's (a reply, a nudge, a sub-agent result, an inject) means no answer
    started this turn, so an older answer is not attached to it.
    """
    for row in reversed(getattr(slot, "messages", None) or []):
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        if role == "card":
            continue
        if role != "user":
            return {}
        stamp = (row.get("meta") or {}).get(WAITING_ON_YOU_META_KEY)
        if isinstance(stamp, dict) and isinstance(stamp.get("ask_id"), str):
            return {"key": str(stamp.get("key") or ""), "ask_id": stamp["ask_id"]}
        return {}
    return {}


async def turn_block(state: Any, slot: Any, *, now: float | None = None) -> str:
    """The ``[WAITING ON YOU]`` block for the turn starting on *slot*, or ``""``.

    Present while an ask is pinned, a retired record is unheard, or the row that opens
    the turn answered an ask. That ask is retired here too, which does nothing when
    the row's own retirement landed and retries it when it did not. The retired
    records are handed over here, once.
    """
    from kiro_crew.context_blocks import render_waiting_on_you

    store = store_mod.store_for(state)
    await store.warm()
    slot_key = str(getattr(slot, "key", "") or "")
    if not slot_key:
        return ""
    answered = _answered(slot)
    if answered:
        await _retire_answered(state, slot_key, answered["ask_id"])
    snapshot = store.snapshot_for_turn(slot_key)
    pinned = store.asks(slot_key)
    if not pinned and not snapshot["retired"] and not answered:
        return ""
    retired = await store.take_retired(slot_key) if snapshot["retired"] else []
    return render_waiting_on_you(
        pinned,
        retired,
        answered=answered.get("key", ""),
        now=time.time() if now is None else now,
    )
