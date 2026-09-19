#!/usr/bin/env python3
"""AstrBot 4.28.0 compatibility smoke for Daily Care wake expiry.

The script imports the actual AstrBot source selected by ``ASTRBOT_SOURCE``.
It exercises real CronMessageEvent/MessageChain transport behavior and the
real per-session lock manager with a fake context transport only.  It does
not start AstrBot, contact QQ/NapCat, call a provider, or require secrets;
full provider/platform E2E remains a deployment-environment check.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", "")).expanduser().resolve()
if not ASTRBOT_SOURCE.is_dir():
    raise SystemExit("ASTRBOT_SOURCE must point to a checked-out AstrBot source tree")

sys.path.insert(0, str(ASTRBOT_SOURCE))
sys.path.insert(1, str(REPO_ROOT.parent))

from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
    InternalAgentSubStage,
)
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.session_lock import session_lock_manager

from astrbot_plugin_daily_care.core.wake import (
    WakeChannel,
    _build_daily_care_wake_event,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_runtime_version() -> None:
    with (ASTRBOT_SOURCE / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    _assert(project.get("version") == "4.28.0", "AstrBot pyproject version is not 4.28.0")
    tag = subprocess.check_output(
        ["git", "-C", str(ASTRBOT_SOURCE), "describe", "--tags", "--exact-match", "HEAD"],
        text=True,
    ).strip()
    _assert(tag == "v4.28.0", f"AstrBot checkout is {tag!r}, not v4.28.0")
    print("PASS: AstrBot version 4.28.0")


class CountingContext:
    def __init__(self, result: object = True):
        self.result = result
        self.calls: list[tuple[object, MessageChain]] = []

    async def send_message(self, session: object, message: MessageChain) -> object:
        self.calls.append((session, message))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _new_event(event_cls, context: CountingContext, wake_id: str, *, outcome=None, delivered=""):
    session = MessageSession.from_str("ci:FriendMessage:42")
    event = event_cls(
        context=context,
        session=session,
        message="",
        extras={
            "enable_streaming": False,
            "daily_care": {"kind": "wake", "wake_id": wake_id},
        },
        message_type=session.message_type,
    )
    if outcome is not None:
        event.set_extra("daily_care_outcome", outcome)
    if delivered:
        event.set_extra("daily_care_delivered_text", delivered)
    return event


async def _run_transport_smoke(event_cls) -> None:
    error_context = CountingContext()
    error_event = _new_event(event_cls, error_context, "error")
    await error_event.send(
        MessageChain([Plain("Error occurred while processing agent request: test")])
    )
    _assert(not error_context.calls, "framework error reached platform")
    _assert(error_event.get_extra("daily_care_outcome") == "invalid", "error not invalid")
    print("PASS: unauthorized framework error blocked")

    valid_context = CountingContext(True)
    valid_event = _new_event(
        event_cls, valid_context, "valid", outcome="send", delivered="午饭吃了吗？"
    )
    result = MessageEventResult().message("午饭吃了吗？")
    _assert(isinstance(result, MessageChain), "MessageEventResult is not MessageChain")
    await valid_event.send(result)
    _assert(len(valid_context.calls) == 1, "exact SEND did not deliver once")
    _assert(valid_event.get_extra("daily_care_platform_sent") is True, "SEND not confirmed")
    _assert(valid_event.get_extra("daily_care_transport_consumed") is True, "SEND not consumed")
    print("PASS: exact SEND allowed")
    print("PASS: MessageEventResult compatibility")

    await valid_event.send(MessageChain([Plain("午饭吃了吗？")]))
    _assert(len(valid_context.calls) == 1, "duplicate SEND delivered twice")
    print("PASS: duplicate SEND blocked")

    mismatch_context = CountingContext(True)
    mismatch_event = _new_event(
        event_cls, mismatch_context, "mismatch", outcome="send", delivered="午饭吃了吗？"
    )
    await mismatch_event.send(MessageChain([Plain("晚饭吃了吗？")]))
    _assert(not mismatch_context.calls, "mismatched SEND reached platform")
    print("PASS: mismatched SEND blocked")

    extra_context = CountingContext(True)
    extra_event = _new_event(
        event_cls, extra_context, "extra", outcome="send", delivered="午饭吃了吗？"
    )
    await extra_event.send(MessageChain([Plain("午饭吃了吗？"), At(name="ci", qq="42")]))
    _assert(not extra_context.calls, "extra component reached platform")
    print("PASS: additional component blocked")

    false_context = CountingContext(False)
    false_event = _new_event(
        event_cls, false_context, "false", outcome="send", delivered="午饭吃了吗？"
    )
    await false_event.send(MessageChain([Plain("午饭吃了吗？")]))
    _assert(false_event.get_extra("daily_care_platform_sent") is False, "False marked sent")
    _assert(false_event.get_extra("daily_care_transport_consumed") is not True, "False consumed")
    print("PASS: platform False handled")

    exception_context = CountingContext(RuntimeError("platform unavailable"))
    exception_event = _new_event(
        event_cls, exception_context, "exception", outcome="send", delivered="午饭吃了吗？"
    )
    try:
        await exception_event.send(MessageChain([Plain("午饭吃了吗？")]))
    except RuntimeError as error:
        _assert(str(error) == "platform unavailable", "unexpected platform exception")
    else:
        raise AssertionError("platform exception was swallowed")
    _assert(exception_event.get_extra("daily_care_platform_sent") is False, "exception marked sent")
    print("PASS: platform exception handled")


async def _run_expiry_and_lock_smoke(event_cls) -> None:
    context = CountingContext(True)
    channel = WakeChannel(context)
    channel.wake_timeout_seconds = 0.01
    umo = "ci:FriendMessage:99"
    first_event = _new_event(event_cls, context, "pending", outcome="send", delivered="过期正文")
    first_event.set_extra("daily_care_tracker", channel)
    _assert(await channel._claim_wake(umo, "pending", first_event), "first wake was not claimed")
    await asyncio.sleep(0.03)
    _assert(umo not in channel._wake_records, "plugin claim was not released")
    _assert(umo in channel._core_pending, "core pending barrier was released too early")

    for wake_id in ("blocked-2", "blocked-3", "blocked-4"):
        later = _new_event(event_cls, context, wake_id)
        _assert(
            not await channel._claim_wake(umo, wake_id, later),
            "expired core wake admitted another Daily Care event",
        )
    print("PASS: expired wake admission bounded")

    lock_acquired = asyncio.Event()

    async def normal_user_turn():
        async with session_lock_manager.acquire_lock(umo):
            lock_acquired.set()

    async with session_lock_manager.acquire_lock(umo):
        waiter = asyncio.create_task(normal_user_turn())
        await asyncio.sleep(0)
        _assert(not waiter.done(), "normal user lock was reentrant")
    await asyncio.wait_for(waiter, timeout=0.5)
    _assert(lock_acquired.is_set(), "normal user did not recover after lock release")
    print("PASS: normal user session lock recovers")

    before = len(context.calls)
    await first_event.send(MessageChain([Plain("过期正文")]))
    _assert(len(context.calls) == before, "expired wake reached platform")
    _assert(first_event.get_extra("daily_care_outcome") == "invalid", "expired wake not invalid")
    print("PASS: expired wake suppressed after recovery")

    first_event.cleanup_temporary_local_files()
    _assert(umo not in channel._core_pending, "terminal wake did not clear barrier")
    print("PASS: expired wake core cleanup released barrier")


async def _run_silent_cleanup_smoke(event_cls) -> None:
    """Verify silent wake cleanup after the real session-lock scope ends."""
    context = CountingContext(True)
    channel = WakeChannel(context)
    umo = "ci:FriendMessage:100"
    event = _new_event(event_cls, context, "silent", outcome="silent")
    event.set_extra("daily_care_tracker", channel)
    _assert(await channel._claim_wake(umo, "silent", event), "silent wake was not claimed")

    async with session_lock_manager.acquire_lock(umo):
        _assert(umo in channel._core_pending, "silent barrier disappeared inside core lock")
        later = _new_event(event_cls, context, "silent-later")
        later.set_extra("daily_care_tracker", channel)
        _assert(
            not await channel._claim_wake(umo, "silent-later", later),
            "later wake entered while silent core event was still active",
        )

    event.cleanup_temporary_local_files()
    event.cleanup_temporary_local_files()
    _assert(umo not in channel._core_pending, "silent cleanup did not clear barrier")

    lock_recovered = asyncio.Event()

    async def normal_user_turn():
        async with session_lock_manager.acquire_lock(umo):
            lock_recovered.set()

    await asyncio.wait_for(normal_user_turn(), timeout=0.5)
    _assert(lock_recovered.is_set(), "normal user lock did not recover after cleanup")
    channel.cancel_all()
    print("PASS: silent cleanup and normal user lock recovery")


async def _run_smoke() -> None:
    event_cls = _build_daily_care_wake_event(CronMessageEvent)
    _assert(issubclass(event_cls, CronMessageEvent), "wake event MRO is incompatible")
    print("PASS: CronMessageEvent inheritance")

    internal_source = inspect.getsource(InternalAgentSubStage.process)
    respond_source = inspect.getsource(RespondStage.process)
    scheduler_source = (
        ASTRBOT_SOURCE / "astrbot/core/pipeline/scheduler.py"
    ).read_text(encoding="utf-8")
    _assert("session_lock_manager.acquire_lock" in internal_source, "4.28 lock scope changed")
    _assert("yield" in internal_source, "4.28 agent stage is not downstream-yielding")
    _assert("OnAfterMessageSentEvent" in respond_source, "4.28 after-send hook path changed")
    _assert(
        "finally" in scheduler_source and "cleanup_temporary_local_files" in scheduler_source,
        "4.28 scheduler cleanup boundary changed",
    )
    print("PASS: 4.28 session lock and after-send hook shape")

    await _run_transport_smoke(event_cls)
    await _run_silent_cleanup_smoke(event_cls)
    await _run_expiry_and_lock_smoke(event_cls)


def main() -> None:
    _assert_runtime_version()
    asyncio.run(_run_smoke())


if __name__ == "__main__":
    main()
