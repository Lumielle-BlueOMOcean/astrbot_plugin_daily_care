# -*- coding: utf-8 -*-
"""
微光-Daily Care - 唤醒通道（v5.3 无痕唤醒 + 话题延续）
把"唤醒 bot 本人"这条唯一的非官方内部路径关进笼子。

v5.2 关键改进（修复用户反馈的三个问题）：
问题1「插件内部信息先出现」根因：运行时提示曾被当作普通用户消息处理。
修复：message 留空，认知提示只作为临时 ProviderRequest 内容，不显示、不进对话流。

问题2「关怀消息突兀」根因：主 agent 把背景当成"用户在说话"，开口带孤立感。
修复：环境感知文本预填进 ProviderRequest.extra_user_content_parts，并标记为临时认知。

v5.3 改进（用户指出唤醒消息未延续上下文）：
问题「有连续性但没延续话题」根因：注入文本把开口动机完全绑定在环境背景上，
会话历史虽已加载但只提供"记忆"，不驱动"说什么"。
修复：注入文本明确这是一次主动唤醒，并把真实会话历史交给 Main Agent 自行决定如何回应。

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
import uuid
from typing import Optional

from astrbot.api import logger


def _build_daily_care_wake_event(cron_event_cls):
    """Build the runtime-specific wake event without importing AstrBot at module load.

    The plugin's standalone tests intentionally do not install AstrBot. Keeping
    this adapter lazy preserves that property while still overriding the
    4.25.x CronMessageEvent send path at runtime.
    """

    class DailyCareWakeEvent(cron_event_cls):
        async def send(self, message) -> None:
            if message is None:
                self.set_extra("daily_care_platform_sent", False)
                return
            try:
                sent = await self.context_obj.send_message(self.session, message)
            except Exception:
                self.set_extra("daily_care_platform_sent", False)
                raise

            platform_sent = sent is True
            self.set_extra("daily_care_platform_sent", platform_sent)
            if platform_sent:
                # CronMessageEvent.send() would call Context.send_message a
                # second time. Jump directly to AstrMessageEvent's bookkeeping
                # implementation so _has_send_oper remains an implementation
                # detail rather than a delivery authority.
                await super(cron_event_cls, self).send(message)

    DailyCareWakeEvent.__name__ = "DailyCareWakeEvent"
    return DailyCareWakeEvent


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

    async def wake(self, session_str: str, wake_facts: str,
                   with_topic: bool = True, *, target_id: int = 0,
                   channel: str = "care", plan_id: int = 0,
                   wake_source: str = "") -> tuple[bool, str]:
        """无痕唤醒 bot 本人。返回 ``(是否入队成功, wake_id)``。

        session_str: 目标会话的 unified_msg_origin（如 平台实例:消息类型:用户ID）
        wake_facts:  从当前 active events 确定性构造的客观事实；主动冷场
                     唤醒可以为空，不能携带 DecisionEngine 的控制上下文
        with_topic:  是否注入最近话题脉络。有具体背景的唤醒（天气/状态关怀）
                     背景本身就是开口理由，不需要话题接续；仅冷场主动需要。
        """
        if not session_str:
            return False, ""
        if not wake_facts and channel != "proactive" and wake_source != "proactive":
            return False, ""
        wake_id = uuid.uuid4().hex
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
                conv_mgr = getattr(self.context, "conversation_manager", None)
                if conv_mgr is None:
                    raise RuntimeError("conversation_manager unavailable")

                cid = await conv_mgr.get_curr_conversation_id(umo)
                if not cid:
                    cid = await conv_mgr.new_conversation(umo, session.platform_id)
                if not cid:
                    raise RuntimeError("conversation id unavailable")

                conversation = await conv_mgr.get_conversation(umo, cid)
                if conversation is None:
                    raise RuntimeError(f"conversation not found: {cid}")
                conversation_id = str(getattr(conversation, "cid", "") or "")
                if not conversation_id:
                    raise RuntimeError("conversation id missing")

                if not hasattr(conversation, "history"):
                    raise ValueError("conversation history missing")
                history_raw = conversation.history
                if history_raw is None:
                    raise ValueError("conversation history is null")
                if isinstance(history_raw, str):
                    contexts = json.loads(history_raw)
                else:
                    contexts = history_raw
                if not isinstance(contexts, list):
                    raise ValueError("conversation history must be a list")

                req.conversation = conversation
                req.contexts = contexts
                # v1.1.5：仅冷场主动等需要话题接续的唤醒才提取；具体关怀不注入话题
                # v1.1.5 完善：recent_topic 注入做总开关（enable_recent_topic，
                # 默认关闭）——完整会话历史 + 长期记忆已足够支撑话题延续，
                # 摘要层默认移除，避免自我强化与复读；需要时可在面板开启。
                if with_topic and self.config.get("enable_recent_topic", False):
                    recent_topic = self._extract_recent_topic(req.contexts)
            except Exception as e:
                logger.warning(f"[DailyCare] 加载真实会话失败，放弃本轮主动唤醒: {e}")
                return False, ""

            # 本轮提示是临时认知：真实历史和其他插件上下文仍由 Main Agent
            # 正常管线提供，Daily Care 只追加本轮事实与协议边界。
            why_lines = [
                "这是一次主动唤醒，本轮没有新的用户输入。",
                "你仍然是当前主对话中的你自己。",
                "结合当前人格、真实会话历史、长期记忆以及其他插件为你提供的当前状态，",
                "自行决定现在是否真的想主动对用户说话。",
                "",
                "本轮可感知的客观事实：",
                wake_facts or "（本轮没有额外客观事实。）",
            ]
            if recent_topic:
                why_lines += [
                    "",
                    "最近你们聊到的话题（作为你记得的上下文，自然地接续它）：",
                    recent_topic,
                ]
            why_lines += [
                "",
                "如果想说话，message 中只写真正要对用户说的话。",
                "如果此刻不想打扰用户，则选择 silent。",
                '只输出完整 JSON：{"action":"send","message":"..."} 或 {"action":"silent","message":""}。',
                "不要输出 markdown、解释文字、内部状态或其他字段。",
            ]
            note = "\n".join(why_lines)
            req.extra_user_content_parts.append(TextPart(text=note).mark_as_temp())

            care_event_cls = _build_daily_care_wake_event(CronMessageEvent)
            care_event = care_event_cls(
                context=self.context,
                session=session,
                message="",
                extras={
                    "enable_streaming": False,
                    "daily_care": {
                        "kind": "wake",
                        "wake_id": wake_id,
                        "target_id": int(target_id or 0),
                        "channel": channel,
                        "plan_id": int(plan_id or 0),
                        "wake_source": wake_source or channel,
                        "conversation_id": conversation_id,
                        "umo": umo,
                    },
                    "provider_request": req,
                },
                message_type=session.message_type,
            )
            await self.context.get_event_queue().put(care_event)
            logger.info(
                f"[DailyCare] 主动唤醒已入队，等待 Main Agent 自主决定是否开口（wake_id={wake_id}）"
            )
            return True, wake_id
        except Exception as e:
            logger.error(f"[DailyCare] 无痕唤醒事件推入失败: {e}")
            return False, ""
