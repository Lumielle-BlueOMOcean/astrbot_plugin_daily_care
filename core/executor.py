# -*- coding: utf-8 -*-
"""
微光-Daily Care - 执行层（v5 / v5.1）
核心原则：插件只当"眼睛"和"闹钟"，不当"嘴"。

v5 架构：
1. 执行"关怀计划"（决策层产物）：到点后把带真实会话的唤醒事件推入事件总线，
   由 AstrBot 官方完整管线唤醒"我本人"开口。
2. 触发时刻在决策时已随机化（窗口内随机），模拟"想起来就关心"。
3. 不再有直发链路——唤醒失败就放弃这次并记日志，宁可少说不可说错。

v5.1：
4. 唤醒逻辑已隔离到 WakeChannel（core/wake.py）——全插件唯一非官方内部路径，
   升级时只检查/修改那个文件。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Optional

from astrbot.api import logger

from .database import CareDatabase
from .wake import WakeChannel

# 关怀窗口定义（与 decision 对齐）
WINDOWS = {
    "morning": (8, 11),    # 早上 8-11 点
    "noon": (11, 14),      # 中午 11-14 点
    "evening": (17, 20),   # 傍晚 17-20 点
    "night": (20, 23),     # 晚上 20-23 点
}


class Executor:
    def __init__(self, db: CareDatabase, config: dict, context, llm_func,
                 persona_prompt: str = "", session: str = ""):
        self.db = db
        self.config = config
        self.context = context
        self.llm_func = llm_func      # 保留（备用，不再用于直发）
        self.persona_prompt = persona_prompt or ""
        self.session = session        # 默认发送会话（unified_msg_origin）
        self.wake_channel = WakeChannel(
            context, config, on_transport_failure=self._on_wake_transport_failure
        )
        # v1.1.7：平台实例动态解析缓存与失败计数（不再锁死、不再兜底）
        self._pid_cache = ""
        self._pid_cache_ts = 0.0
        self._pid_fail_count = 0

    def _on_wake_transport_failure(self, event, reason: str) -> None:
        """Finalize plan state when a wake fails before after-message hooks."""
        care = event.get_extra("daily_care")
        if not isinstance(care, dict) or care.get("kind") != "wake":
            return
        plan_id = int(care.get("plan_id") or 0)
        if plan_id:
            self.db.mark_plan(plan_id, "skipped")
        logger.warning(
            f"[DailyCare] wake_id={care.get('wake_id', '')} "
            f"stage=wake_terminal outcome=invalid reason={reason}"
        )

    # ---------- 时间判定 ----------
    def in_dnd(self) -> bool:
        """是否处于安静期 = 勿扰时段 OR 动态休息窗口（v1.1.5 晚安识别）。"""
        from .rest import in_quiet
        return in_quiet(self.db, self.config)

    def current_window(self) -> Optional[str]:
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        for name, (sh, eh) in WINDOWS.items():
            if sh * 60 <= cur < eh * 60:
                return name
        return None

    # ---------- 会话 ----------
    def _target_session(self, target: dict) -> str:
        uid = str(target.get("user_id") or "").strip()
        if not uid:
            return self.session or ""
        platform_id = self._platform_id()
        if not platform_id:
            # v1.1.7：解析失败不拼幽灵会话，返回空由上层放弃本次唤醒
            return ""
        return f"{platform_id}:FriendMessage:{uid}"

    def _platform_id(self) -> str:
        """解析平台实例 ID。

        优先级：显式配置 platform_id（非 auto）> 已注册平台实例（TTL 缓存）。
        v1.1.7：
        - 不再硬编码兜底 "Lumielle"（对其他用户该实例不存在，兜底反而埋雷）。
        - 不再锁死初始化时的 session 前缀：动态解析失败会定期重试，而非永久沿用旧值。
        - 连续失败时在日志中提示用户在 WebUI 手动配置 UMO。
        """
        pid = str(self.config.get("platform_id", "") or "").strip()
        if pid and pid != "auto":
            return pid
        now = time.time()
        if self._pid_cache and now - self._pid_cache_ts < 60:
            return self._pid_cache
        try:
            pm = getattr(self.context, "platform_manager", None)
            if pm is not None:
                insts = getattr(pm, "platform_insts", None) or []
                for inst in insts:
                    pid = (getattr(inst, "config", None) or {}).get("id", "")
                    if pid and pid != "webchat":
                        self._pid_cache = pid
                        self._pid_cache_ts = now
                        self._pid_fail_count = 0
                        return pid
        except Exception as e:
            logger.warning(f"[DailyCare] 动态解析平台实例失败: {e}")
        # 解析失败：清空缓存以便尽快重试，并计数提示手动配置
        self._pid_cache = ""
        self._pid_fail_count += 1
        if self._pid_fail_count >= 3 and self._pid_fail_count % 3 == 0:
            logger.warning(
                f"[DailyCare] 已连续 {self._pid_fail_count} 次动态解析平台实例失败，"
                "定时唤醒暂时停用。请在 WebUI「关怀对象」区手动填写 UMO / 平台实例 ID，"
                "或确认平台实例已注册后重启 AstrBot。"
            )
        return ""

    # ---------- 会话上下文（仅反思读取历史用，不参与直发）----------
    async def _load_session_context(self, session: str) -> tuple[str, list]:
        """从 AstrBot 读取会话当前人格与对话历史。失败回退快照与空历史。"""
        persona = self.persona_prompt or ""
        history: list = []
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if cm is None:
                return persona, history
            conv_id = await cm.get_curr_conversation_id(session)
            if not conv_id:
                return persona, history
            conv = await cm.get_conversation(session, conv_id)
            if conv is None:
                return persona, history
            raw = getattr(conv, "history", "") or ""
            if isinstance(raw, str) and raw.strip():
                try:
                    history = json.loads(raw)
                except Exception:
                    history = []
            elif isinstance(raw, list):
                history = raw
        except Exception as e:
            logger.warning(f"[DailyCare] 读取会话上下文失败: {e}")
        return persona, history

    # ---------- 计划执行 ----------
    async def execute_due_plans(self) -> list[str]:
        """执行今日到期的关怀计划（trigger_ts 已到点或在当前窗口内）。"""
        if self.in_dnd():
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        now_ts = int(time.time())
        plans = self.db.get_pending_plans(today)
        queued = []
        for plan in plans:
            # v1.1.6：计划按各自 target 发送（多对象关怀），默认对象作为兜底
            target = self.db.get_target(plan.get("target_id") or 0) or self.db.get_default_target()
            if not target:
                self.db.mark_plan(plan["id"], "skipped")
                continue
            # 触发判定：已到随机时刻，或窗口匹配且已过窗口起点
            trigger_ts = plan.get("trigger_ts") or 0
            window = plan.get("trigger_window") or ""
            due = False
            if trigger_ts and trigger_ts <= now_ts:
                due = True
            elif window and window == self.current_window():
                wsh = WINDOWS.get(window, (0, 0))[0]
                if now_ts >= int(datetime.now().replace(hour=wsh, minute=0, second=0, microsecond=0).timestamp()):
                    due = True
            if not due:
                continue
            wake_facts = plan.get("content_summary") or ""
            is_proactive = plan.get("task_type") == "proactive"
            if not wake_facts and not is_proactive:
                self.db.mark_plan(plan["id"], "skipped")
                continue
            # v1.1.7（补丁）：计划背景创建于凌晨/深夜，其中的时间描述可能已过期。
            # 注入当前真实时间，避免 LLM 把旧时间当作「此刻」（曾导致早上 8 点的
            # 问候写成「凌晨三点多了」）。当前时间显式覆盖，旧背景仅作内容参考。
            _now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            if wake_facts:
                wake_facts = f"{wake_facts}\n（附注：以上事实写于更早时刻；当前实际时间：{_now_str}，请以当前时间为准。）"
            # v1.1.5：计划关怀(care)与冷场主动互斥——窗口内另一类刚发过则跳过
            channel = "proactive" if is_proactive else "care"
            if self._mutex_blocked(channel, target["id"]):
                logger.info("[DailyCare] 计划关怀与冷场主动互斥，跳过本次")
                self.db.mark_plan(plan["id"], "skipped")
                continue
            if not self.db.mark_plan(plan["id"], "processing"):
                continue
            try:
                ok, wake_id = await self._woke_for_care(
                    target,
                    wake_facts,
                    with_topic=is_proactive,
                    channel=channel,
                    plan_id=plan["id"],
                    wake_source="proactive" if is_proactive else "plan",
                )
            except Exception as e:
                logger.warning(f"[DailyCare] 计划唤醒入队异常，跳过本次: {e}")
                ok, wake_id = False, ""
            if ok:
                queued.append(wake_id)
                logger.info(f"[DailyCare] 计划主动唤醒已入队，等待 Main Agent 决定是否发送（随机时刻 {datetime.fromtimestamp(trigger_ts).strftime('%H:%M') if trigger_ts else '窗口' }，wake_id={wake_id}）")
            else:
                # 唤醒失败：放弃本次，不降级直发
                logger.warning(f"[DailyCare] 计划唤醒失败，放弃本次（不降级直发）: {wake_facts[:30]}...")
                self.db.mark_plan(plan["id"], "skipped")
        return queued

    @staticmethod
    def _last_send_key(target_id: int, channel: str) -> str:
        """冷却分轨的 kv 键：主动/天气/状态各自记录最后发送时间。"""
        if channel == "proactive":
            return f"last_proactive_send_{target_id}"
        if channel in ("weather", "weather_alert"):
            return f"last_weather_send_{target_id}"
        return f"last_care_send_{target_id}"

    def _mutex_blocked(self, channel: str, target_id: int) -> bool:
        """v1.1.5 互斥合并：冷场主动(proactive)与状态/计划关怀(care)内容高度相似
        （如「我们有一阵子没说话了」「已有 N 分钟没有对话」），同一沉默时段两类
        几乎同时命中时会连发两条同类消息。此处在发送前做互斥：silence_exclude_window_min
        分钟内，另一类刚发过则本次直接放弃（决策层已各按自身冷却判断过，这里只做
        跨板块互斥）。天气(weather)含客观信息量，不参与互斥。
        """
        if channel not in ("proactive", "care"):
            return False
        win = int(self.config.get("silence_exclude_window_min", 30))
        if win <= 0:
            return False
        other = "care" if channel == "proactive" else "proactive"
        last = self.db.kv_get(self._last_send_key(target_id, other), 0)
        if not last:
            return False
        return (time.time() - last) < win * 60

    async def execute_immediate(self, wake_facts: str, channel: str = "care",
                                target: Optional[dict] = None, *, plan_id: int = 0,
                                wake_source: str = "") -> bool:
        """决策为 act 时立即唤醒开口。channel 区分来源：主动消息传 proactive，关怀传 care。

        v1.1.6：支持指定 target（多对象关怀）。不传则用默认对象。
        """
        if self.in_dnd():
            return False
        if target is None:
            target = self.db.get_default_target()
        if not target:
            return False
        # v1.1.5：冷场主动与状态关怀互斥（天气豁免）
        if self._mutex_blocked(channel, target["id"]):
            logger.info(f"[DailyCare] {channel} 与另一沉默类板块互斥，放弃本次")
            return False
        ok, wake_id = await self._woke_for_care(
            target,
            wake_facts,
            with_topic=(channel == "proactive"),
            channel=channel,
            plan_id=plan_id,
            wake_source=wake_source or channel,
        )
        if ok:
            logger.info(
                f"[DailyCare] 主动唤醒已入队，等待 Main Agent 自主决定是否开口（wake_id={wake_id}）"
            )
            return True
        logger.warning("[DailyCare] 立即开口唤醒失败，放弃本次（不降级直发）")
        return False

    # ---------- 背景构造（自然化）----------
    def _compose_background(self, target: dict, events: list[dict]) -> str:
        """把活跃事件转成「我自己惦记的事」。

        v5：不再用"你注意到"的报告句式——那会让开口显得生硬。
        改为平实陈述，像自己心里本来就知道的事。
        on_llm_request 注入会声明这些是环境信息，不是用户说的话。
        """
        if not events:
            return ""
        lines = []
        for ev in events[:4]:
            summary = ev.get("summary") or ""
            if not summary:
                continue
            lines.append(summary)
        if not lines:
            return ""
        return "\n".join(lines)

    # ---------- 测试入口 ----------
    async def test_send(self, wake_facts: str = "") -> list[str]:
        """测试：立即基于活跃事件唤醒一次开口。"""
        target = self.db.get_default_target()
        if not target:
            return []
        if not wake_facts:
            events = self.db.get_active_events(target["id"])
            if not events:
                return []
            wake_facts = self._compose_background(target, events)
        if not wake_facts:
            return []
        ok, wake_id = await self._woke_for_care(
            target, wake_facts, with_topic=False,
            channel="test", wake_source="manual_test",
        )
        if ok:
            return [wake_id]
        return []

    # ---------- 唤醒（v5.1：交给 WakeChannel，全插件唯一非官方路径被隔离）----------
    async def _woke_for_care(self, target: dict, wake_facts: str,
                            with_topic: bool = True, *, channel: str = "care",
                            plan_id: int = 0, wake_source: str = "") -> tuple[bool, str]:
        """唤醒 bot 本人开口（v5 终极方案，v5.1 起实现隔离在 core/wake.py）。

        把带真实会话的 CronMessageEvent（is_wake=True）推入事件总线，
        由 AstrBot 官方完整管线全自动处理：
          1. waking_check：私聊事件直接判定为唤醒（is_wake=True）
          2. process_stage：唤醒命令且无发送操作 → 自动进入主 agent 子阶段
          3. build_main_agent：构建带完整人格/记忆/历史的"我本人"
          4. 主 agent 输出 → respond stage 自动发送（Prepare to send）
          5. _save_to_history 自动写入真实对话历史

        插件不碰 LLM 生成、不碰发送、不碰历史保存——只负责"到点叫醒"。
        唤醒失败：放弃本次，绝不降级直发。
        """
        session_str = self._target_session(target)
        if not session_str:
            return False, ""
        return await self.wake_channel.wake(
            session_str,
            wake_facts,
            with_topic=with_topic,
            target_id=target.get("id", 0),
            channel=channel,
            plan_id=plan_id,
            wake_source=wake_source or channel,
        )
