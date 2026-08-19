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
        self.wake_channel = WakeChannel(context, config)

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
        return f"{platform_id}:FriendMessage:{uid}"

    def _platform_id(self) -> str:
        pid = str(self.config.get("platform_id", "") or "").strip()
        if pid and pid != "auto":
            return pid
        if self.session and ":" in self.session:
            return self.session.split(":")[0]
        # v1.1.6：动态从已注册平台实例获取真实 platform_id（不硬编码 Lumielle）
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
        sent = []
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
            background = plan.get("content_summary") or ""
            if not background:
                self.db.mark_plan(plan["id"], "skipped")
                continue
            # v1.1.5：计划关怀(care)与冷场主动互斥——窗口内另一类刚发过则跳过
            if self._mutex_blocked("care", target["id"]):
                logger.info("[DailyCare] 计划关怀与冷场主动互斥，跳过本次")
                self.db.mark_plan(plan["id"], "skipped")
                continue
            ok, _ = await self._woke_for_care(target, background, with_topic=False)
            if ok:
                self.db.mark_plan(plan["id"], "sent")
                # 记录发送日志（以背景代表"提醒过这件事"，供防重复）
                self.db.add_send_log(target["id"], plan["id"], background[:500], "care")
                self.db.kv_set(f"last_care_send_{target['id']}", int(time.time()))
                self.db.kv_set("last_activity_ts", int(time.time()))  # v1.1.1：主动开口也算交流，重置静默
                sent.append(background)
                logger.info(f"[DailyCare] 计划已执行（随机时刻 {datetime.fromtimestamp(trigger_ts).strftime('%H:%M') if trigger_ts else '窗口' }）")
            else:
                # 唤醒失败：放弃本次，不降级直发
                logger.warning(f"[DailyCare] 计划唤醒失败，放弃本次（不降级直发）: {background[:30]}...")
                self.db.mark_plan(plan["id"], "skipped")
        return sent

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
        跨板块互斥）。天气(weather)含客观信息量（下雨/降温/预警），不参与互斥。
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

    async def execute_immediate(self, background: str, channel: str = "care", target: Optional[dict] = None) -> bool:
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
        ok, _ = await self._woke_for_care(target, background, with_topic=(channel == "proactive"))
        if ok:
            self.db.add_send_log(target["id"], 0, background[:500], channel)
            # 冷却分轨：按 channel 记录各自最后发送时间
            self.db.kv_set(self._last_send_key(target["id"], channel), int(time.time()))
            self.db.kv_set("last_activity_ts", int(time.time()))  # v1.1.1：主动开口也算交流，重置静默
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
    async def test_send(self, background: str = "") -> list[str]:
        """测试：立即基于活跃事件唤醒一次开口。"""
        target = self.db.get_default_target()
        if not target:
            return []
        if not background:
            events = self.db.get_active_events(target["id"])
            if not events:
                return []
            background = self._compose_background(target, events)
        if not background:
            return []
        ok, _ = await self._woke_for_care(target, background, with_topic=False)
        if ok:
            self.db.add_send_log(target["id"], 0, background[:500], "test")
            self.db.kv_set(f"last_care_send_{target['id']}", int(time.time()))
            self.db.kv_set("last_activity_ts", int(time.time()))  # v1.1.1：主动开口也算交流，重置静默
            return [background]
        return []

    # ---------- 唤醒（v5.1：交给 WakeChannel，全插件唯一非官方路径被隔离）----------
    async def _woke_for_care(self, target: dict, background: str,
                            with_topic: bool = True) -> tuple[bool, str]:
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
        return await self.wake_channel.wake(session_str, background, with_topic=with_topic)
