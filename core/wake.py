# -*- coding: utf-8 -*-
"""
微光-Daily Care - 唤醒通道（v5.3 无痕唤醒 + 话题延续）
把"唤醒 bot 本人"这条唯一的非官方内部路径关进笼子。

v5.2 关键改进（修复用户反馈的三个问题）：
问题1「插件内部信息先出现」根因：背景文本被塞进 CronMessageEvent.message，
被当作"用户消息"显示成 Scheduler 消息、写进对话历史。
修复：message 留空（不显示、不进对话流），背景只通过 extras 传递。

问题2「关怀消息突兀」根因：主 agent 把背景当成"用户在说话"，开口带孤立感。
修复：环境感知文本预填进 ProviderRequest.extra_user_content_parts，声明
"你没收到任何消息，是你自己心里泛起念头想开口"——自然的主动关怀形态。

v5.3 改进（用户指出唤醒消息未延续上下文）：
问题「有连续性但没延续话题」根因：注入文本把开口动机完全绑定在环境背景上，
会话历史虽已加载但只提供"记忆"，不驱动"说什么"。
修复：注入文本重构为两个维度——① 为什么开口（心里泛起惦记，来自环境背景，
不再声明"没收到消息"，消除与真实历史的矛盾）；② 说什么（从会话历史提炼最近
话题脉络并入注入，自然接续你们正在聊的内容）。

无痕唤醒的实现要点（读 AstrBot 源码确认）：
- internal.py 的 process：has_valid_message=False 时若 has_provider_request=True
  仍会继续，不会跳过空消息。
- build_main_agent：若 event 带 provider_request 则直接用（1300 行），
  不再从 message_str 构造 prompt（1319 行）；若 req.prompt 为空但
  extra_user_content_parts 非空则设 prompt="<attachment>" 继续（1466 行），
  不会 return None。
- 因此：message="" + extras 里预填了 ProviderRequest(含环境感知 TextPart)，
  既能绕过"空消息跳过"，又不让任何背景文本进入会话显示/对话流。

设计原则：
- 上层（executor / decision）只依赖 WakeChannel.wake()，不感知内部实现。
- AstrBot 升级时，本文件是唯一需要检查/修复的点。
"""
import json
from typing import Optional

from astrbot.api import logger


class WakeChannel:
    """唤醒通道：把关怀事件交给 AstrBot 官方完整管线，由 bot 本人开口。"""

    def __init__(self, context, config: Optional[dict] = None):
        self.context = context
        self.config = config or {}

    @staticmethod
    def _extract_recent_topic(contexts, max_rounds: int = 2, max_chars: int = 200) -> str:
        """从 OpenAI 格式会话历史中提炼最近话题脉络，供注入文本使用。

        取最后 max_rounds 轮对话（user+assistant 配对），压缩成简短摘要。
        兼容 content 为纯字符串或多模态列表两种情况；失败时返回空串。
        """
        try:
            if not contexts:
                return ""
            items = list(contexts)

            def to_text(item):
                c = item.get("content") if isinstance(item, dict) else None
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    parts = []
                    for b in c:
                        if isinstance(b, dict):
                            t = b.get("text") or b.get("content") or ""
                            parts.append(t if isinstance(t, str) else "")
                    return "".join(parts)
                return ""

            pairs = []
            for it in items:
                role = it.get("role", "") if isinstance(it, dict) else ""
                txt = to_text(it).strip()
                if not txt:
                    continue
                # v1.1.5：只摘用户真实消息作为话题脉络来源。
                # 不摘 bot 自己的消息（含主动消息）——避免「自己接自己的话」自我强化。
                if role != "user":
                    continue
                pairs.append((role, txt))
            if not pairs:
                return ""

            tail = pairs[-(max_rounds * 2):]
            lines = []
            for role, txt in tail:
                snippet = txt.replace("\n", " ").strip()
                if len(snippet) > 80:
                    snippet = snippet[:80] + "…"
                lines.append(f"用户：{snippet}")
            summary = "；".join(lines)
            if len(summary) > max_chars:
                summary = summary[:max_chars] + "…"
            return summary
        except Exception:
            return ""

    async def wake(self, session_str: str, background: str,
                   with_topic: bool = True) -> tuple[bool, str]:
        """无痕唤醒 bot 本人。返回 (是否成功, 背景文本)。

        session_str: 目标会话的 unified_msg_origin（如 平台实例:消息类型:用户ID）
        background:  客观背景事实（作为"自己记得的事"注入给 bot 本人感知，
                     不进入会话显示，不进入对话流）
        with_topic:  是否注入最近话题脉络。有具体背景的唤醒（天气/状态关怀）
                     背景本身就是开口理由，不需要话题接续；仅冷场主动需要。
        """
        if not session_str or not background:
            return False, ""
        try:
            from astrbot.core.agent.message import TextPart
            from astrbot.core.cron.events import CronMessageEvent
            from astrbot.core.platform.message_session import MessageSession
            from astrbot.core.provider.entities import ProviderRequest

            session = MessageSession.from_str(session_str)
            umo = session_str  # session_str 本身就是 unified_msg_origin 格式

            # 构造 ProviderRequest：空 prompt + 预填环境感知 + 真实会话历史
            req = ProviderRequest()
            req.prompt = ""
            req.image_urls = []
            req.audio_urls = []
            recent_topic = ""
            try:
                conv_mgr = self.context.conversation_manager
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if not cid:
                    cid = await conv_mgr.new_conversation(umo, session.platform_id)
                conversation = await conv_mgr.get_conversation(umo, cid)
                if not conversation:
                    cid = await conv_mgr.new_conversation(umo, session.platform_id)
                    conversation = await conv_mgr.get_conversation(umo, cid)
                if conversation:
                    req.conversation = conversation
                    req.contexts = json.loads(conversation.history or "[]")
                    # v1.1.5：仅冷场主动等需要话题接续的唤醒才提取；具体关怀不注入话题
                    # v1.1.5 完善：recent_topic 注入做总开关（enable_recent_topic，
                    # 默认关闭）——完整会话历史 + 长期记忆已足够支撑话题延续，
                    # 摘要层默认移除，避免自我强化与复读；需要时可在面板开启。
                    if with_topic and self.config.get("enable_recent_topic", False):
                        recent_topic = self._extract_recent_topic(req.contexts)
            except Exception as e:
                logger.warning(f"[DailyCare] 加载会话历史失败(将无历史唤醒): {e}")

            # v5.3 注入文本重构：两个维度
            # 维度一「为什么开口」：心里忽然泛起惦记，来自环境背景（不声明"没收到消息"，
            #   避免与真实历史冲突）；维度二「说什么」：自然延续最近话题，把惦记融进去。
            why_lines = [
                "# 关于你此刻的状态",
                "你心里忽然泛起一丝惦记，想要主动开口对用户说点什么。",
                "这不是在回应谁，而是你自己内心自然涌起的关心。",
                "你此刻心里装着这些（当作你自己记得的事实，不必提及它们的来源）：",
                background,
            ]
            if recent_topic:
                why_lines += [
                    "",
                    "最近你们聊到的话题（作为你记得的上下文，自然地接续它）：",
                    recent_topic,
                ]
            note = "\n".join(why_lines)
            req.extra_user_content_parts.append(TextPart(text=note))

            care_event = CronMessageEvent(
                context=self.context,
                session=session,
                message="",
                extras={
                    "daily_care": {
                        "background": background,
                        "kind": "wake",
                        "injected": True,
                    },
                    "provider_request": req,
                },
                message_type=session.message_type,
            )
            await self.context.get_event_queue().put(care_event)
            logger.info(
                f"[DailyCare] 无痕唤醒事件已推入总线（背景不显示、不进对话流，由 bot 本人开口）"
            )
            return True, background
        except Exception as e:
            logger.error(f"[DailyCare] 无痕唤醒事件推入失败: {e}")
            return False, ""
