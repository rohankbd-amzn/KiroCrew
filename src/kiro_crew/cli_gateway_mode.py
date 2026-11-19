"""Live identity for the CLI's home-scoped gateway mode decision."""

import time

from kiro_crew.config.paths import config_dir
from kiro_crew.gateway_lock import LockProbeError, lock_holder
from kiro_crew.instances import run_marker

# The clock and the sleep, named so a test can drive the wait without sleeping.
_now = time.monotonic
_sleep = time.sleep


def home_gateway_identity() -> tuple[int, str] | None:
    """Use restart's lock oracle, not a marker or an arbitrary port listener.

    The lock identifies this home's gateway even without a TCP listener. A
    listener's argv alone can name a gateway of another home on the same port.
    An indeterminate probe retains the recorded mode; restart's existing stop
    and spawn guards still decide whether any process may be replaced.
    """
    try:
        holder = lock_holder(config_dir())
    except LockProbeError:
        return None
    if holder.pid is None or not holder.alive:
        return None
    return holder.pid, run_marker.pid_start_token(holder.pid)


def service_gateway_headless(bound_secs: float, interval: float) -> bool:
    """The mode of the gateway a service restart just started.

    The replacement takes ``gateway.lock`` before it boots and publishes its
    record later (an ordinary boot only after its listener is up), so a first
    read can see the new lock holder beside the old record. Until a live
    holder exists and the record names it, the record is pending, not stale:
    poll, up to *bound_secs*. Then decide as for any read with the last
    identity seen. A damaged or unreadable record raises at once.
    """
    deadline = _now() + bound_secs
    while True:
        identity = home_gateway_identity()
        data = run_marker.read_gateway_mode()
        if data is not None:
            _, stale = run_marker.gateway_mode_decision(data, identity)
            if identity is not None and not stale:
                return run_marker.gateway_headless_from(data, identity)
        if _now() >= deadline:
            return run_marker.gateway_headless_from(data, identity)
        _sleep(interval)
