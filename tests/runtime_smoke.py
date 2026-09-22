#!/usr/bin/env python3
"""Compatibility smoke test against the real AstrBot 4.25.5 runtime.

This is intentionally separate from ``test_core.py``.  The unit tests use
small AstrBot fakes so they can run without AstrBot; this script imports the
actual AstrBot source selected by ``ASTRBOT_SOURCE`` and only fakes the
outbound context method so no platform or provider is contacted.

The error-path scenario calls the real Daily Care wake event's ``send`` method
with a real AstrBot ``MessageChain``.  This is the same transport call used by
AstrBot's ``InternalAgentSubStage._send_llm_error_message``; constructing the
whole agent pipeline would add unrelated provider/database fixtures without
increasing coverage of the transport firewall.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", "")).expanduser().resolve()

if not ASTRBOT_SOURCE.is_dir():
    raise SystemExit("ASTRBOT_SOURCE must point to a checked-out AstrBot source tree")

sys.path.insert(0, str(ASTRBOT_SOURCE))
sys.path.insert(1, str(REPO_ROOT.parent))

import tomllib

from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.message_session import MessageSession

from astrbot_plugin_daily_care import main as plugin_main
from astrbot_plugin_daily_care.core.wake import _build_daily_care_wake_event


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_runtime_version() -> None:
    with (ASTRBOT_SOURCE / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    _assert(project.get("version") == "4.25.5", "AstrBot pyproject version is not 4.25.5")
    tag = subprocess.check_output(
        ["git", "-C", str(ASTRBOT_SOURCE), "describe", "--tags", "--exact-match", "HEAD"],
        text=True,
    ).strip()
    _assert(tag == "v4.25.5", f"AstrBot checkout is {tag!r}, not v4.25.5")
    print("PASS: AstrBot version 4.25.5")


def _assert_standard_plugin_data_path() -> None:
    original_root = os.environ.get("ASTRBOT_ROOT")
    with tempfile.TemporaryDirectory(prefix="daily_care_runtime_data_") as root:
        os.environ["ASTRBOT_ROOT"] = root
        try:
            data_dir = Path(plugin_main._resolve_data_dir()).resolve()
        finally:
            if original_root is None:
                os.environ.pop("ASTRBOT_ROOT", None)
            else:
                os.environ["ASTRBOT_ROOT"] = original_root
        expected = (Path(root) / "data" / "plugin_data" / "astrbot_plugin_daily_care").resolve()
        _assert(data_dir == expected, f"unexpected plugin data path: {data_dir}")
    print("PASS: standard AstrBot plugin data path")


class CountingContext:
    def __init__(self, result: object = True):
        self.result = result
        self.calls: list[tuple[object, MessageChain]] = []

    async def send_message(self, session: object, message: MessageChain) -> object:
        self.calls.append((session, message))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _new_event(event_cls, context: CountingContext, *, outcome=None, delivered=""):
    session = MessageSession.from_str("ci:FriendMessage:42")
    event = event_cls(
        context=context,
        session=session,
        message="",
        extras={
            "enable_streaming": False,
            "daily_care": {"kind": "wake", "wake_id": "runtime-smoke"},
        },
        message_type=session.message_type,
    )
    if outcome is not None:
        event.set_extra("daily_care_outcome", outcome)
    if delivered:
        event.set_extra("daily_care_delivered_text", delivered)
    return event


async def _run_smoke() -> None:
    event_cls = _build_daily_care_wake_event(CronMessageEvent)
    _assert(issubclass(event_cls, CronMessageEvent), "wake event does not inherit CronMessageEvent")
    _assert(event_cls.__name__ == "DailyCareWakeEvent", "unexpected wake event class name")
    print("PASS: CronMessageEvent inheritance")

    # C1 / E: This is the real AstrBot CronMessageEvent subclass and real
    # MessageChain path used by InternalAgentSubStage error handling.
    error_context = CountingContext()
    error_event = _new_event(event_cls, error_context)
    await error_event.send(
        MessageChain([Plain("Error occurred while processing agent request: test")])
    )
    _assert(not error_context.calls, "unauthorized framework error reached platform")
    _assert(error_event.get_extra("daily_care_platform_sent") is not True, "error marked sent")
    _assert(error_event.get_extra("daily_care_outcome") == "invalid", "error was not invalid")
    _assert(error_event.get_result().chain == [], "rejected error remained in event result")
    print("PASS: unauthorized framework error blocked")

    # C2 / D: MessageEventResult is the real RespondStage result and is a
    # MessageChain, so the exact authorized body must pass the firewall.
    valid_context = CountingContext(True)
    valid_event = _new_event(
        event_cls,
        valid_context,
        outcome="send",
        delivered="午饭吃了吗？",
    )
    result = MessageEventResult().message("午饭吃了吗？")
    _assert(isinstance(result, MessageChain), "MessageEventResult is not a MessageChain")
    valid_event.set_result(result)
    await valid_event.send(result)
    _assert(len(valid_context.calls) == 1, "exact SEND did not call platform once")
    _assert(valid_event.get_extra("daily_care_platform_sent") is True, "exact SEND not marked sent")
    _assert(
        valid_event.get_extra("daily_care_transport_consumed") is True,
        "exact SEND missing consumed marker",
    )
    print("PASS: exact SEND allowed")
    print("PASS: MessageEventResult compatibility")

    # C3: The second event.send call must not enter Context.send_message.
    await valid_event.send(MessageChain([Plain("午饭吃了吗？")]))
    _assert(len(valid_context.calls) == 1, "duplicate SEND reached platform")
    print("PASS: duplicate SEND blocked")

    # C4: A body mismatch is not authorized even if the outcome says send.
    mismatch_context = CountingContext(True)
    mismatch_event = _new_event(
        event_cls,
        mismatch_context,
        outcome="send",
        delivered="午饭吃了吗？",
    )
    await mismatch_event.send(MessageChain([Plain("晚饭吃了吗？")]))
    _assert(not mismatch_context.calls, "mismatched SEND reached platform")
    print("PASS: mismatched SEND blocked")

    # C5: Any additional real AstrBot component invalidates the whole chain.
    extra_context = CountingContext(True)
    extra_event = _new_event(
        event_cls,
        extra_context,
        outcome="send",
        delivered="午饭吃了吗？",
    )
    await extra_event.send(MessageChain([Plain("午饭吃了吗？"), At(name="ci", qq="42")]))
    _assert(not extra_context.calls, "SEND with extra component reached platform")
    print("PASS: additional component blocked")

    # C6: False is not a successful delivery and must not set consumed.
    false_context = CountingContext(False)
    false_event = _new_event(
        event_cls,
        false_context,
        outcome="send",
        delivered="午饭吃了吗？",
    )
    await false_event.send(MessageChain([Plain("午饭吃了吗？")]))
    _assert(false_event.get_extra("daily_care_platform_sent") is False, "False delivery marked sent")
    _assert(
        false_event.get_extra("daily_care_transport_consumed") is not True,
        "False delivery marked consumed",
    )
    print("PASS: platform False handled")

    # C7: A transport exception must not create a successful state marker.
    exception_context = CountingContext(RuntimeError("platform unavailable"))
    exception_event = _new_event(
        event_cls,
        exception_context,
        outcome="send",
        delivered="午饭吃了吗？",
    )
    try:
        await exception_event.send(MessageChain([Plain("午饭吃了吗？")]))
    except RuntimeError as error:
        _assert(str(error) == "platform unavailable", "unexpected platform exception")
    else:
        raise AssertionError("platform exception was swallowed")
    _assert(exception_event.get_extra("daily_care_platform_sent") is False, "exception marked sent")
    _assert(
        exception_event.get_extra("daily_care_transport_consumed") is not True,
        "exception marked consumed",
    )
    print("PASS: platform exception handled")


def main() -> None:
    _assert_runtime_version()
    _assert(callable(plugin_main.DailyCarePlugin), "DailyCarePlugin import failed")
    print("PASS: plugin import")
    _assert_standard_plugin_data_path()
    asyncio.run(_run_smoke())


if __name__ == "__main__":
    main()
