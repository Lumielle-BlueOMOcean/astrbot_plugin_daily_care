#!/usr/bin/env python3
"""AstrBot 4.26.6 hook dispatch smoke for Daily Care wake authorization.

Uses the actual AstrBot source tree selected by ``ASTRBOT_SOURCE`` and its
registry, MainAgentHooks, CronMessageEvent, MessageChain, and Plain classes.
It does not start a bot, contact a provider/platform, or use production data.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import os
import subprocess
import sys
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


class _Context:
    def __init__(self, result=True):
        self.result = result
        self.calls: list[MessageChain] = []

    async def send_message(self, _session, message):
        self.calls.append(message)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


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


async def _test_agent_done_dispatch_survives_other_plugin_stop(plugin_module) -> None:
    expected = "午饭吃了吗？"
    context = _Context()
    event_class = _build_daily_care_wake_event(CronMessageEvent)
    event = _new_event(event_class, context, "agent-done-stop")
    run_context = SimpleNamespace(
        context=SimpleNamespace(event=event),
        messages=[
            SimpleNamespace(role="user", content=[]),
            SimpleNamespace(role="assistant", content=[]),
        ],
    )
    raw = '{"action":"send","message":"午饭吃了吗？"}'
    response = LLMResponse(
        role="assistant",
        completion_text=raw,
    )
    request = SimpleNamespace(func_tool=object(), prompt="")
    await call_event_hook(event, EventType.OnLLMRequestEvent, request)
    _assert(request.func_tool is None, "wake-only request tools were not disabled")
    _assert(event.get_extra("daily_care_tools_disabled") is True, "request hook marker missing")
    stopper, stopper_path = _register_stopper(
        "agent_done", EventType.OnAgentDoneEvent, priority=100
    )
    try:
        await MAIN_AGENT_HOOKS.on_agent_done(run_context, response)
        _assert(
            event.get_extra("daily_care_outcome") == "send",
            "Daily Care authorization hook did not run before another plugin stopped dispatch",
        )
        _assert(
            event.get_extra("daily_care_delivered_text") == expected,
            "authorized body was not bound to the current wake",
        )
        _assert(event.is_stopped(), "the higher-priority hook did not stop propagation as expected")
    finally:
        star_handlers_registry.remove(stopper)
        star_map.pop(stopper_path, None)

    event.set_result(MessageEventResult().message(raw))
    # A stop flag set by OnAgentDone remains on the real AstrMessageEvent.
    # ResultDecorateStage still invokes its first (highest-priority) handler;
    # Daily Care must therefore be first to sanitize the raw envelope.
    await _run_decorating(event)
    chain = event.get_result().chain
    _assert(
        event.get_extra("daily_care_decorating_hook_seen") is True,
        "Daily Care result decorator did not run after an earlier event stop",
    )
    _assert(
        len(chain) == 1 and isinstance(chain[0], Plain) and chain[0].text == expected,
        "decorator did not replace the envelope with the exact authorized body",
    )

    # Independently verify that Daily Care wins ordering when another plugin
    # stops the decorating event itself.
    decorating_context = _Context()
    decorating_event = _new_event(event_class, decorating_context, "decorating-stop")
    await _run_agent_done(decorating_event, raw)
    decorating_event.set_result(MessageEventResult().message(raw))
    decorating_stopper, decorating_stopper_path = _register_stopper(
        "decorating", EventType.OnDecoratingResultEvent, priority=100
    )
    try:
        await _run_decorating(decorating_event)
        _assert(
            decorating_event.get_extra("daily_care_decorating_hook_seen") is True,
            "Daily Care result decorator was not dispatched",
        )
        _assert(decorating_event.is_stopped(), "decorating stopper did not stop propagation")
        chain = decorating_event.get_result().chain
        _assert(
            len(chain) == 1 and isinstance(chain[0], Plain) and chain[0].text == expected,
            "decorator did not replace the envelope with the exact authorized body",
        )
        decorating_event.set_extra("daily_care_committed", True)
        await _run_stage(_respond_stage(), decorating_event)
        _assert(len(decorating_context.calls) == 1, "decorating-stop SEND was not delivered once")
    finally:
        star_handlers_registry.remove(decorating_stopper)
        star_map.pop(decorating_stopper_path, None)

    event.set_extra("daily_care_committed", True)
    await _run_stage(_respond_stage(), event)
    _assert(len(context.calls) == 1, "valid Main Agent SEND was not delivered once")
    _assert(event.get_extra("daily_care_platform_sent") is True, "platform success not recorded")
    _assert(event.get_extra("daily_care_after_sent_hook_seen") is True, "after-send hook was not dispatched")
    await event.send(MessageChain([Plain(expected)]))
    _assert(len(context.calls) == 1, "duplicate transport reached the platform")


async def _test_silent_invalid_disabled_and_transport_fail_closed(plugin_module) -> None:
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
    print("PASS: valid SILENT stays silent through ResultDecorateStage and RespondStage")

    invalid_context = _Context()
    invalid_event = _new_event(event_class, invalid_context, "invalid-json")
    raw_invalid = '{"action":"send","message":'
    await _run_agent_done(invalid_event, raw_invalid)
    _assert(invalid_event.get_extra("daily_care_outcome") == "invalid", "invalid JSON was not fail-closed")
    await _run_decorating(invalid_event)
    _assert(not invalid_event.get_result().chain, "invalid JSON remained in the result")
    await _run_stage(_respond_stage(), invalid_event)
    _assert(not invalid_context.calls, "invalid JSON reached the platform")
    print("PASS: invalid JSON produces no platform message")

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
    request.prompt = "<attachment>"
    request.func_tool = object()
    await call_event_hook(wake_event, EventType.OnLLMRequestEvent, request)
    _assert(request.prompt == "", "attachment sentinel was not removed")
    _assert(request.func_tool is None, "wake tools were not disabled on actual wake request")
    _assert(len(request.extra_user_content_parts) == 1, "wake fact was lost during request hook")
    _assert(request.extra_user_content_parts[0]._no_save is True, "temporary marker was lost during request hook")
    channel.cancel_all()
    await wake_event.send(MessageChain([Plain("取消后不得送达")]))
    _assert(not wake_context.sent, "cancelled wake reached the platform")
    print("PASS: wake facts remain temporary ProviderRequest content with current wake_id")
    print("PASS: request hook preserves temporary facts while applying wake-only safety changes")
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
    asyncio.run(_test_agent_done_dispatch_survives_other_plugin_stop(plugin_module))
    print("PASS: wake-only tools disabled by real OnLLMRequestEvent dispatch")
    print("PASS: MainAgentHooks dispatch preserved wake authorization")
    print("PASS: decorating hook produced exact authorized body")
    print("PASS: exact SEND delivered once; duplicate blocked")
    asyncio.run(_test_silent_invalid_disabled_and_transport_fail_closed(plugin_module))
    asyncio.run(_test_wake_context_reload_and_normal_chat(plugin_module))


if __name__ == "__main__":
    main()
