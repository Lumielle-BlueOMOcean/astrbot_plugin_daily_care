# -*- coding: utf-8 -*-
"""
微光-Daily Care - 理解层（v2）
插件只当"眼睛"，不当"嘴"。本层全部由 LLM 负责"理解"，绝不生成话术。

1. ChatReflector.reflect()：每隔一段时间反思最近的聊天记录，
   提取用户的状态变化（身体不适/熬夜/情绪/恢复…），
   写入结构化关怀事件（care_events, source='state'）。
   v5.7.0：反思时注入【当前活跃状态事件清单】，由 LLM 对比近期对话
   语义判定哪些事件已经结束（用户说"好了/不难受了/退烧了"等，任何自然
   说法都算），按 event_id 精确关闭；正则关键词匹配降级为兜底。
2. WeatherJudge.judge()：对每次天气检查结果，由 LLM 判断
   当前天气是否值得主动提醒；值得则产出"事实背景"写入事件
   （source='weather'），不值得则什么都不做。

产出的一律是"事实背景"，不含任何指令、话术或建议。
开口说话永远是 bot 本人的事（见 executor.py）。
"""
import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any, Optional

from astrbot.api import logger

from .database import CareDatabase

# 状态类型 → 默认强度 / 默认存活小时数
STATE_TYPES = {
    "sick":       {"intensity": 4, "ttl": 72},    # 身体不适
    "injury":     {"intensity": 4, "ttl": 72},    # 受伤
    "late_night": {"intensity": 2, "ttl": 48},    # 熬夜/睡眠不足
    "tired":      {"intensity": 2, "ttl": 48},    # 疲惫
    "mood":       {"intensity": 3, "ttl": 48},    # 情绪低落
    "diet":       {"intensity": 2, "ttl": 24},    # 饮食风险
    "recovery":   {"intensity": 1, "ttl": 0},     # 恢复信号（用于关闭旧事件）
}

STATE_LABEL = {
    "sick": "身体不适", "injury": "受伤", "late_night": "熬夜",
    "tired": "疲惫", "mood": "情绪低落", "diet": "饮食",
    "recovery": "恢复", "other": "其他",
}

# v5.8.2 补扫粗筛关键词（只决定「要不要精判」，最终判定仍由 LLM 语义完成）
# 正向不适词 = 动态组装（care_causes.keywords，LLM 可新增）+ 下面这份静态集。
# 静态集 v5.8.2 大扩充（本地子串匹配零 token 成本，宁滥勿缺）：
#   单字高信号词（癌/症/瘤/炎…出现即大概率与疾病相关）+ 常见不常见病名，
#   多命中只多一次 LLM 精判调用，绝不漏判；结论始终由 LLM 下。
FORWARD_KEYWORDS = [
    # 单字高信号（疾病通用）
    "癌", "症", "瘤", "炎", "肿", "痒", "晕", "吐", "泻", "麻", "颤",
    "烧", "咳", "痰", "疹", "疮", "溃", "胀", "僵", "疼", "痛",
    # 常见病 / 相对常见的不常见病
    "高血压", "低血压", "糖尿病", "冠心病", "心绞痛", "心律失常", "心肌炎",
    "贫血", "哮喘", "气管炎", "支气管炎", "肺炎", "肺结核", "肺结节",
    "鼻炎", "鼻窦炎", "中耳炎", "咽炎", "扁桃体炎", "喉炎",
    "胃炎", "胃溃疡", "十二指肠溃疡", "胃食管反流", "肠炎", "肠胃炎",
    "结肠炎", "阑尾炎", "肝炎", "脂肪肝", "胆囊炎", "胆结石", "肾结石",
    "肾炎", "膀胱炎", "尿路感染",
    "关节炎", "类风湿", "痛风", "骨质疏松", "颈椎病", "腰椎间盘突出",
    "腰肌劳损", "肩周炎", "腱鞘炎", "网球肘",
    "湿疹", "荨麻疹", "皮炎", "痤疮", "口腔溃疡", "牙龈炎", "牙周炎",
    "蛀牙", "智齿", "麦粒肿", "结膜炎", "角膜炎", "青光眼", "白内障",
    "偏头痛", "三叉神经痛", "面瘫", "中风", "脑梗", "癫痫", "帕金森",
    "抑郁症", "焦虑症", "强迫症", "失眠症", "神经衰弱",
    "甲亢", "甲减", "甲状腺结节", "乳腺增生", "乳腺结节", "前列腺炎",
    "痔疮", "肛裂", "疝气", "静脉曲张", "灰指甲", "脚气", "冻疮",
    "中暑", "晕车", "晕船", "食物中毒", "花粉症", "低血糖", "高血糖", "血脂高",
    "脂肪瘤", "血管瘤", "渐冻症", "白血病", "淋巴瘤", "红斑狼疮",
    "克罗恩病", "多发性硬化", "重症肌无力", "尿毒症", "肝硬化", "胰腺炎",
    "脑膜炎", "心肌梗死",
    # 原有兜底
    "不舒服", "难受", "累死", "没精神", "睡不着", "吃辣", "吃冰", "受伤",
    "摔了", "犯困", "困死了",
]
RECOVERY_KEYWORDS = [
    "好了", "痊愈", "恢复了", "退烧了", "退了", "不疼了", "不痛了", "不难受了",
    "没事了", "没什么事了", "不咳了", "好多了", "差不多了", "好啦", "没问题了",
]


def now_ts() -> int:
    import time
    return int(time.time())


class ChatReflector:
    """对话状态反思：LLM 从最近的聊天记录里提取用户状态变化，并判定旧事件是否结束。"""

    # v5.8.2：框架窗口估算（AstrBot 50 轮 ≈ 100 条消息，留余量防误判）
    WINDOW_SIZE = 120

    def __init__(self, db: CareDatabase, config: dict, llm_func):
        self.db = db
        self.config = config
        self.llm_func = llm_func  # async (system_prompt, user_prompt) -> str

    # ---------- 输入构建 ----------
    @staticmethod
    def _extract_user_messages(history) -> list[dict]:
        """从会话历史中提取 user 消息。

        AstrBot 历史消息为 OpenAI 格式（role/content），不含时间戳，
        因此增量衔接采用「内容锚点」：记住上次看到的最后一条消息内容，
        下次从历史中定位该锚点，往后接续（见 reflect）。
        """
        import re
        _meta = re.compile(r'^\[[^\]]*\]\s*\([^)]*\)\s*:\s*')
        _ts = re.compile(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*$')
        _rem = re.compile(r'<system_reminder>.*?</system_reminder>', re.S)

        msgs = []
        for msg in history or []:
            try:
                if hasattr(msg, "to_dict"):
                    d = msg.to_dict()
                elif isinstance(msg, dict):
                    d = dict(msg)
                else:
                    continue
                role = d.get("role", "")
                content = d.get("content", "")
                if isinstance(content, list):
                    text = ""
                    for seg in content:
                        if isinstance(seg, dict) and seg.get("type") == "text":
                            text += seg.get("text", "")
                    content = text
                content = str(content or "").strip()
                if role == "user" and content:
                    content = _rem.sub('', content)
                    content = _meta.sub('', content)
                    content = _ts.sub('', content)
                    content = content.strip()
                    if content:
                        msgs.append({"role": "user", "content": content})
            except Exception:
                continue
        return msgs

    @staticmethod
    def _format_active_events(active_events: Optional[list[dict]]) -> str:
        """把活跃状态事件格式化成 prompt 片段，带事件 ID / type / cause 供 LLM 引用。"""
        if not active_events:
            return "- （无）\n"
        out = []
        for e in active_events:
            tag = ""
            if e.get("type") and e.get("cause"):
                tag = f"[{e['type']}:{e['cause']}]"
            out.append(f"- [事件ID {e['id']}] {tag} {e['summary']}（{e.get('detail') or ''}）")
        return "\n".join(out) + "\n"

    @staticmethod
    def _format_causes(causes: list) -> str:
        """把病因清单格式化成 prompt 片段（含症状参考词）。"""
        if not causes:
            return "- （无）\n"
        out = []
        for c in causes:
            kws = f"，参考词：{c['keywords']}" if c.get("keywords") else ""
            out.append(f"- [{c['type']}] {c['cause']}{kws}")
        return "\n".join(out) + "\n"

    # ---------- LLM 反思 ----------
    def _build_prompt(self, user_msgs: list[dict], target_name: str, new_start: int = 0,
                      active_events: Optional[list[dict]] = None) -> tuple[str, str]:
        causes = self.db.list_causes()
        system = (
            "你是对话状态观察者。你的唯一任务：从对话片段中提取【用户本人】的状态变化，"
            "并对比当前活跃关怀事件，判断哪些已经结束。\n"
            "你只负责提取和描述，绝对不要生成任何关怀话术、建议或提醒文案。\n\n"
            "只输出一个 JSON 对象（不要 markdown 代码块，不要额外解释）：\n"
            '{"new_states":[{"type":"sick|injury|late_night|tired|mood|diet|recovery|other",'
            '"cause":"二级病因（从下方病因清单中选择，见规则5）",'
            '"summary":"简短摘要，如：嗓子不适",'
            '"detail":"一句话说明依据（引用用户原话或客观描述，不评价）",'
            '"intensity":1-5（严重程度）,'
            '"confidence":0-1（你有多确定）}],'
            '"resolved_events":[{"event_id":整数,"reason":"一句话依据"}],'
            '"sleep_detected":true或false（用户是否表达了正要去睡觉，见规则9）}\n\n'
            "规则：\n"
            "1. 只提取对话中【新出现】的状态，忽略已经过去很久的旧事。\n"
            "2. 用户用任何自然语言表达不适都算（如'嗓子像吞刀片''整个人被抽空'），不要只认关键词。\n"
            "3. 【事件结束判定】对比下方【当前活跃关怀事件】与对话片段：用户明确表示某状态已结束"
            "（'好了/痊愈/退烧了/不疼了/没事了/不难受了/喉咙不难受了'等，任何自然说法都算，"
            "不要求与原事件用词一致），把对应事件的 event_id 放进 resolved_events 并写明 reason。\n"
            "4. 【关键：按病因精确关闭】事件按病因（cause）独立存在，恢复也按病因精确匹配：\n"
            "   - 用户说'感冒好了'→ 只关闭感冒相关的（含其典型伴随症状，如咳嗽/嗓子——你判断是否同源）；\n"
            "   - 独立病因的事件（如胃部反酸、头痛、扭伤），用户没明确提到恢复，绝不关闭；\n"
            "   - 拿不准是不是同源时，倾向保守——只关你确信的。\n"
            "5. 【病因归因】new_states 的 cause 字段从下方【病因清单】中选择最匹配的；"
            "语义确实对不上任何现有 cause（如'过敏'）时，才新增一个简短 cause 名。\n"
            "6. 用户提到熬夜/没睡/失眠→late_night；加班/太累/累死→tired；难过/焦虑/压力大→mood；"
            "吃辣/吃冰/吃烧烤→diet；发烧/头疼/嗓子疼→sick；受伤/摔了→injury。\n"
            "7. 普通日常闲聊、与用户状态无关的内容，不要提取。\n"
            "8. 没有新状态时 new_states 输出 []，没有事件结束时 resolved_events 输出 []。\n"
            "9. 【晚安识别】判断用户是否明确表达了『正要去睡觉/休息』：直接说晚安、睡了、"
            "去睡觉等直白说法，以及委婉说法（'我撑不住了''眼皮打架了''先下了''困到不行'等，"
            "任何自然表达都算）都算。若是则 sleep_detected 置 true，否则 false。"
            "注意：'还没睡''睡不着''熬夜''刚睡醒'等不表示『正要去睡』，置 false。\n"
            "10. 【状态推翻检测】提取新状态前，先检查同一对话片段内用户后续消息是否推翻了"
            "前面的表述——如先说'我要睡了'后又说'我没睡/我又醒了'，或先说'好累'后说"
            "'其实还好'。若后续消息明确推翻或否定该状态，则不要提取（new_states 里排除），"
            "sleep_detected 也置 false。只提取在整个片段中仍然成立的状态。\n"
        )
        lines = []
        if new_start > 0:
            lines.append("【此前对话（已看过，仅作上下文参考，不要重复提取）】")
            for m in user_msgs[:new_start]:
                lines.append(f"- {m['content'][:120]}")
            lines.append("")
            lines.append("【新增对话（本次观察重点，只从这里提取新出现的变化）】")
            for m in user_msgs[new_start:]:
                lines.append(f"- {m['content'][:120]}")
        else:
            for m in user_msgs:
                lines.append(f"- {m['content'][:120]}")
        user = (
            f"关怀对象（用户本人）：{target_name}\n"
            f"病因清单（cause 从中选择）：\n"
            + self._format_causes(causes)
            + f"当前活跃关怀事件：\n"
            + self._format_active_events(active_events)
            + f"对话片段（按时间顺序）：\n"
            + "\n".join(lines)
            + "\n\n请输出 JSON。"
        )
        return system, user

        return system, user

    async def reflect(self, history) -> list[int]:
        """反思对话历史，返回写入的关怀事件 id 列表。

        v5.6.4 增量衔接（内容锚点版）：
        AstrBot 历史消息无时间戳，因此不依赖游标时间戳，改为记录上次反思
        看到的最后一条 user 消息内容（锚点）。本次从历史中定位锚点最后一次
        出现的位置，只反思它之后的新对话，并带最近 3 条旧消息做衔接；
        锚点丢失（历史被截断）时降级为最近 24 条全量；无新消息时跳过 LLM。

        v5.7.0 语义判定结束：
        反思时注入当前活跃状态事件清单（带 event_id），LLM 对比近期对话，
        通过 resolved_events 字段直接关闭语义上已结束的事件；recovery 类型
        与正则匹配保留为兜底。
        """
        target = self.db.get_default_target()
        if not target:
            return []
        user_msgs = self._extract_user_messages(history)
        if not user_msgs:
            return []
        # v5.7.1：先把窗口内消息落库到流水表（插件侧全量历史副本，供补扫）
        self.db.stream_append(target["id"], [m["content"] for m in user_msgs])
        anchor = str(self.db.kv_get("reflect_anchor", "") or "")
        start = 0
        anchored = False
        if anchor:
            # 从后往前定位锚点最后一次出现的位置
            for i in range(len(user_msgs) - 1, -1, -1):
                if user_msgs[i]["content"].startswith(anchor) or anchor in user_msgs[i]["content"]:
                    start = i + 1
                    anchored = True
                    break
        if not anchored:
            # 首次反思 / 锚点已被挤出历史：最近 24 条全量
            window = user_msgs[-24:]
            new_start = 0
        else:
            if start >= len(user_msgs):
                return []  # 没有新消息，跳过 LLM 调用
            ctx_start = max(0, start - 3)   # 衔接：起点前最多 3 条旧消息
            window = user_msgs[ctx_start:]
            new_start = start - ctx_start
            if len(window) > 43:            # 上限保护：新增区最多 40 条
                cut = len(window) - 43
                window = window[cut:]
                new_start = max(0, new_start - cut)

        # v5.7.0：注入活跃状态事件，供 LLM 语义对比判定哪些已结束
        active_state_events = [
            e for e in self.db.get_active_events(target["id"]) if e["source"] == "state"
        ]
        system, user = self._build_prompt(window, target["name"], new_start, active_state_events)
        try:
            text = await self.llm_func(system, user)
            parsed = self._parse_items(text)
            items = parsed["new_states"]
            logger.info(f"[DailyCare] 反思 LLM 原始输出: {str(text)[:500]}")
        except Exception as e:
            logger.warning(f"[DailyCare] 状态反思 LLM 调用失败: {e}")
            return []

        # LLM 调用成功：把锚点推进到最后一条 user 消息（防止重复反思同一段对话）
        if user_msgs:
            self.db.kv_set("reflect_anchor", user_msgs[-1]["content"][:60])

        # v5.8.2：主分析成功 → 本次窗口内消息标 seen（已被 LLM 覆盖）
        self.db.stream_mark_seen_by_hashes(
            target["id"],
            {hashlib.md5(m["content"].encode("utf-8")).hexdigest() for m in user_msgs},
        )

        # v1.1.5：晚安识别 LLM 兜底——关键词未覆盖的委婉表达由反思 LLM 判定。
        # 防重复：若休息窗口已在生效（关键词已命中）且距上次标记不足 1 小时，
        # 视为同一次晚安，不重复顺延窗口；否则以当前时间为锚点设置休息窗口。
        if parsed.get("sleep_detected"):
            try:
                from .rest import mark_sleep, rest_since_ts, in_rest_window
                now = now_ts()
                since = rest_since_ts(self.db)
                if not in_rest_window(self.db) or (now - since) > 3600:
                    hours = int(self.config.get("rest_after_sleep_hours", 7) or 7)
                    mark_sleep(self.db, after_hours=hours)
                    logger.info("[DailyCare] 反思 LLM 兜底：检测到用户表达晚安，进入动态休息窗口")
            except Exception as e:
                logger.debug(f"[DailyCare] 反思晚安兜底异常(可忽略): {e}")

        # v5.7.1：统一由 _apply_new_states 处理新状态（含 recovery 兜底），主分析与补扫复用
        created = self._apply_new_states(target["id"], parsed["new_states"])
        resolved = self._apply_llm_resolutions(target["id"], parsed["resolved_events"])
        if created:
            logger.info(f"[DailyCare] 状态反思完成，写入 {len(created)} 条状态事件")
        if resolved:
            logger.info(f"[DailyCare] LLM 语义判定关闭 {resolved} 条状态事件")

        # v5.8.2：补扫 seen=0 的消息（粗筛精判 + 未看型全量打包精判）
        backfill_created, backfill_resolved = await self._backfill_scan(target)
        created.extend(backfill_created)
        self.db.kv_set("reflect_cursor_seq", self.db.stream_max_seq(target["id"]))
        self.db.kv_set("last_reflect_resolved", resolved + backfill_resolved)
        return created

    def _apply_new_states(self, target_id: int, items: list) -> list:
        """处理 LLM 输出的新状态列表，写入关怀事件。recovery 走兜底关闭，不新建。

        v5.8.0：事件按 (type, cause) 归并去重——同病因重复事件合并，
        不再因摘要措辞不同而爆炸；cause 动态化，现有清单对不上时 LLM 新增。
        """
        created = []
        for it in items or []:
            stype = str(it.get("type", "other") or "other")
            if stype not in STATE_TYPES:
                stype = "other"
            summary = str(it.get("summary", "")).strip()
            if not summary:
                continue
            label = STATE_LABEL.get(stype, "其他")
            if stype == "recovery":
                # 恢复信号（兜底路径）：按 cause 精确关闭，不新建
                self._resolve_by_type(target_id, summary, str(it.get("detail", "") or ""),
                                      str(it.get("cause", "") or ""))
                continue
            detail = str(it.get("detail", "")).strip()
            try:
                intensity = int(it.get("intensity", STATE_TYPES[stype]["intensity"]))
            except Exception:
                intensity = STATE_TYPES[stype]["intensity"]
            intensity = max(1, min(5, intensity))
            cfg = STATE_TYPES[stype]
            ttl = cfg["ttl"]
            # v5.8.0：cause 归因 + 动态新增
            cause = str(it.get("cause", "") or "").strip()
            if cause:
                self.db.upsert_cause(stype, cause, "")
            eid = self.db.add_event(
                target_id=target_id,
                source="state",
                summary=f"{label}：{summary}",
                detail=detail,
                event_type=stype,
                cause=cause,
                intensity=intensity,
                priority=cfg.get("priority", 1),
                ttl_hours=ttl,
            )
            created.append(eid)
        return created

    # ---------- v5.7.1 窗口外补扫（粗筛 + 精判） ----------
    def _forward_keywords(self) -> list:
        """动态组装正向粗筛词：care_causes 所有 cause 的 keywords + 静态兜底集。

        v5.8.1 动态化：LLM 反思归因时动态新增的 cause，其 keywords 会自动
        进入粗筛词库——窗口外补扫无需改代码即可覆盖新病因。
        """
        kws = set(FORWARD_KEYWORDS)
        try:
            for c in self.db.list_causes():
                for kw in (c.get("keywords") or "").split(","):
                    kw = kw.strip()
                    if kw:
                        kws.add(kw)
        except Exception:
            pass  # 读库失败时退化为静态兜底集
        return list(kws)

    def _keyword_hit(self, text: str) -> bool:
        """关键词粗筛：动态正向不适词或静态恢复词任一命中即触发精判
        （只决定要不要精判，不决定结论）。"""
        for kw in self._forward_keywords() + RECOVERY_KEYWORDS:
            if kw in text:
                return True
        return False

    async def _backfill_scan(self, target: dict) -> tuple:
        """补扫流水表中 seen=0（从未被任何 LLM 覆盖）的消息。v5.8.2 seen 方案。

        两阶段：
        1. 粗筛精判：seen=0 的消息做关键词粗筛（动态病因词 + 恢复词），命中者
           按 seq 分组（相邻窗口重叠合并），每组取前后各 5 条上下文交 LLM 语义精判。
        2. 未看型全量：seen=0 且已出框架窗口（seq 落后 max_seq 超过 WINDOW）
           的消息——它们从未被任何 LLM 看过，粗筛漏掉也无妨，按 10 条一组
           打包精判，不依赖关键词。

        每次补扫精判预算：粗筛最多 3 块 + 未看型全量最多 2 组，防 token 爆炸。
        精判成功的消息标 seen，保证幂等；失败的保持 seen=0 下次重试。

        返回 (created_ids, resolved_count)。
        """
        created = []
        resolved_total = 0
        max_seq = self.db.stream_max_seq(target["id"])

        # 阶段1：粗筛精判（seen=0 且关键词命中）
        pending = self.db.stream_unseen(target["id"])
        if pending:
            hits = [m for m in pending if self._keyword_hit(m["content"])]
            if hits:
                hits.sort(key=lambda m: m["seq"])
                groups = [[hits[0]]]
                for m in hits[1:]:
                    if m["seq"] - groups[-1][-1]["seq"] <= 11:  # 前后各5轮窗口重叠 → 合并
                        groups[-1].append(m)
                    else:
                        groups.append([m])
                for g in groups[:3]:
                    c, r = await self._judge_block(target, g)
                    created.extend(c)
                    resolved_total += r

        # 阶段2：未看型全量（seen=0 且已出窗口，最多 2 组）
        window = self.WINDOW_SIZE  # 框架窗口估算（50轮≈100条，留余量）
        old = self.db.stream_unseen_old(target["id"], max_seq - window, limit=200)
        if old:
            for i in range(0, min(len(old), 20), 10):  # 最多 2 组 × 10 条
                group = old[i:i + 10]
                c, r = await self._judge_block(target, group)
                created.extend(c)
                resolved_total += r

        if created:
            logger.info(f"[DailyCare] 补扫精判完成，写入 {len(created)} 条状态事件")
        return created, resolved_total

    async def _judge_block(self, target: dict, group: list) -> tuple:
        """对一个消息组做 LLM 语义精判（粗筛命中块带前后文 / 未看型组原样）。

        精判成功（LLM 正常返回）后，把组内全部消息标 seen——无论 LLM 是否
        提取出新状态，看过就算覆盖，保证幂等不重复扫。

        返回 (created_ids, resolved_count)。
        """
        lo = max(1, group[0]["seq"] - 5)
        hi = group[-1]["seq"] + 5
        ctx = self.db.stream_between(target["id"], lo, hi)
        user_msgs = [{"role": "user", "content": m["content"]} for m in ctx]
        if not user_msgs:
            return [], 0
        active = [e for e in self.db.get_active_events(target["id"]) if e["source"] == "state"]
        system, user = self._build_prompt(user_msgs, target["name"], 0, active)
        try:
            text = await self.llm_func(system, user)
            parsed = self._parse_items(text)
            logger.info(f"[DailyCare] 补扫精判原始输出: {str(text)[:400]}")
        except Exception as e:
            logger.warning(f"[DailyCare] 补扫精判 LLM 调用失败: {e}")
            return [], 0
        # 精判成功：组内消息标 seen（幂等）
        self.db.stream_mark_seen_by_ids(target["id"], [m["id"] for m in ctx])
        created = self._apply_new_states(target["id"], parsed["new_states"])
        resolved = self._apply_llm_resolutions(target["id"], parsed["resolved_events"])
        if resolved:
            logger.info(f"[DailyCare] 补扫精判关闭 {resolved} 条状态事件")
        return created, resolved

    def _apply_llm_resolutions(self, target_id: int, resolutions: list) -> int:
        """按 LLM 判定的 event_id 关闭状态事件（v5.7.0 主路径）。

        只处理 source='state' 且仍 active、且属于该目标的事件；
        weather 等外部事件不受 LLM 反思影响。
        """
        resolved = 0
        for res in resolutions or []:
            if not isinstance(res, dict):
                continue
            raw = res.get("event_id")
            if raw is None:
                continue
            try:
                eid = int(raw)
            except Exception:
                continue
            ev = self.db.get_event(eid)
            if not ev or ev["status"] != "active" or ev["source"] != "state":
                continue
            if ev["target_id"] != target_id:
                continue
            self.db.resolve_event(eid)
            self.db.cancel_plans_by_event(eid)
            resolved += 1
        return resolved

    def _resolve_by_type(self, target_id: int, summary: str, detail: str = "",
                         cause: str = "") -> None:
        """根据恢复信号关闭相关 active 状态事件（兜底路径，v5.8.0 按 cause 精确关闭）。

        匹配策略（v5.8.0）：
        1. LLM 已给 cause：直接关闭该 cause 下的 active 事件（含旧数据兜底）。
        2. 未给 cause 时：从恢复信号提取症状词，匹配病因表 keywords，
           命中 cause 后精确关闭；多个 cause 命中则都关。
        3. 不再按大类全关——独立病因（胃部/头痛等）用户没明确提到时绝不误伤。
        """
        import re
        _recover = r"([一-龥]{1,8}?)(?:彻底|完全|已经|都|基本|差不多)?(?:好了|痊愈|恢复了|好多了|退烧了|退了|没事了|不难受了|不咳了|不疼了|不痛了)"
        _suffix = r"(已经|都)?(好了|痊愈|恢复了|好多了|好了不少|没事了|没什么事了|不咳了|不疼了|退烧了|退了|不难受了|不痛了)$"
        _prefix = r"^(用户|我|他|她|自己)(说|提到|称|讲|觉得|感觉)?[:：]?"

        if cause:
            # 路径1：LLM 指定 cause → 精确关闭该 cause 全部 active 事件
            causes = self.db.list_causes()
            match_type = ""
            for cc in causes:
                if cc["cause"] == cause:
                    match_type = cc["type"]
                    break
            active = [e for e in self.db.get_active_events(target_id) if e["source"] == "state"]
            hit = []
            for e in active:
                if e.get("cause") == cause:
                    hit.append(e["id"])
                elif not e.get("cause"):
                    # 旧数据兜底：cause 为空（可能 type 也为空），summary/detail 含 cause 词即关
                    joined = (e["summary"] or "") + (e.get("detail") or "")
                    if cause in joined:
                        hit.append(e["id"])
            self._resolve_ids(target_id, hit, summary)
            return

        # 路径2：提取症状词 → 匹配病因表 keywords → 精确关闭
        texts = [summary, detail]
        cores = []
        for text in texts:
            if not text:
                continue
            m = re.search(_recover, text)
            if m and m.group(1):
                cores.append(m.group(1))
            core = re.sub(_prefix, "", text).strip()
            core = core.strip('“”"\'')
            core = re.sub(_suffix, "", core).strip()
            if core:
                cores.append(core)
        seen = set()
        clean = []
        for c in cores:
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                clean.append(c)
        if not clean:
            return  # 提取不到症状词，保守不关

        causes = self.db.list_causes()
        hit_causes = set()
        for c in clean:
            for cc in causes:
                kws = (cc.get("keywords") or "").split(",")
                if any(kw and kw in c for kw in kws) or c in (cc.get("cause") or ""):
                    hit_causes.add((cc["type"], cc["cause"]))
        if not hit_causes:
            return
        active = self.db.get_active_events(target_id)
        hit = []
        for t, c in hit_causes:
            cause_row = next((cc for cc in causes if cc["type"] == t and cc["cause"] == c), None)
            kws = (cause_row.get("keywords") or "").split(",") if cause_row else []
            for e in active:
                if e["source"] != "state" or e["status"] != "active":
                    continue
                if e["id"] in hit:
                    continue
                if e.get("type") == t and e.get("cause") == c:
                    hit.append(e["id"])
                elif not e.get("cause"):
                    # 旧数据兜底：cause 为空（可能 type 也为空），症状词命中即关
                    joined = (e["summary"] or "") + (e.get("detail") or "")
                    if any(kw and kw in joined for kw in kws) or c in joined:
                        hit.append(e["id"])
        self._resolve_ids(target_id, hit, summary)

    def _resolve_ids(self, target_id: int, ids: list, summary: str) -> int:
        """按 id 列表关闭事件并取消关联计划。返回关闭数。"""
        resolved = 0
        for eid in ids:
            ev = self.db.get_event(eid)
            if not ev or ev["status"] != "active" or ev["source"] != "state":
                continue
            if ev["target_id"] != target_id:
                continue
            self.db.resolve_event(eid)
            self.db.cancel_plans_by_event(eid)
            resolved += 1
        if resolved:
            logger.info(f"[DailyCare] 恢复信号 '{summary}' 关闭了 {resolved} 条状态事件")
        return resolved

    @staticmethod
    def _parse_items(text: str) -> dict:
        """解析 LLM 输出。

        返回 {"new_states": [...], "resolved_events": [...]}。
        兼容两种格式：
        - 新版对象：{"new_states":[...], "resolved_events":[...]}
        - 旧版数组：[...]（视为 new_states，resolved_events 为空）
        """
        if not text:
            return {"new_states": [], "resolved_events": [], "sleep_detected": False}
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        # 先尝试对象格式（必须带 new_states/resolved_events 键，否则可能是数组被误切）
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                if isinstance(data, dict) and ("new_states" in data or "resolved_events" in data):
                    ns = data.get("new_states")
                    re_ = data.get("resolved_events")
                    sd = data.get("sleep_detected")
                    return {
                        "new_states": ns if isinstance(ns, list) else [],
                        "resolved_events": re_ if isinstance(re_, list) else [],
                        "sleep_detected": bool(sd),
                    }
            except Exception:
                pass
        # 再尝试旧版数组格式
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                if isinstance(data, list):
                    return {"new_states": data, "resolved_events": [], "sleep_detected": False}
            except Exception:
                pass
        return {"new_states": [], "resolved_events": [], "sleep_detected": False}


class WeatherJudge:
    """天气判断：LLM 决定当前天气是否值得主动提醒，产出事实背景。"""

    def __init__(self, db: CareDatabase, config: dict, llm_func):
        self.db = db
        self.config = config
        self.llm_func = llm_func

    # ---------- 冷却 ----------
    def _cooldown_key(self, location_id: int) -> str:
        return f"weather_remind_cooldown_{location_id}"

    def _in_cooldown(self, location_id: int, hours: int = 6) -> bool:
        """同类天气提醒冷却：默认 6 小时内同一地点不重复提醒。"""
        if hours <= 0:
            return False
        last = self.db.kv_get(self._cooldown_key(location_id), 0)
        return (now_ts() - last) < hours * 3600

    def _mark_reminded(self, location_id: int) -> None:
        now = now_ts()
        self.db.kv_set(self._cooldown_key(location_id), now)
        self.db.kv_set("last_weather_remind_ts", now)

    # ---------- LLM 判断 ----------
    def _build_prompt(self, location_name: str, wx: dict, last_remind_desc: str) -> tuple[str, str]:
        system = (
            "你是天气关怀判断器。给定用户所在城市的天气数据，判断此刻是否值得主动提醒用户。\n"
            "你只负责判断，绝不生成关怀话术、建议或完整句子文案。\n\n"
            "只输出一个 JSON 对象（不要 markdown 代码块，不要额外解释）：\n"
            '{"should_remind": true或false, "background": "一句话客观事实", "reason": "简短理由"}\n\n'
            "规则：\n"
            "1. 只有明显影响出行、健康或舒适度的天气才值得提醒：暴雨/雷雨、台风、冰雹、"
            "极端高温或低温、剧烈降温（24h 降幅≥8°C）、大雾、大风、空气严重污染等。\n"
            "2. 普通晴天、多云、小雨、一般性降水，不要提醒。\n"
            "3. background 必须是【客观事实】，如'你所在的城市今晚 20 点后有雷阵雨，气温降至 18°C'。"
            "绝对不要写成建议或话术（不要出现'记得''小心''带上'等词）。\n"
            "4. 若距离上次同类提醒不到 6 小时且天气没有明显变化，should_remind 应为 false。\n"
            "5. 拿不准时倾向不提醒（宁缺毋滥）。\n"
        )
        # 精简天气数据，避免塞太多 token
        brief = {
            "now": wx.get("now", {}),
            "hourly_next_12h": (wx.get("hourly") or [])[:12],
            "alerts": wx.get("alerts") or [],
        }
        user = (
            f"用户所在城市：{location_name}\n"
            f"上次同类提醒：{last_remind_desc}\n"
            f"当前天气数据：\n{json.dumps(brief, ensure_ascii=False)}\n\n"
            "请判断是否值得提醒，输出 JSON。"
        )
        return system, user

    async def judge(self, location_id: int, location_name: str, wx: dict,
                    cooldown_hours: int = 6) -> Optional[str]:
        """判断某地点天气是否值得提醒。返回 background 事实背景；不值得返回 None。"""
        if self._in_cooldown(location_id, cooldown_hours):
            return None
        last = self.db.kv_get(self._cooldown_key(location_id), 0)
        last_desc = datetime.fromtimestamp(last).strftime("%m-%d %H:%M") if last else "无"
        system, user = self._build_prompt(location_name, wx, last_desc)
        try:
            text = await self.llm_func(system, user)
            data = self._parse_json(text)
        except Exception as e:
            logger.warning(f"[DailyCare] 天气判断 LLM 调用失败: {e}")
            return None
        if not data:
            return None
        if data.get("should_remind") is True:
            background = str(data.get("background", "")).strip()
            if background:
                self._mark_reminded(location_id)
                logger.info(f"[DailyCare] {location_name} 天气值得提醒: {background}")
                return background
        return None

    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
