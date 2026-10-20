#!/usr/bin/env python3
"""AstrBot 4.26.6 scheduler and hook smoke for Daily Care wake authorization.

Uses the actual AstrBot source tree selected by ``ASTRBOT_SOURCE`` and its
registry, MainAgentHooks, CronMessageEvent, MessageChain, and Plain classes.
It does not start a bot, contact a provider/platform, or use production data.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import tomllib
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", "")).expanduser().resolve()
if not ASTRBOT_SOURCE.is_dir():
    raise SystemExit("ASTRBOT_SOURCE must point to AstrBot v4.26.6 source")

# Keep the isolated smoke fully offline and avoid AstrBot's background metrics
# worker opening a database after the test event loop has closed.
os.environ.setdefault("ASTRBOT_DISABLE_METRICS", "1")
sys.path.insert(0, str(ASTRBOT_SOURCE))

def _ensure_plugin_namespace() -> None:
    # Reproduce StarManager's plugin module path, which is significant because
    # handler registration and plugin activation are keyed by __module__.
    for name in ("data", "data.plugins"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = []
            sys.modules[name] = module
    package = types.ModuleType("data.plugins.astrbot_plugin_daily_care")
    package.__path__ = [str(REPO_ROOT)]
    sys.modules["data.plugins.astrbot_plugin_daily_care"] = package


_ensure_plugin_namespace()

from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)
from data.plugins.astrbot_plugin_daily_care.core.wake import (
    WakeChannel,
    _build_daily_care_wake_event,
)
from data.plugins.astrbot_plugin_daily_care.core.database import CareDatabase


PLUGIN_MODULE_PATH = "data.plugins.astrbot_plugin_daily_care.main"
PLUGIN_PACKAGE = "data.plugins.astrbot_plugin_daily_care"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _verify_version() -> None:
    with (ASTRBOT_SOURCE / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    tag = subprocess.check_output(
        ["git", "-C", str(ASTRBOT_SOURCE), "describe", "--tags", "--exact-match", "HEAD"],
        text=True,
    ).strip()
    _assert(version == "4.26.6", f"AstrBot version is {version!r}")
    _assert(tag == "v4.26.6", f"AstrBot tag is {tag!r}")
    print("PASS: AstrBot version 4.26.6")


def _load_plugin():
    module = importlib.import_module(f"{PLUGIN_PACKAGE}.main")
    metadata = star_map[PLUGIN_MODULE_PATH]
    plugin = object.__new__(module.DailyCarePlugin)
    metadata.module_path = PLUGIN_MODULE_PATH
    metadata.root_dir_name = "astrbot_plugin_daily_care"
    metadata.activated = True
    metadata.reserved = False
    metadata.name = "astrbot_plugin_daily_care"
    metadata.star_cls = plugin
    metadata.star_cls_type = module.DailyCarePlugin
    for handler in star_handlers_registry.get_handlers_by_module_name(
        PLUGIN_MODULE_PATH,
    ):
        handler.handler = functools.partial(handler.handler, plugin)
    return module, plugin


def _new_event(event_cls, context, wake_id: str):
    session = MessageSession.from_str("aiocqhttp:FriendMessage:42")
    return event_cls(
        context=context,
        session=session,
        message="",
        extras={
            "enable_streaming": False,
            "daily_care": {"kind": "wake", "wake_id": wake_id, "plan_id": 0},
        },
        message_type=session.message_type,
    )


class _AgentCompletionStage:
    """Provider-free agent boundary using real MainAgentHooks dispatch."""

    def __init__(self, raw: str, trace: list[str]):
        self.raw = raw
        self.trace = trace

    async def process(self, event):
        self.trace.append("agent")
        request = SimpleNamespace(func_tool=object(), prompt="")
        await call_event_hook(event, EventType.OnLLMRequestEvent, request)
        response = LLMResponse(role="assistant", completion_text=self.raw)
        run_context = SimpleNamespace(
            context=SimpleNamespace(event=event),
            messages=[
                SimpleNamespace(role="user", content=[]),
                SimpleNamespace(role="assistant", content=[]),
            ],
        )
        await MAIN_AGENT_HOOKS.on_agent_done(run_context, response)
        event.set_result(MessageEventResult().message(response.completion_text))
        self.trace.append("agent_result")
        yield


def _observed_stage(stage, stage_name: str, trace: list[str], observe=None):
    """Observe entry while delegating execution to the real AstrBot stage."""
    original_process = stage.process

    if inspect.isasyncgenfunction(original_process):
        async def process(event):
            trace.append(stage_name)
            if observe is not None:
                observe(event)
            async for item in original_process(event):
                yield item
    else:
        async def process(event):
            trace.append(stage_name)
            if observe is not None:
                observe(event)
            await original_process(event)

    stage.process = process
    return stage


def _actual_scheduler(trace: list[str], raw: str, respond_payloads=None):
    """Use the real scheduler algorithm and real downstream AstrBot stages."""

    def observe_respond(event):
        result = event.get_result()
        respond_payloads.append(
            result.get_plain_text() if result is not None else None
        )

    scheduler = PipelineScheduler.__new__(PipelineScheduler)
    scheduler.stages = [
        _AgentCompletionStage(raw, trace),
        _observed_stage(_decorator_stage(), "decorating", trace),
        _observed_stage(
            _respond_stage(),
            "respond",
            trace,
            observe=observe_respond if respond_payloads is not None else None,
        ),
    ]
    return scheduler


class _SqliteConversationManager:
    """Minimal isolated persistence adapter for Daily Care's conversation API."""

    def __init__(self, db_path: Path, umo: str, conversation_id: str):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE conversation (umo TEXT, cid TEXT, history TEXT, "
                "PRIMARY KEY (umo, cid))"
            )
            conn.execute(
                "INSERT INTO conversation VALUES (?, ?, '[]')",
                (umo, conversation_id),
            )

    async def get_conversation(self, umo: str, conversation_id: str):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT history FROM conversation WHERE umo=? AND cid=?",
                (umo, conversation_id),
            ).fetchone()
        return SimpleNamespace(history=row[0]) if row else None

    async def update_conversation(self, umo: str, conversation_id: str, *, history):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE conversation SET history=? WHERE umo=? AND cid=?",
                (json.dumps(history, ensure_ascii=False), umo, conversation_id),
            )


class _Context:
    def __init__(self, result=True):
        self.result = result
        self.calls: list[MessageChain] = []

    async def send_message(self, _session, message):
        self.calls.append(message)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def _test_real_scheduler_stage_propagation_and_database(plugin) -> None:
    """Exercise Scheduler.execute, hook dispatch, RespondStage, and persistence."""
    expected = "午饭吃了吗？"
    event_class = _build_daily_care_wake_event(CronMessageEvent)

    with tempfile.TemporaryDirectory(prefix="dailycare-4266-scheduler-") as tmp:
        temp_root = Path(tmp)
        care_db = CareDatabase(str(temp_root / "care-data"))
        conversation_id = "isolated-conversation"
        history_db_path = temp_root / "conversation-history.sqlite3"

        def make_context():
            context = _Context()
            context.conversation_manager = _SqliteConversationManager(
                history_db_path,
                "aiocqhttp:FriendMessage:42",
                conversation_id,
            )
            return context

        plugin.db = care_db
        plugin.context = SimpleNamespace()
        plugin._executor = SimpleNamespace(
            _last_send_key=lambda target_id, channel: f"last:{target_id}:{channel}"
        )

        def make_event(context, wake_id):
            event = _new_event(event_class, context, wake_id)
            care = event.get_extra("daily_care")
            care.update(
                {
                    "target_id": 71,
                    "channel": "care",
                    "plan_id": 0,
                    "conversation_id": conversation_id,
                    "umo": event.unified_msg_origin,
                }
            )
            return event

        # No other hook stops propagation: the actual scheduler must send and
        # the real after-send handler must persist both delivery and history.
        context = make_context()
        event = make_event(context, "scheduler-send")
        _assert(event.plugins_name is None, "wildcard plugin scope should leave plugins_name unrestricted")
        trace: list[str] = []
        scheduler = _actual_scheduler(
            trace,
            '{"action":"send","message":"午饭吃了吗？"}',
        )
        await scheduler.execute(event)
        _assert("decorating" in trace and "respond" in trace, "real scheduler skipped a normal downstream stage")
        _assert(len(context.calls) == 1, "real scheduler SEND did not reach platform exactly once")
        _assert(context.calls[0].get_plain_text() == expected, "real scheduler sent a non-body result")
        _assert(event.get_extra("daily_care_committed") is True, "after-send finalizer did not run")
        _assert(event.get_extra("daily_care_platform_sent") is True, "platform delivery was not confirmed")
        with sqlite3.connect(care_db.db_path) as conn:
            send_rows = conn.execute(
                "SELECT target_id, plan_id, content, channel FROM send_log"
            ).fetchall()
        _assert(send_rows == [(71, 0, expected, "care")], f"unexpected isolated send_log rows: {send_rows!r}")
        with sqlite3.connect(history_db_path) as conn:
            history_raw = conn.execute(
                "SELECT history FROM conversation WHERE cid=?",
                (conversation_id,),
            ).fetchone()[0]
        history = json.loads(history_raw)
        assistant_rows = [item for item in history if item.get("role") == "assistant"]
        _assert(len(assistant_rows) == 1, f"history writeback was not exactly once: {history!r}")
        _assert(assistant_rows[0]["content"][0]["text"] == expected, "history contains text other than the exact SEND body")
        _assert(event.get_extra("daily_care_committed") is True, "successful SEND was not committed by the hook")

        # Repeated transport and callback attempts must not duplicate either
        # platform delivery, database log, or persisted assistant history.
        await event.send(MessageChain([Plain(expected)]))
        await plugin._after_message_sent_care_wake(event)
        with sqlite3.connect(care_db.db_path) as conn:
            send_count = conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0]
        with sqlite3.connect(history_db_path) as conn:
            final_history = json.loads(
                conn.execute(
                    "SELECT history FROM conversation WHERE cid=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
        _assert(len(context.calls) == 1, "duplicate transport reached the platform")
        _assert(send_count == 1, "duplicate after-send callback wrote another send_log row")
        _assert(sum(item.get("role") == "assistant" for item in final_history) == 1, "duplicate callback wrote history twice")
        print("PASS: actual PipelineScheduler SEND delivered once and persisted isolated send_log/history")

        # A lower-priority AgentDone handler can stop dispatch after DailyCare
        # has parsed the response. The scheduler's actual stage progression
        # still reaches RespondStage; transport must honor the stop flag.
        agent_context = _Context()
        agent_event = make_event(agent_context, "scheduler-agent-stop")
        agent_trace: list[str] = []
        agent_payloads: list[str | None] = []
        agent_stopper, agent_stopper_path = _register_stopper(
            "scheduler-agent-stop", EventType.OnAgentDoneEvent, priority=100
        )
        try:
            await _actual_scheduler(
                agent_trace,
                '{"action":"send","message":"不得越过 AgentDone stop"}',
                agent_payloads,
            ).execute(agent_event)
        finally:
            star_handlers_registry.remove(agent_stopper)
            star_map.pop(agent_stopper_path, None)
        _assert(agent_event.is_stopped(), "AgentDone stop_event did not persist")
        _assert(agent_event.get_extra("daily_care_agent_done_hook_seen") is True, "DailyCare did not run before lower-priority AgentDone stopper")
        _assert("respond" in agent_trace, "actual PipelineScheduler did not visit RespondStage after AgentDone stop")
        _assert(not agent_context.calls, "transport bypassed an AgentDone stop_event")
        _assert(agent_event.get_extra("daily_care_outcome") == "invalid", "stopped AgentDone wake did not fail closed")
        _assert(agent_event.get_extra("daily_care_platform_sent") is False, "AgentDone stop did not record suppressed transport")
        print("PASS: actual scheduler reaches RespondStage after AgentDone stop; transport suppresses delivery")

        # If an earlier AgentDone hook prevents DailyCare authorization, an
        # eligible decorating hook still clears the raw envelope before the
        # scheduler's outer loop reaches RespondStage. This distinguishes an
        # AgentDone stop alone from the production log pattern where raw JSON
        # was observed at RespondStage.
        early_context = _Context()
        early_event = make_event(early_context, "scheduler-early-agent-stop")
        early_trace: list[str] = []
        early_payloads: list[str | None] = []
        early_stopper, early_stopper_path = _register_stopper(
            "scheduler-early-agent-stop",
            EventType.OnAgentDoneEvent,
            priority=20_000,
        )
        try:
            await _actual_scheduler(
                early_trace,
                '{"action":"send","message":"仅 AgentDone 提前停止"}',
                early_payloads,
            ).execute(early_event)
        finally:
            star_handlers_registry.remove(early_stopper)
            star_map.pop(early_stopper_path, None)
        _assert(early_event.get_extra("daily_care_agent_done_hook_seen") is not True, "earlier stopper did not suppress DailyCare AgentDone hook")
        _assert(early_event.get_extra("daily_care_decorating_hook_seen") is True, "eligible decorating hook did not run after AgentDone stop")
        _assert(early_payloads and early_payloads[0] == "", "eligible decorating hook did not clear raw envelope before RespondStage")
        _assert(not early_context.calls, "early AgentDone stop reached the platform")
        print("PASS: AgentDone stop alone does not leave raw JSON at Respond when decorating hook remains eligible")

        # A lower-priority decorating handler likewise stops after DailyCare
        # has sanitized the chain; the transport firewall must still suppress.
        decorating_context = _Context()
        decorating_event = make_event(decorating_context, "scheduler-decorating-stop")
        decorating_trace: list[str] = []
        decorating_payloads: list[str | None] = []
        decorating_stopper, decorating_stopper_path = _register_stopper(
            "scheduler-decorating-stop", EventType.OnDecoratingResultEvent, priority=100
        )
        try:
            await _actual_scheduler(
                decorating_trace,
                '{"action":"send","message":"不得越过 Decorating stop"}',
                decorating_payloads,
            ).execute(decorating_event)
        finally:
            star_handlers_registry.remove(decorating_stopper)
            star_map.pop(decorating_stopper_path, None)
        _assert(decorating_event.is_stopped(), "decorating stop_event did not persist")
        _assert(decorating_event.get_extra("daily_care_decorating_hook_seen") is True, "DailyCare did not run before lower-priority decorating stopper")
        _assert("respond" in decorating_trace, "actual scheduler did not visit RespondStage after decorating stop")
        _assert(not decorating_context.calls, "transport bypassed a decorating stop_event")
        _assert(decorating_event.get_extra("daily_care_outcome") == "invalid", "stopped decorating wake did not fail closed")
        _assert(decorating_event.get_extra("daily_care_platform_sent") is False, "decorating stop did not record suppressed transport")
        print("PASS: actual scheduler reaches RespondStage after decorating stop; transport suppresses delivery")

        # Silent, malformed and expired outputs run through the same real
        # scheduler/RespondStage path and must never produce a platform send.
        for wake_id, raw, expired, expected_outcome in (
            ("scheduler-silent", '{"action":"silent","message":""}', False, "silent"),
            ("scheduler-invalid", "not-json", False, "invalid"),
            ("scheduler-expired", '{"action":"send","message":"过期正文"}', True, "invalid"),
        ):
            safe_context = _Context()
            safe_event = make_event(safe_context, wake_id)
            if expired:
                safe_event.set_extra("daily_care_expired", True)
            safe_trace: list[str] = []
            await _actual_scheduler(safe_trace, raw).execute(safe_event)
            _assert("respond" in safe_trace, f"{wake_id} did not traverse real RespondStage")
            _assert(not safe_context.calls, f"{wake_id} reached the platform")
            _assert(safe_event.get_extra("daily_care_outcome") == expected_outcome, f"{wake_id} had wrong terminal outcome")
            _assert(safe_event.get_extra("daily_care_finalized") is True, f"{wake_id} did not finalize")
        print("PASS: real scheduler SILENT, invalid, and expired wake paths send nothing")

        # A disabled/filtered authorization hook cannot acquire permission by
        # falling through to the framework's RespondStage.
        handlers = {
            handler.event_type: handler
            for handler in star_handlers_registry
            if handler.handler_module_path == PLUGIN_MODULE_PATH
        }
        agent_handler = handlers[EventType.OnAgentDoneEvent]
        decorating_handler = handlers[EventType.OnDecoratingResultEvent]
        old_enabled = agent_handler.enabled, decorating_handler.enabled
        try:
            agent_handler.enabled = False
            decorating_handler.enabled = False
            disabled_context = _Context()
            disabled_event = make_event(disabled_context, "scheduler-disabled-hooks")
            disabled_trace: list[str] = []
            await _actual_scheduler(
                disabled_trace,
                '{"action":"send","message":"hook 禁用时不得发送"}',
            ).execute(disabled_event)
            _assert("respond" in disabled_trace, "disabled-hook case did not reach RespondStage")
            _assert(not disabled_context.calls, "disabled authorization hooks bypassed transport guard")
            _assert(disabled_event.get_extra("daily_care_outcome") == "invalid", "disabled hooks were not fail-closed")
        finally:
            agent_handler.enabled, decorating_handler.enabled = old_enabled
        print("PASS: real scheduler with disabled authorization hooks fails closed")

        scoped_context = _Context()
        scoped_event = make_event(scoped_context, "scheduler-filtered-hooks")
        scoped_event.plugins_name = ["another_plugin"]
        scoped_trace: list[str] = []
        scoped_payloads: list[str | None] = []
        await _actual_scheduler(
            scoped_trace,
            '{"action":"send","message":"范围过滤时不得发送"}',
            scoped_payloads,
        ).execute(scoped_event)
        _assert("respond" in scoped_trace, "filtered-hook case did not reach RespondStage")
        _assert(
            '{"action":"send"' in (scoped_payloads[0] or ""),
            "filtered hooks did not reproduce the unmodified envelope at RespondStage",
        )
        _assert(not scoped_context.calls, "filtered authorization hooks bypassed transport guard")
        _assert(scoped_event.get_extra("daily_care_outcome") == "invalid", "filtered hooks were not fail-closed")
        print("PASS: event plugin scope filtering fails closed in the actual scheduler")

        with sqlite3.connect(care_db.db_path) as conn:
            final_send_count = conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0]
        _assert(final_send_count == 1, "a stopped/silent/invalid wake changed successful send records")
        with sqlite3.connect(history_db_path) as conn:
            final_history = json.loads(
                conn.execute(
                    "SELECT history FROM conversation WHERE cid=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
        _assert(
            sum(item.get("role") == "assistant" for item in final_history) == 1,
            "a stopped/silent/invalid wake changed successful assistant history",
        )
        print("PASS: suppressed scheduler paths do not add send_log or assistant history")


def _decorator_stage():
    stage = ResultDecorateStage.__new__(ResultDecorateStage)
    stage.content_safe_check_reply = False
    stage.content_safe_check_stage = None
    stage.reply_prefix = ""
    stage.enable_segmented_reply = False
    stage.forward_threshold = 10_000
    stage.reply_with_mention = False
    stage.reply_with_quote = False
    stage.show_reasoning = False
    stage.tts_trigger_probability = 0.0
    stage.t2i_word_threshold = 1_000
    stage.t2i_use_network = False
    stage.t2i_active_template = ""
    stage.ctx = SimpleNamespace(
        plugin_manager=SimpleNamespace(
            context=SimpleNamespace(get_using_tts_provider=lambda _umo: None)
        ),
        astrbot_config={
            "provider_tts_settings": {"enable": False},
            "t2i": False,
        },
    )
    return stage


def _respond_stage():
    stage = RespondStage.__new__(RespondStage)
    stage.config = {}
    stage.platform_settings = {"path_mapping": []}
    stage.enable_seg = False
    stage.only_llm_result = False
    return stage


async def _run_stage(stage, event):
    result = stage.process(event)
    if inspect.isasyncgen(result):
        async for _ in result:
            pass
    else:
        await result


async def _run_agent_done(event, raw: str):
    run_context = SimpleNamespace(
        context=SimpleNamespace(event=event),
        messages=[
            SimpleNamespace(role="user", content=[]),
            SimpleNamespace(role="assistant", content=[]),
        ],
    )
    response = LLMResponse(role="assistant", completion_text=raw)
    await MAIN_AGENT_HOOKS.on_agent_done(run_context, response)
    event.set_result(MessageEventResult().message(raw))
    return run_context, response


async def _run_decorating(event):
    await _run_stage(_decorator_stage(), event)


def _register_stopper(name: str, event_type: EventType, priority: int):
    module_path = f"ci.dailycare_stopper.{name}"
    star_map[module_path] = StarMetadata(
        name=f"CI stopper {name}", author="CI", desc="test", version="0"
    )

    async def stop(event, *_args):
        event.stop_event()

    handler = StarHandlerMetadata(
        event_type=event_type,
        handler_full_name=f"{module_path}_stop",
        handler_name="stop",
        handler_module_path=module_path,
        handler=stop,
        event_filters=[],
        extras_configs={"priority": priority},
    )
    star_handlers_registry.append(handler)
    return handler, module_path


def _register_request_rebuilder():
    module_path = "ci.dailycare_request_rebuilder"
    star_map[module_path] = StarMetadata(
        name="CI request rebuilder", author="CI", desc="test", version="0"
    )

    async def rebuild(_event, request):
        request.system_prompt = "rebuilt system context"
        request.prompt = "<attachment>"

    handler = StarHandlerMetadata(
        event_type=EventType.OnLLMRequestEvent,
        handler_full_name=f"{module_path}_rebuild",
        handler_name="rebuild",
        handler_module_path=module_path,
        handler=rebuild,
        event_filters=[],
        extras_configs={"priority": 100},
    )
    star_handlers_registry.append(handler)
    return handler, module_path


async def _test_transport_and_hook_components_fail_closed(plugin_module) -> None:
    event_class = _build_daily_care_wake_event(CronMessageEvent)
    raw_silent = '{"action":"silent","message":""}'
    silent_context = _Context()
    silent_event = _new_event(event_class, silent_context, "silent")
    await _run_agent_done(silent_event, raw_silent)
    _assert(silent_event.get_extra("daily_care_outcome") == "silent", "silent was not authorized")
    await _run_decorating(silent_event)
    _assert(not silent_event.get_result().chain, "silent produced a user-visible result")
    await _run_stage(_respond_stage(), silent_event)
    _assert(not silent_context.calls, "silent reached the platform")
    _assert(silent_event.get_extra("daily_care_outcome") == "silent", "silent was downgraded")
    _assert(silent_event.get_extra("daily_care_after_sent_hook_seen") is True, "silent lifecycle hook not dispatched")
    print("PASS: component-level SILENT handling stays silent")

    invalid_context = _Context()
    invalid_event = _new_event(event_class, invalid_context, "invalid-json")
    raw_invalid = '{"action":"send","message":'
    await _run_agent_done(invalid_event, raw_invalid)
    _assert(invalid_event.get_extra("daily_care_outcome") == "invalid", "invalid JSON was not fail-closed")
    await _run_decorating(invalid_event)
    _assert(not invalid_event.get_result().chain, "invalid JSON remained in the result")
    await _run_stage(_respond_stage(), invalid_event)
    _assert(not invalid_context.calls, "invalid JSON reached the platform")
    print("PASS: component-level invalid JSON produces no platform message")

    handlers = {
        handler.event_type: handler
        for handler in star_handlers_registry
        if handler.handler_module_path == PLUGIN_MODULE_PATH
    }
    agent_handler = handlers[EventType.OnAgentDoneEvent]
    decorating_handler = handlers[EventType.OnDecoratingResultEvent]
    old_enabled = agent_handler.enabled, decorating_handler.enabled
    try:
        agent_handler.enabled = False
        decorating_handler.enabled = False
        disabled_context = _Context()
        disabled_event = _new_event(event_class, disabled_context, "disabled-hooks")
        raw = '{"action":"send","message":"不能绕过守卫"}'
        await _run_agent_done(disabled_event, raw)
        await _run_decorating(disabled_event)
        _assert(disabled_event.get_extra("daily_care_outcome") is None, "disabled hook unexpectedly ran")
        await disabled_event.send(MessageChain([Plain(raw)]))
        _assert(not disabled_context.calls, "disabled safety hooks bypassed transport guard")
        _assert(disabled_event.get_extra("daily_care_outcome") == "invalid", "disabled hook did not fail closed")
    finally:
        agent_handler.enabled, decorating_handler.enabled = old_enabled
    print("PASS: disabled safety hooks cannot bypass transport guard")

    metadata = star_map[PLUGIN_MODULE_PATH]
    old_activated = metadata.activated
    try:
        metadata.activated = False
        inactive_context = _Context()
        inactive_event = _new_event(event_class, inactive_context, "inactive-plugin")
        raw = '{"action":"send","message":"未激活插件不得授权"}'
        await _run_agent_done(inactive_event, raw)
        await _run_decorating(inactive_event)
        _assert(inactive_event.get_extra("daily_care_outcome") is None, "inactive plugin hook unexpectedly ran")
        await inactive_event.send(MessageChain([Plain(raw)]))
        _assert(not inactive_context.calls, "inactive plugin bypassed transport guard")
        _assert(inactive_event.get_extra("daily_care_outcome") == "invalid", "inactive plugin did not fail closed")
    finally:
        metadata.activated = old_activated
    print("PASS: deactivated plugin hooks cannot bypass transport guard")

    scoped_context = _Context()
    scoped_event = _new_event(event_class, scoped_context, "plugin-scope")
    scoped_event.plugins_name = ["another_plugin"]
    scoped_raw = '{"action":"send","message":"不能绕过过滤"}'
    await _run_agent_done(scoped_event, scoped_raw)
    await _run_decorating(scoped_event)
    await scoped_event.send(MessageChain([Plain(scoped_raw)]))
    _assert(not scoped_context.calls, "plugin allowlist filter was bypassed")
    _assert(scoped_event.get_extra("daily_care_outcome") == "invalid", "filtered hooks did not fail closed")
    print("PASS: plugin-scope filtered hooks fail closed")

    for wake_id, outgoing in (
        ("mismatch", MessageChain([Plain("另一个正文")])),
        ("prefix", MessageChain([Plain("前缀：午饭吃了吗？")])),
        ("extra-plain", MessageChain([Plain("午饭吃了吗？"), Plain("内部说明")])),
        ("extra-component", MessageChain([Plain("午饭吃了吗？"), At(name="CI", qq="42")])),
    ):
        context = _Context()
        event = _new_event(event_class, context, wake_id)
        await _run_agent_done(event, '{"action":"send","message":"午饭吃了吗？"}')
        await _run_decorating(event)
        await event.send(outgoing)
        _assert(not context.calls, f"{wake_id} unauthorized chain reached platform")
        _assert(event.get_extra("daily_care_outcome") == "invalid", f"{wake_id} mismatch not invalid")
    print("PASS: mismatch, prefix, extra Plain, and mixed components are blocked")

    other_wake_context = _Context()
    other_wake_event = _new_event(event_class, other_wake_context, "different-wake")
    await other_wake_event.send(MessageChain([Plain("午饭吃了吗？")]))
    _assert(not other_wake_context.calls, "authorization leaked between wake events")
    _assert(other_wake_event.get_extra("daily_care_outcome") == "invalid", "unrelated wake did not fail closed")
    print("PASS: authorization is event-scoped and cannot leak across wake_id")

    expired_context = _Context()
    expired_event = _new_event(event_class, expired_context, "expired")
    expired_event.set_extra("daily_care_expired", True)
    await _run_agent_done(expired_event, '{"action":"send","message":"过期正文"}')
    await _run_decorating(expired_event)
    await expired_event.send(MessageChain([Plain("过期正文")]))
    _assert(not expired_context.calls, "expired wake was delivered")
    _assert(expired_event.get_extra("daily_care_outcome") == "invalid", "expired wake not invalid")
    print("PASS: expired wake cannot be delivered")

    false_context = _Context(False)
    false_event = _new_event(event_class, false_context, "platform-false")
    await _run_agent_done(false_event, '{"action":"send","message":"未确认"}')
    await _run_decorating(false_event)
    await false_event.send(MessageChain([Plain("未确认")]))
    _assert(false_event.get_extra("daily_care_platform_sent") is False, "platform False marked sent")
    _assert(false_event.get_extra("daily_care_transport_consumed") is not True, "platform False consumed")
    print("PASS: platform False does not authorize success")

    exception_context = _Context(RuntimeError("simulated platform error"))
    exception_event = _new_event(event_class, exception_context, "platform-error")
    await _run_agent_done(exception_event, '{"action":"send","message":"异常"}')
    await _run_decorating(exception_event)
    try:
        await exception_event.send(MessageChain([Plain("异常")]))
    except RuntimeError as error:
        _assert(str(error) == "simulated platform error", "unexpected platform exception")
    else:
        raise AssertionError("platform exception was swallowed")
    _assert(exception_event.get_extra("daily_care_platform_sent") is False, "platform exception marked sent")
    _assert(exception_event.get_extra("daily_care_transport_consumed") is not True, "platform exception consumed")
    print("PASS: platform exception does not authorize success")


async def _test_wake_context_reload_and_normal_chat(plugin_module) -> None:
    class _Queue:
        def __init__(self):
            self.items = []

        async def put(self, event):
            self.items.append(event)

    conversation = SimpleNamespace(cid="ci-conversation", history="[]")
    conversation_manager = SimpleNamespace(
        get_curr_conversation_id=lambda _umo: asyncio.sleep(0, result="ci-conversation"),
        get_conversation=lambda _umo, _cid: asyncio.sleep(0, result=conversation),
    )
    wake_context = SimpleNamespace(
        conversation_manager=conversation_manager,
        queue=_Queue(),
        get_event_queue=lambda: wake_context.queue,
    )
    wake_context.sent = []

    async def send_message(_session, message):
        wake_context.sent.append(message)
        return True

    wake_context.send_message = send_message
    channel = WakeChannel(wake_context)
    ok, wake_id = await channel.wake(
        "aiocqhttp:FriendMessage:42",
        "本轮客观事实",
        channel="care",
        wake_source="care",
    )
    _assert(ok and wake_id, "real WakeChannel failed to enqueue")
    wake_event = wake_context.queue.items[0]
    request = wake_event.get_extra("provider_request")
    _assert(wake_event.get_extra("daily_care")["wake_id"] == wake_id, "wake_id lost")
    _assert(len(request.extra_user_content_parts) == 1, "wake fact part missing")
    _assert(request.extra_user_content_parts[0]._no_save is True, "wake fact was not temporary")
    original_parts = tuple(request.extra_user_content_parts)
    request.func_tool = object()
    rebuilder, rebuilder_path = _register_request_rebuilder()
    try:
        await call_event_hook(wake_event, EventType.OnLLMRequestEvent, request)
    finally:
        star_handlers_registry.remove(rebuilder)
        star_map.pop(rebuilder_path, None)
    _assert(request.prompt == "", "attachment sentinel was not removed")
    _assert(
        request.system_prompt == "rebuilt system context",
        "upstream request reconstruction was not applied",
    )
    _assert(request.func_tool is None, "wake tools were not disabled on actual wake request")
    _assert(
        request.extra_user_content_parts == list(original_parts),
        "wake facts were lost during upstream request reconstruction",
    )
    _assert(
        request.extra_user_content_parts[0]._no_save is True,
        "temporary marker was lost during upstream request reconstruction",
    )
    channel.cancel_all()
    await wake_event.send(MessageChain([Plain("取消后不得送达")]))
    _assert(not wake_context.sent, "cancelled wake reached the platform")
    print("PASS: wake facts remain temporary ProviderRequest content with current wake_id")
    print("PASS: upstream request reconstruction preserves temporary wake facts and Daily Care safety changes")
    print("PASS: cancelled wake cannot be delivered")

    # Simulate AstrBot's active-plugin unbind/reload path using the real manager
    # cleanup API and verify no stale or duplicate handler retains the old instance.
    from astrbot.core.star.star import star_registry
    from astrbot.core.star.star_manager import PluginManager

    metadata = star_map[PLUGIN_MODULE_PATH]
    if metadata not in star_registry:
        star_registry.append(metadata)
    manager = object.__new__(PluginManager)
    for reload_index in range(3):
        await manager._unbind_plugin("astrbot_plugin_daily_care", PLUGIN_MODULE_PATH)
        _ensure_plugin_namespace()
        module = importlib.import_module(f"{PLUGIN_PACKAGE}.main")
        metadata = star_map[PLUGIN_MODULE_PATH]
        metadata.module_path = PLUGIN_MODULE_PATH
        metadata.root_dir_name = "astrbot_plugin_daily_care"
        metadata.activated = True
        metadata.reserved = False
        metadata.name = "astrbot_plugin_daily_care"
        if metadata not in star_registry:
            star_registry.append(metadata)
        plugin_instance = object.__new__(module.DailyCarePlugin)
        metadata.star_cls = plugin_instance
        metadata.star_cls_type = module.DailyCarePlugin
        module_handlers = star_handlers_registry.get_handlers_by_module_name(PLUGIN_MODULE_PATH)
        for handler in module_handlers:
            handler.handler = functools.partial(handler.handler, plugin_instance)
        for event_type, handler_name in (
            (EventType.OnLLMRequestEvent, "_on_llm_request_care_wake"),
            (EventType.OnAgentDoneEvent, "_on_agent_done_care_wake"),
            (EventType.OnDecoratingResultEvent, "_on_decorating_result_care_wake"),
            (EventType.OnAfterMessageSentEvent, "_after_message_sent_care_wake"),
        ):
            entries = [
                item for item in star_handlers_registry
                if item.handler_module_path == PLUGIN_MODULE_PATH
                and item.event_type == event_type
                and item.handler_name == handler_name
            ]
            _assert(len(entries) == 1, f"reload {reload_index} left duplicate/missing {handler_name}")
            _assert(entries[0].handler.args[0] is plugin_instance, f"{handler_name} retained stale plugin instance")
    print("PASS: three real plugin unbind/reload cycles leave one freshly bound safety handler")

    # Ordinary events do not carry Daily Care wake metadata and must preserve
    # normal tools, result text, and the ability to acquire the same session lock.
    ordinary_context = _Context()
    session = MessageSession.from_str("aiocqhttp:FriendMessage:42")
    ordinary_event = CronMessageEvent(
        context=ordinary_context,
        session=session,
        message="普通用户消息",
        extras={},
        message_type=session.message_type,
    )
    ordinary_request = SimpleNamespace(func_tool=object(), prompt="普通消息")
    original_tools = ordinary_request.func_tool
    await call_event_hook(ordinary_event, EventType.OnLLMRequestEvent, ordinary_request)
    _assert(ordinary_request.func_tool is original_tools, "ordinary tools were modified")
    ordinary_context_wrapper = SimpleNamespace(context=SimpleNamespace(event=ordinary_event))
    ordinary_response = LLMResponse(role="assistant", completion_text="正常回复")
    await MAIN_AGENT_HOOKS.on_agent_done(ordinary_context_wrapper, ordinary_response)
    _assert(ordinary_event.get_extra("daily_care_outcome") is None, "ordinary event got wake outcome")
    ordinary_event.set_result(MessageEventResult().message("正常回复"))
    await _run_decorating(ordinary_event)
    _assert(ordinary_event.get_result().get_plain_text() == "正常回复", "ordinary result was modified")

    from astrbot.core.utils.session_lock import session_lock_manager

    acquired = asyncio.Event()

    async def next_user_message():
        async with session_lock_manager.acquire_lock(ordinary_event.unified_msg_origin):
            acquired.set()

    await asyncio.wait_for(next_user_message(), timeout=1)
    _assert(acquired.is_set(), "ordinary user message could not acquire the real session lock")
    print("PASS: ordinary user event and next-user session lock remain unaffected")


def main() -> None:
    _verify_version()
    plugin_module, _plugin = _load_plugin()
    registered = {
        handler.event_type: handler
        for handler in star_handlers_registry
        if handler.handler_module_path == PLUGIN_MODULE_PATH
        and handler.handler_name
        in {
            "_on_llm_request_care_wake",
            "_on_agent_done_care_wake",
            "_on_decorating_result_care_wake",
            "_after_message_sent_care_wake",
        }
    }
    expected = {
        EventType.OnLLMRequestEvent,
        EventType.OnAgentDoneEvent,
        EventType.OnDecoratingResultEvent,
        EventType.OnAfterMessageSentEvent,
    }
    _assert(set(registered) == expected, "Daily Care safety hook registration incomplete")
    _assert(all(item.enabled for item in registered.values()), "hook unexpectedly disabled")
    _assert(star_map[PLUGIN_MODULE_PATH].activated, "plugin is not activated in registry")
    print("PASS: real Daily Care safety hooks registered, enabled, and activated")
    asyncio.run(_test_real_scheduler_stage_propagation_and_database(_plugin))
    print("PASS: wake-only tools disabled by real OnLLMRequestEvent dispatch")
    print("PASS: MainAgentHooks dispatch and real PipelineScheduler stage progression exercised")
    asyncio.run(_test_transport_and_hook_components_fail_closed(plugin_module))
    asyncio.run(_test_wake_context_reload_and_normal_chat(plugin_module))


if __name__ == "__main__":
    main()
