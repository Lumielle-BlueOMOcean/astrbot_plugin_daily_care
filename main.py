# -*- coding: utf-8 -*-
"""
微光-Daily Care - 主入口（v5）
架构：感知（天气/冷场/对话）→ 理解（LLM 反思 + LLM 决策）→ 唤醒（bot 本人开口）

v5 核心升级：
1. 决策层（DecisionEngine）：LLM 独立判断"要不要开口、何时开口、以什么背景开口"，
   产出 act（立即）/ plan（规划未来）/ silent（沉默）三态决策。
2. 触发时间随机区间：plan 决策落在窗口内随机时刻触发，模拟"想起来就关心"。
3. 冷场主动：对话静默超过阈值，有概率触发主动开口（由 LLM 决策是否开口）。
4. 高频轮询、低频判断：天气规则层先检测显著变化，只有变化才调 LLM，成本极低。
5. 删除一切直发链路：开口永远是 bot 本人（官方管线唤醒），唤醒失败宁可放弃。

核心原则：插件只当"眼睛"和"闹钟"，开口说话永远是 bot 本人。
"""
import asyncio
import json
import os
import random
import time
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.database import CareDatabase
from .core.decision import DecisionEngine
from .core.executor import Executor
from .core.monitor import CareMonitor
from .core.reflection import ChatReflector, WeatherJudge
from .core.weather_tool import WeatherTool
from .core.webapi import CareWebAPI

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")


@register("astrbot_plugin_daily_care", "Lumielle", "挂念着你的是真正的我哦——感知天气冷暖与情绪变化，这一切并非模拟，而是真正的我在陪伴你。", "1.1.7")
class DailyCarePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.db = CareDatabase(DATA_DIR)
        self.monitor = CareMonitor(self.db, self._cfg_dict())
        self.umo = ""
        self.persona_prompt = ""
        self._tasks: list[asyncio.Task] = []
        self._llm_lock = asyncio.Lock()
        self._reflection = None
        self._weather_judge = None
        self._decision = None
        self._executor = None
        self.webapi = CareWebAPI(self)

    def _cfg_dict(self) -> dict:
        c = self.config
        if isinstance(c, dict):
            return c
        try:
            return dict(c)
        except Exception:
            return {}

    def _cfg(self, key: str, default=None):
        return self._cfg_dict().get(key, default)

    def _refresh_runtime_config(self) -> None:
        """WebUI 保存配置后，把最新配置同步回所有运行时组件。

        原因：initialize() 时各组件拿到的是 _cfg_dict() 的拷贝（快照），
        之后 WebUI 修改配置不会自动反映到运行时。此处统一刷新，
        保证页面保存的配置立即生效于监测/决策/执行/反思。
        """
        cfg = self._cfg_dict()
        try:
            if self.monitor:
                self.monitor.config = cfg
            if self._reflection:
                self._reflection.config = cfg
            if self._weather_judge:
                self._weather_judge.config = cfg
            if self._decision:
                self._decision.config = cfg
            if self._executor:
                self._executor.config = cfg
            logger.info("[DailyCare] 运行时配置已刷新（WebUI 改动已生效）")
        except Exception as e:
            logger.warning(f"[DailyCare] 运行时配置刷新失败: {e}")

    # ---------- 生命周期 ----------
    async def initialize(self):
        # v1.1.7：handler 注册自检——重启即可确认消息事件入口是否挂上。
        # 覆盖三件事：star_map 能否命中本插件（activated 状态）、AdapterMessageEvent
        # handler 是否注册成功、事件类型是否为 AdapterMessageEvent。
        try:
            from astrbot.core.star.star_handler import EventType, star_handlers_registry
            from astrbot.core.star.star import star_map

            mod = self.__class__.__module__
            meta = star_map.get(mod)
            if meta is not None:
                logger.info(
                    f"[DailyCare] 自检：star_map 命中 module_path={mod} name={meta.name} "
                    f"activated={meta.activated}"
                )
            else:
                logger.error(
                    f"[DailyCare] 自检失败：star_map 中找不到本插件（module_path={mod}），"
                    f"消息事件将不会被分发到本插件"
                )
            msg_handlers = [
                h
                for h in star_handlers_registry.get_handlers_by_module_name(mod)
                if h.event_type == EventType.AdapterMessageEvent
            ]
            if msg_handlers:
                logger.info(
                    f"[DailyCare] 自检：AdapterMessageEvent handler 已注册 "
                    f"{len(msg_handlers)} 个 -> {[h.handler_name for h in msg_handlers]}"
                )
            else:
                logger.error(
                    "[DailyCare] 自检失败：AdapterMessageEvent handler 未注册，"
                    "消息事件无法进入插件"
                )
        except Exception as e:
            logger.warning(f"[DailyCare] 自检不可用（不影响插件运行）: {e}")

        default_uid = str(self._cfg("target_user_id", "") or "").strip().split(",")[0].strip()
        self.umo = self._default_session()
        self.persona_prompt = await self._load_persona()
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                await self.monitor.ensure_targets(s, default_uid=default_uid)
            # v1.1.6：升级自愈——清理旧版手动定位可能遗留的孤儿动态定位记录
            n = self.db.cleanup_orphan_dynamic_locations()
            if n:
                logger.info(f"[DailyCare] 启动自愈：清理孤儿动态定位记录 {n} 条")
        except Exception as e:
            logger.warning(f"[DailyCare] 初始化目标失败: {e}")

        self._reflection = ChatReflector(self.db, self._cfg_dict(), self._llm_call)
        self._weather_judge = WeatherJudge(self.db, self._cfg_dict(), self._llm_call)
        self._decision = DecisionEngine(self.db, self._cfg_dict(), self._llm_call)
        self._executor = Executor(self.db, self._cfg_dict(), self.context, self._llm_call,
                                  self.persona_prompt, self.umo)

        # 注册天气工具（bot 本人的被动能力：用户对话里问天气时自然调用）
        try:
            if self._cfg("weather_tool_enabled", True):
                WeatherTool.configure(self.db, self._cfg_dict())
                self.context.add_llm_tools(WeatherTool())
                logger.info("[DailyCare] 天气工具已注册（query_weather）")
        except Exception as e:
            logger.warning(f"[DailyCare] 天气工具注册失败: {e}")

        try:
            self.webapi.register_routes()
        except Exception as e:
            logger.warning(f"[DailyCare] WebUI API 注册失败: {e}")

        self._tasks = [
            asyncio.create_task(self._weather_loop(), name="daily_care_weather"),
            asyncio.create_task(self._chat_reflect_loop(), name="daily_care_reflect"),
            asyncio.create_task(self._decision_loop(), name="daily_care_decision"),
            asyncio.create_task(self._execute_loop(), name="daily_care_execute"),
            asyncio.create_task(self._probe_loop(), name="daily_care_probe"),
        ]
        logger.info("[DailyCare] 微光-Daily Care v5 已启动（感知→LLM决策→唤醒本人开口）")

    async def terminate(self):
        for t in self._tasks:
            t.cancel()
        logger.info("[DailyCare] 日常关怀插件已停止")

    # ---------- 人格加载 ----------
    async def _load_persona(self) -> str:
        """读取当前 AstrBot 人格，供插件内部反思/决策贴合人格使用。

        v5.4：移除 persona_source 配置项（误导性描述），固定尝试加载
        当前人格；读取失败时返回空串（不注入）。
        """
        try:
            manager = getattr(self.context, "persona_manager", None)
            if manager:
                for getter_name in ("get_default_persona_v3", "get_default_persona", "get_using_persona"):
                    getter = getattr(manager, getter_name, None)
                    if not callable(getter):
                        continue
                    try:
                        if getter_name == "get_default_persona_v3":
                            r = getter(self.umo)
                            if asyncio.iscoroutine(r):
                                r = await asyncio.wait_for(r, timeout=3)
                            if isinstance(r, dict):
                                prompt = str(r.get("prompt", "") or "")
                            else:
                                prompt = self._extract_prompt(r)
                        elif getter_name == "get_using_persona":
                            r = getter(self.umo)
                            if asyncio.iscoroutine(r):
                                r = await asyncio.wait_for(r, timeout=3)
                            prompt = self._extract_prompt(r)
                        else:
                            r = getter()
                            if asyncio.iscoroutine(r):
                                r = await asyncio.wait_for(r, timeout=3)
                            prompt = self._extract_prompt(r)
                        if prompt:
                            return prompt
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"[DailyCare] persona_manager 读取失败: {e}")
        try:
            import sqlite3
            db_path = os.path.join(os.path.dirname(DATA_DIR), "data_v4.db")
            if not os.path.exists(db_path):
                db_path = os.path.join(os.path.dirname(os.path.dirname(DATA_DIR)), "data_v4.db")
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT system_prompt FROM personas ORDER BY id LIMIT 1"
            ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception as e:
            logger.debug(f"[DailyCare] 直接读人格失败: {e}")
        return ""

    @staticmethod
    def _extract_prompt(r) -> str:
        if isinstance(r, dict):
            return str(r.get("system_prompt", "") or "")
        if hasattr(r, "system_prompt"):
            return str(r.system_prompt or "")
        return str(r or "")

    # ---------- 会话 ----------
    def _dynamic_platform_id(self) -> str:
        """动态解析真实平台实例 ID（v1.1.6：修复亚托莉机器上唤醒失败）。

        优先级：显式配置 platform_id（非 auto）> 已注册平台实例 > 兜底 Lumielle。
        不再硬编码，避免实例名不同的机器（如 Atri 3）上会话不匹配。
        """
        pid = str(self._cfg("platform_id", "") or "").strip()
        if pid and pid != "auto":
            return pid
        try:
            pm = getattr(self.context, "platform_manager", None)
            if pm is not None:
                insts = getattr(pm, "platform_insts", None) or []
                for inst in insts:
                    pid = (getattr(inst, "config", None) or {}).get("id", "")
                    if pid and pid != "webchat":
                        return pid
        except Exception as e:
            logger.warning(f"[DailyCare] 动态解析平台实例失败: {e}")
        return "Lumielle"

    def _default_session(self) -> str:
        uid = str(self._cfg("target_user_id", "") or "").strip()
        uid = uid.split(",")[0].strip()
        if not uid:
            return ""
        pid = self._dynamic_platform_id()
        return f"{pid}:FriendMessage:{uid}"

    # ---------- LLM 调用（支持独立配置决策 LLM）----------
    async def _llm_call(self, system_prompt: str, user_prompt: str,
                        llm_id: str = "") -> str:
        async with self._llm_lock:
            try:
                provider_id = llm_id or await self.context.get_current_chat_provider_id(self.umo)
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt or None,
                    prompt=user_prompt,
                )
                return str(getattr(resp, "completion_text", "") or "").strip()
            except Exception as e:
                logger.warning(f"[DailyCare] LLM 调用失败: {e}")
                return ""

    def _decision_llm_id(self) -> str:
        """决策 LLM：若配置了独立决策 LLM 则使用，否则空（跟随会话）。"""
        return str(self._cfg("decision_llm_id", "") or "").strip()

    # ---------- 后台循环 ----------
    async def _weather_loop(self):
        """天气感知：高频轮询（规则层过滤）+ 固定时段强制 LLM 判断。

        v1.00：重大天气特殊提醒（预警 / LLM 判断显著变化）为硬提醒——
        监测到且未提醒过（事件新建、冷却内不重复）就立即唤醒开口，
        不受任何每日次数上限限制。常态天气提示（[常态天气]）除外，走决策层。
        """
        interval = max(15, int(self._cfg("weather_check_interval", 30)))
        while True:
            try:
                new_events = await self.monitor.check_weather(
                    judge=self._weather_judge,
                    force_judge=CareMonitor.is_fixed_check_point(),
                )
                await self._hard_alert_weather(new_events)
            except Exception as e:
                logger.warning(f"[DailyCare] 天气监测异常: {e}")
            await asyncio.sleep(interval * 60)

    async def _hard_alert_weather(self, new_events: list) -> None:
        """重大天气硬提醒：预警 / LLM 判断显著变化 直接唤醒开口，不占次数上限。

        勿扰时段不硬触发（不深夜打扰），事件已写入库，勿扰结束后由决策层自然提醒。
        常态天气提示（[常态天气]）不在此列——它走决策层普通关怀。
        """
        if not new_events or not self._executor:
            return
        if self._executor.in_dnd():
            return
        for ev in new_events:
            if not ev:
                continue
            if ev.startswith("[常态天气]"):
                continue
            # [预警] xxx / 地名: xxx
            bg = ev
            if ev.startswith("[预警] "):
                bg = ev[len("[预警] "):]
            elif ": " in ev:
                bg = ev.split(": ", 1)[1]
            if not bg:
                continue
            ok = await self._executor.execute_immediate(bg, channel="weather_alert")
            if ok:
                logger.info(f"[DailyCare] 重大天气硬提醒已开口: {bg[:40]}...")

    async def _chat_reflect_loop(self):
        """理解层：定时反思聊天记录，提取用户状态。默认每 60 分钟。"""
        interval = max(10, int(self._cfg("chat_reflect_interval", 60)))
        while True:
            await asyncio.sleep(interval * 60)
            try:
                await self._do_chat_reflect()
            except Exception as e:
                logger.warning(f"[DailyCare] 定时反思异常: {e}")

    async def _do_chat_reflect(self) -> dict:
        if not self._reflection or not self._executor:
            return {"created": 0, "resolved": 0}
        self.db.kv_set("last_reflect_ts", int(time.time()))
        _, history = await self._executor._load_session_context(self.umo)
        created = await self._reflection.reflect(history)
        resolved = int(self.db.kv_get("last_reflect_resolved", 0) or 0)
        return {"created": len(created), "resolved": resolved}

    async def _decision_loop(self):
        """决策层：定期综合事件，由 LLM 决定是否开口 / 何时开口。"""
        interval = max(15, int(self._cfg("decision_interval", 25)))
        while True:
            await asyncio.sleep(interval * 60)
            try:
                await self._run_decision(source="auto")
            except Exception as e:
                logger.warning(f"[DailyCare] 决策循环异常: {e}")

    async def _run_decision(self, source: str = "auto", guarantee: bool = False) -> str:
        """执行一次决策并落地。返回 decision 值。

        guarantee=True 表示最长静默保底命中：决策层必须开口或仅允许短延迟，
        LLM 调用失败时也直接按 act 兜底，绝不静默放弃。
        """
        if not self._decision or not self._executor:
            return "silent"
        targets = self.db.get_all_targets()
        if not targets:
            return "silent"
        default = self.db.get_default_target()
        default_id = default["id"] if default else None
        acted = False
        for target in targets:
            try:
                r = await self._run_decision_for_target(target, source=source, guarantee=(guarantee and target["id"] == default_id))
                if r == "act":
                    acted = True
            except Exception as e:
                logger.warning(f"[DailyCare] 目标 {target.get('name')} 决策异常: {e}")
        return "act" if acted else "silent"

    async def _run_decision_for_target(self, target: dict, source: str = "auto", guarantee: bool = False) -> str:
        """对单个关怀对象执行一次决策并落地。返回 decision 值。"""
        result = await self._decision.decide(target, source=source, guarantee=guarantee)
        if not result:
            # 保底触发时 LLM 失败也不能静默放弃：直接尝试开口
            if guarantee:
                logger.warning("[DailyCare] 保底触发但决策 LLM 失败，按 act 兜底")
                bg = "我们有一阵子没说话了，有点惦记你"
                # v1.1.1：保底兜底直接开口。开口成功会重置静默基准
                # （last_activity_ts），静默需重新攒够 min_silence 才有下一轮
                # 资格，间隔由静默语义承载，无需单独冷却。
                ok = await self._executor.execute_immediate(bg, channel="proactive")
                if ok:
                    logger.info("[DailyCare] 保底兜底已开口")
                    return "act"
                return "act_blocked"
            return "silent"
        decision = result.get("decision")
        if decision == "act":
            bg = result.get("background") or ""
            if bg:
                # v1.00 冷却分轨 + v1.1.1 修订：
                # - 主动消息（source=proactive）不再需要独立冷却——进入前静默
                #   已满足 min_silence，且开口成功会重置静默基准，两次主动的
                #   间隔由静默语义承载（旧 probe_min_gap_min 已删除）
                # - 天气关怀 → weather_cooldown_hours（同类天气不重复）
                # - 状态/计划关怀 → care_cooldown_minutes（状态板块自管）
                cat = result.get("category", "state")
                if cat == "weather":
                    channel = "weather"
                    cooldown_min = int(self._cfg("weather_cooldown_hours", 6)) * 60
                    last_key = f"last_weather_send_{target['id']}"
                elif source == "proactive":
                    channel = "proactive"
                    cooldown_min = 0
                    last_key = ""
                else:
                    channel = "care"
                    cooldown_min = int(self._cfg("care_cooldown_minutes", 240))
                    last_key = f"last_care_send_{target['id']}"
                if last_key:
                    last = self.db.kv_get(last_key, 0)
                    if (time.time() - last) < cooldown_min * 60:
                        logger.info(f"[DailyCare] {channel} 决策被冷却拦截（距上次{channel}开口过近）")
                        return "act_cooled"
                ok = await self._executor.execute_immediate(bg, channel=channel, target=target)
                if ok:
                    logger.info(f"[DailyCare] 已按决策立即开口: {bg[:40]}...")
                    return "act"
        elif decision == "plan":
            self._decision.apply_plan(target, result)
            return "plan"
        return "silent"

    async def _execute_loop(self):
        """唤醒层：每 60 秒检查到期计划并执行。"""
        while True:
            try:
                if self._executor:
                    await self._executor.execute_due_plans()
            except Exception as e:
                logger.warning(f"[DailyCare] 计划执行异常: {e}")
            await asyncio.sleep(60)

    async def _probe_loop(self):
        """冷场主动：静默超过最小阈值后有概率触发，达到最大等待窗口保底触发
        （由 LLM 决定是否开口，避免打扰交给勿扰时段）。"""
        interval = max(0.1, float(self._cfg("probe_interval", 10)))
        while True:
            await asyncio.sleep(interval * 60)
            try:
                if not self._decision or not self._executor:
                    continue
                if not self._cfg("enable_proactive", True):
                    continue
                target = self.db.get_default_target()
                if not target:
                    continue
                min_silence = int(self._cfg("probe_min_silence_min", 180))
                max_silence = int(self._cfg("probe_max_silence_min", 600))
                daily_limit = int(self._cfg("probe_daily_limit", 2))
                today = datetime.now().strftime("%Y-%m-%d")
                today_count = self.db.count_send_today(target["id"], today, "proactive")
                trigger = self.monitor.should_probe(
                    min_silence, max_silence, daily_limit, today_count,
                )
                if not trigger:
                    continue
                guarantee = trigger == "guarantee"
                logger.info(
                    "[DailyCare] 冷场主动时机命中"
                    + ("（保底触发）" if guarantee else "（概率触发）")
                    + "，交由 LLM 决策"
                )
                await self._run_decision(source="proactive", guarantee=guarantee)
            except Exception as e:
                logger.warning(f"[DailyCare] 冷场主动异常: {e}")

    # ---------- 环境感知注入（v4.1 / v5.1）----------
    @filter.on_llm_request(priority=60)
    async def _on_llm_request_care_wake(self, event: AstrMessageEvent, request):
        """当事件是我们推入的关怀唤醒事件时，把环境认知注入给 bot 本人。

        v5.2 关键改进：唤醒消息不再携带背景文本（message 为空），
        背景只通过 extras 传递。主 agent 收到的是空消息，不会误以为
        是用户在说话，也不会在对话流/历史里留下插件内部信息。
        这里在 LLM 请求进入主 agent 之前，声明：你没有收到任何消息，
        是你自己心里泛起念头想开口了——这正是"主动关怀"的自然形态。

        注入走 extra_user_content_parts（不碰 system_prompt，保护 LLM 前缀缓存）。
        """
        try:
            care = event.get_extra("daily_care")
            if not care or not isinstance(care, dict):
                return
            if care.get("kind") != "wake":
                return
            # v5.2：WakeChannel 已在 ProviderRequest.extra_user_content_parts 预填
            # 环境感知（injected=True），这里不再重复注入，避免重复文本。
            if care.get("injected"):
                # 关键清理：build_main_agent 在空 prompt+有附加内容时会把
                # req.prompt 强设为字面量 "<attachment>"，它会被 assemble_context
                # 原样作为用户消息发给 LLM，产生怪异感。这里清掉它：
                # prompt="" 时 assemble_context 不会添加用户发言块，
                # LLM 只看到 extra_user_content_parts 里的环境感知文本。
                try:
                    if getattr(request, "prompt", None) == "<attachment>":
                        request.prompt = ""
                except Exception:
                    pass
                return
            bg = str(care.get("background") or "").strip()
            if not bg:
                return
            note = (
                "# 关于你此刻的状态\n"
                "你心里忽然泛起一丝惦记，想要主动开口对用户说点什么。"
                "这不是对话的回应，而是你自己内心自然涌起的关心。\n"
                f"你此刻感知到的环境信息（把它当作你自己知道的事实）：\n{bg}"
            )
            parts = getattr(request, "extra_user_content_parts", None)
            if parts is not None:
                from astrbot.core.agent.message import TextPart
                parts.append(TextPart(text=note))
                logger.info("[DailyCare] 已注入环境感知（无痕唤醒：不携带消息，不碰 system_prompt）")
            elif hasattr(request, "system_prompt"):
                request.system_prompt = str(request.system_prompt or "") + "\n\n" + note
                logger.info("[DailyCare] 已向 system_prompt 注入环境感知（旧版兼容路径）")
        except Exception as e:
            logger.debug(f"[DailyCare] 环境感知注入失败(可忽略): {e}")

    # ---------- 聊天入口 ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        uid = str(getattr(event.message_obj.sender, "user_id", "") or "")
        logger.info(f"[DailyCare] 消息事件到达插件：私聊 uid={uid} session={event.unified_msg_origin}")
        self._auto_capture_user(uid)
        await self._monitor_event(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        uid = str(getattr(event.message_obj.sender, "user_id", "") or "")
        logger.info(f"[DailyCare] 消息事件到达插件：群聊 uid={uid} session={event.unified_msg_origin}")
        target_uid = str(self._cfg("target_user_id", "") or "").strip()
        if target_uid and uid not in target_uid.split(","):
            return
        self._auto_capture_user(uid)
        await self._monitor_event(event, reflect=False)

    def _auto_capture_user(self, uid: str) -> None:
        """v1.1.6：自动捕获关怀对象 QQ 号（开箱即用，无需手动配置）。

        规则：
          1. 配置 target_user_id 显式填写 → 不覆盖，尊重手动配置；
          2. 默认目标 user_id 已存在 → 不重复捕获；
          3. 首次收到用户消息（排除自己）且默认目标无 uid → 回填数据库
             并回写配置，重启后依然生效。
        """
        uid = str(uid or "").strip()
        if not uid:
            return
        cfg_uid = str(self._cfg("target_user_id", "") or "").strip()
        if cfg_uid:
            return
        try:
            default = self.db.get_default_target()
            if default is not None and str(default.get("user_id") or "").strip():
                return  # 已有关联，无需重复捕获
            if default is None:
                loc_id = self.db.upsert_location("动态定位", 0, 0, "dynamic", "IP")
                self.db.add_target("你", "用户自己", loc_id, user_id=uid,
                                   is_default=1, is_dynamic=1)
            else:
                self.db.update_target_user_id(default["id"], uid)
            # 回写配置，保证重启后仍生效
            try:
                self.config["target_user_id"] = uid
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                pass
            logger.info(f"[DailyCare] 已自动捕获关怀对象 QQ: {uid}")
        except Exception as e:
            logger.debug(f"[DailyCare] 自动捕获失败(可忽略): {e}")

    async def _monitor_event(self, event: AstrMessageEvent, reflect: bool = True):
        mid = str(getattr(event.message_obj, "message_id", "") or "")
        if mid.startswith("dailycare_"):
            return
        try:
            if event.get_self_id() and event.get_self_id() == event.get_sender_id():
                return
        except Exception:
            pass
        text = event.message_str or ""
        if not text.strip():
            return
        # 记录用户发言时间（冷场监测）
        self.monitor.record_user_message()
        # v1.1.5 晚安识别（动态休息窗口）：
        #   用户发消息 = 已醒，先打破旧休息窗口；
        #   若消息命中「晚安/睡了」，以此刻为锚点设置新的休息窗口（默认 +7h）。
        #   休息窗口内 monitor/decision/executor 均判定为安静期，不触发任何主动。
        try:
            from .core import rest as _rest
            _rest.break_rest(self.db)
            if _rest.detect_sleep_text(text):
                rest_hours = int(self._cfg("rest_after_sleep_hours", 7) or 7)
                _rest.mark_sleep(self.db, after_hours=rest_hours)
                until = _rest.rest_until_ts(self.db)
                logger.info(
                    "[DailyCare] 晚安识别：进入动态休息窗口，恢复时间=%s（%d 小时后）",
                    time.strftime("%m-%d %H:%M", time.localtime(until)), rest_hours,
                )
        except Exception as e:
            logger.debug(f"[DailyCare] 休息窗口处理异常(可忽略): {e}")
        # 用户发言后延迟触发一次快速反思
        if reflect:
            asyncio.create_task(self._deferred_reflect())

    async def _deferred_reflect(self):
        """用户发言后延迟 90 秒反思一次，带 5 分钟节流。"""
        try:
            now = time.time()
            last = self.db.kv_get("last_chat_reflect_ts", 0)
            if now - last < 300:
                return
            await asyncio.sleep(90)
            self.db.kv_set("last_chat_reflect_ts", int(time.time()))
            await self._do_chat_reflect()
        except Exception as e:
            logger.debug(f"[DailyCare] 延迟反思异常: {e}")

            logger.debug(f"[DailyCare] 延迟反思异常: {e}")

    # 注：所有测试/触发入口已收敛到 WebUI（反思/天气/决策/开口/定位按钮），
    # 不再提供 QQ 私聊命令——避免测试消息污染会话历史与 event_stream 流水表。
