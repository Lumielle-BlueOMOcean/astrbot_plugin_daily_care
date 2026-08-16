# -*- coding: utf-8 -*-
"""
微光-Daily Care - 决策层（v5 新增）
架构升级：把"要不要开口、何时开口、以什么背景开口"的决策权交给 LLM，
而不是算法规则。参考 angel_heart / angel_memory 的独立 LLM 判断模式。

DecisionEngine 输入：
- 活跃关怀事件（天气 / 状态）
- 当前时间 / 时段
- 今日已开口次数与上限
- 最近提醒历史（48h，防重复，但不硬性去重——保留"唠叨"）
- 上次开口时间（冷却）

决策输出（结构化 JSON）：
- decision: act（现在开口）/ plan（规划未来关怀）/ silent（不开口）
- background: 客观事实背景（act/plan 时必填，绝不含话术）
- plan_date: 未来关怀日期 YYYY-MM-DD（plan 时必填）
- trigger_window: morning/noon/evening/night（plan 时必填，执行层窗口内随机时刻触发）
- reason: 简短理由

核心原则：
1. 决策 LLM 只决定"要不要 + 背景 + 时间"，绝不生成话术——话永远由 bot 本人开口时说。
2. 触发时间是"随机区间"而非固定时间点，模拟"想起来就关心"的人类节奏。
3. 防重复靠"让决策 LLM 看到最近说过什么"，而非硬规则去重。
"""
import json
import re
import time
from datetime import datetime, date, timedelta
from typing import Any, Optional

from astrbot.api import logger

from .database import CareDatabase

# 关怀窗口定义（与 executor 对齐）
WINDOWS = {
    "morning": (8, 11),    # 早上 8-11 点
    "noon": (11, 14),      # 中午 11-14 点
    "evening": (17, 20),   # 傍晚 17-20 点
    "night": (20, 23),     # 晚上 20-23 点
}

WINDOW_LABEL = {
    "morning": "早上", "noon": "中午", "evening": "傍晚", "night": "晚上",
}


def _parse_note_windows(cfg_val) -> list:
    """解析常态提示倾向时段配置为列表。

    兼容三种格式：JSON 数组字符串（'["morning","07:00-08:00"]'）、
    逗号分隔（'morning,noon'）、单个（'morning'）。
    """
    if not cfg_val:
        return []
    if isinstance(cfg_val, list):
        return [str(x).strip() for x in cfg_val if str(x).strip()]
    s = str(cfg_val)
    try:
        arr = json.loads(s)
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if str(x).strip()]
    except Exception:
        pass
    return [x.strip() for x in s.split(",") if x.strip()]


def _in_note_window(now, windows: list) -> bool:
    """当前时刻是否命中任一倾向时段。

    预设窗口名（morning/noon/evening/night）直接查 WINDOWS；
    自定义时段（HH:MM-HH:MM）按分钟区间判断，支持跨天（22:00-02:00）。
    """
    if not windows:
        return False
    cur = now.hour * 60 + now.minute
    for w in windows:
        if w in WINDOWS:
            sh, eh = WINDOWS[w]
            if sh * 60 <= cur < eh * 60:
                return True
        else:
            m = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", w)
            if m:
                s = int(m.group(1)) * 60 + int(m.group(2))
                e = int(m.group(3)) * 60 + int(m.group(4))
                if s < e:
                    if s <= cur < e:
                        return True
                elif cur >= s or cur < e:
                    return True
    return False


def window_of_now(now: Optional[datetime] = None) -> Optional[str]:
    """当前时刻属于哪个关怀窗口"""
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    for name, (sh, eh) in WINDOWS.items():
        if sh * 60 <= cur < eh * 60:
            return name
    return None


def random_ts_in_window(plan_date: str, window: str, now_ts: int = 0) -> int:
    """在指定日期的窗口内随机生成一个触发时刻（时间戳）。

    若窗口日期是今天且窗口尚未结束，则在「当前时刻 + 5分钟」到「窗口结束」之间随机，
    避免生成已经过去的时刻。
    """
    import random as _r
    sh, eh = WINDOWS.get(window, WINDOWS["evening"])
    try:
        d = datetime.strptime(plan_date, "%Y-%m-%d")
    except Exception:
        d = datetime.now()
    day_start = datetime(d.year, d.month, d.day)
    lo = int(day_start.replace(hour=sh, minute=0).timestamp())
    hi = int(day_start.replace(hour=eh, minute=0).timestamp())
    now_ts = now_ts or int(time.time())
    if lo < now_ts < hi:
        lo = now_ts + 300  # 至少 5 分钟后
    if lo >= hi:
        # 窗口剩余不足 5 分钟：下限收到窗口结束前 5 分钟
        lo = hi - 300
    if lo < now_ts:
        # 下限仍早于当前（极端边界）：至少 1 分钟后，且绝不越过窗口结束
        lo = now_ts + 60
        if lo >= hi:
            return hi - 60  # 窗口几乎结束：取结束前 1 分钟，保证不越界
    return _r.randint(lo, hi)


class DecisionEngine:
    """关怀决策引擎：LLM 决定是否开口 / 何时开口 / 以什么背景开口。"""

    def __init__(self, db: CareDatabase, config: dict, llm_func):
        self.db = db
        self.config = config
        self.llm_func = llm_func  # async (system_prompt, user_prompt) -> str
        self._guarantee = False

    # ---------- 输入构建 ----------
    def _collect_context(self, target: dict) -> dict:
        """收集决策所需的全部上下文（事件、时间、频率、提醒历史）。"""
        events = self.db.get_active_events(target["id"])
        # 按来源分组
        weather_events = [e for e in events if e["source"] == "weather"]
        state_events = [e for e in events if e["source"] == "state"]
        # 今日开口
        today = date.today().isoformat()
        today_count = self.db.count_send_today(target["id"], today)
        # 关怀积极度 1-10（WebUI 拖条）：越高越倾向主动开口
        try:
            care_level = int(self.config.get("care_level", 5))
        except (TypeError, ValueError):
            care_level = 5
        care_level = max(1, min(10, care_level))
        # 关怀消息每日上限（天气/状态/计划），与主动消息上限独立
        daily_limit = int(self.config.get("care_daily_limit", 2))
        today_count = self.db.count_send_today(target["id"], today, "care")
        # 上次开口
        last_send = self.db.kv_get(f"last_care_send_{target['id']}", 0)
        # 最近提醒历史（48h）
        reminders = self.db.get_recent_reminders(target["id"], hours=48)
        # 活跃事件摘要
        ev_lines = []
        for e in events[:6]:
            created = datetime.fromtimestamp(e["created_at"]).strftime("%m-%d %H:%M")
            # v1.01：事件带上 cause 标识（如 weather/daily-note），供决策层识别常态天气提示
            tag = e["source"] + (f"/{e['cause']}" if e.get("cause") else "")
            ev_lines.append(
                f"- [{tag}] {created}：{e['summary']}"
                + (f"（{e['detail']}）" if e.get("detail") and e["source"] == "state" else "")
            )
        # 常态天气提示倾向时段（1.0.0：多选+自定义，解析为列表）
        daily_note_windows = _parse_note_windows(
            self.config.get("daily_weather_note_window", '["morning"]')
        )
        # 静默时长（冷场主动消息用）
        last_user = self.db.kv_get("last_user_msg_ts", 0)
        silence_min = int((time.time() - last_user) / 60) if last_user else 0
        return {
            "events": ev_lines,
            "weather_count": len(weather_events),
            "state_count": len(state_events),
            "today": today,
            "today_count": today_count,
            "daily_limit": daily_limit,
            "care_level": care_level,
            "last_send": datetime.fromtimestamp(last_send).strftime("%m-%d %H:%M") if last_send else "无",
            "reminders": reminders,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "window": window_of_now() or "无",
            "silence_minutes": silence_min,
            "daily_note_windows": daily_note_windows,
            "in_note_window": _in_note_window(datetime.now(), daily_note_windows),
            "target_name": target.get("name", "你"),
            "guarantee": self._guarantee,
        }

    def _build_prompt(self, ctx: dict) -> tuple[str, str]:
        system = (
            "你是关怀决策引擎。基于感知到的信息，决定此刻是否值得主动关怀用户，以及关怀的时间安排。\n"
            "你只负责决策和提供客观事实背景，绝对不要生成关怀话术、完整句子或建议文案。\n\n"
            "只输出一个 JSON 对象（不要 markdown 代码块，不要额外解释）：\n"
            '{"decision":"act|plan|silent", "background":"一句话客观事实", '
            '"plan_date":"YYYY-MM-DD（仅plan时需要）", "trigger_window":"morning|noon|evening|night（仅plan时需要）", '
            '"category":"weather|state|proactive（可选，本次开口的归属类别）", "reason":"简短理由"}\n\n'
            "决策规则：\n"
            "1. act：存在值得【此刻】开口的情况——例如刚出现的恶劣天气（暴雨/雷电/台风/极端温度）、"
            "用户刚表达的身体不适或情绪波动、或者处于合适的关怀时段且有明确的关怀理由。\n"
            "2. plan：当前不适合立刻开口，但值得在【未来】某时段关怀——例如发现用户生病，"
            "可以规划明天或后天早上的问候；天气预警在明天，可以规划明天对应时段的提醒。\n"
            "3. silent：没有值得关怀的理由，或今日关怀已接近上限，或距离上次开口太近（避免打扰）。\n"
            "4. background 必须是客观事实（如'你所在的城市明天傍晚有暴雨'），不要出现'记得''小心''带上'等词。\n"
            "5. 参考『最近提醒过』的内容：如果同类提醒刚说过且情况没有新变化，倾向 silent；"
            "如果情况有实质进展（雨真的下起来了、用户说更难受了），可以换个角度再开口——唠叨是人类关怀的一部分。\n"
            "6. 今日关怀次数若已达到上限，除非有极端情况，否则 silent。\n"
            "7. 拿不准时倾向 silent（宁缺毋滥）。\n"
            "7b. 关怀积极度（1-10，越高越主动）：积极度 >= 7 时，理由一般也可以考虑 act；"
            "积极度 <= 3 时，除非有极端情况（恶劣天气/明显不适），否则倾向 silent。\n"
            "8. 若『对话已静默』信息存在且静默时间较长（≥120分钟）、今日开口不多，"
            "可以考虑 act——这是主动开启话题的自然契机，background 写客观事实如"
            "'我们有一阵子没说话了'；但若静默时间过短（<60分钟）不要 act，避免打扰。\n"
            "9. 若事件中有『常态天气提示』（cause 含 daily-note）：这是每天固定的天气问候，"
            "属于低优先级日常关怀——若『当前在倾向时段内』为真（见用户输入）且并非"
            "勿扰时段、今日开口不多，倾向 act，background 直接用该事件的天气事实；"
            "若刚提醒过同类内容（见『最近提醒过』）则 silent，避免重复。\n"
            "10. category 字段（仅 act 时需要）：若 background 引用的是天气事实（如天气预警、显著变化、常态天气问候），输出 weather；若引用用户状态（不适/情绪/作息等），输出 state；若因静默冷场主动开启话题，输出 proactive。这用于消息分轨计数，请如实归类。\n"
        )
        if ctx.get("guarantee"):
            system += (
                "\n【强制规则·保底触发】本次是『最长静默保底』触发的主动消息："
                "用户已经静默超过最大等待窗口，这次【必须】开口——"
                "这是插件对用户的承诺，不开口就违背了约定。\n"
                "因此：\n"
                "- 禁止输出 silent。\n"
                "- 优先输出 act。\n"
                "- 仅当处于勿扰时段或当前确实极不适合打扰时，才允许输出 plan，"
                "且 plan_date 必须是【今天】，trigger_window 必须是今天尚未结束的窗口，"
                "绝对禁止把开口推到明天或更远。\n"
                "- background 必须给出一个真实、自然的开口理由（天气变化、"
                "用户近况、或单纯的'我们有一阵子没说话了'）。\n"
            )
        user_lines = [
            f"关怀对象：{ctx['target_name']}",
            f"当前时间：{ctx['now']}（所处关怀时段：{ctx['window'] or '无'}）",
            f"今日关怀消息 {ctx['today_count']}/{ctx['daily_limit']} 条",
            f"关怀积极度：{ctx['care_level']}/10（越高越倾向于主动开口）",
            f"上次开口：{ctx['last_send']}",
            f"常态天气提示倾向时段：{', '.join(ctx.get('daily_note_windows', []) or ['morning']) or '无'}（仅在存在常态天气事件时参考）"
            + (f"，当前{'在' if ctx.get('in_note_window') else '不在'}倾向时段内" if ctx.get('daily_note_windows') else ""),
        ]
        if ctx.get("guarantee"):
            user_lines.append("⚠️ 本次为保底触发：静默已达最大窗口，必须开口。")
        if ctx.get("silence_minutes"):
            user_lines.append(f"对话已静默 {ctx['silence_minutes']} 分钟（若值得可以主动开启话题）")
        if ctx["events"]:
            user_lines.append("感知到的事件：\n" + "\n".join(ctx["events"]))
        else:
            user_lines.append("感知到的事件：无")
        if ctx["reminders"]:
            user_lines.append(
                "最近 48 小时提醒过用户的内容：\n"
                + "\n".join(f"- {r[:80]}" for r in ctx["reminders"][-8:])
            )
        else:
            user_lines.append("最近 48 小时提醒过用户的内容：无")
        user_lines.append("请输出决策 JSON。")
        return system, "\n".join(user_lines)

    # ---------- 决策执行 ----------
    async def decide(self, target: dict, source: str = "auto", guarantee: bool = False) -> Optional[dict]:
        """执行一次决策。返回结构化决策 dict；失败返回 None。

        guarantee=True 表示『最长静默保底』命中：静默区间内必须开口，
        决策层不允许 silent，也不允许把开口无限后推（plan 只能落在今天）。
        """
        self._guarantee = bool(guarantee)
        ctx = self._collect_context(target)
        system, user = self._build_prompt(ctx)
        try:
            text = await self.llm_func(system, user)
            data = self._parse_json(text)
        except Exception as e:
            logger.warning(f"[DailyCare] 决策 LLM 调用失败: {e}")
            return None
        if not data:
            return None
        decision = str(data.get("decision", "silent")).strip().lower()
        if decision not in ("act", "plan", "silent"):
            decision = "silent"
        bg = str(data.get("background", "")).strip()
        reason = str(data.get("reason", "")).strip()
        plan_date = str(data.get("plan_date", "")).strip()
        window = str(data.get("trigger_window", "")).strip()
        if window not in WINDOWS:
            window = ""
        # v1.00：category 分轨（weather/state/proactive），供消息计数隔离；缺省按 state 处理
        category = str(data.get("category", "")).strip().lower()
        if category not in ("weather", "state", "proactive"):
            category = "state"
        result = {
            "decision": decision,
            "background": bg,
            "plan_date": plan_date,
            "trigger_window": window,
            "category": category,
            "reason": reason,
        }
        # ---- 保底硬约束：静默保底命中时，决不允许 silent 或把开口推到明天 ----
        if self._guarantee:
            if decision == "silent":
                decision = "act"
                if not bg:
                    bg = "我们有一阵子没说话了，有点惦记你"
                logger.info("[DailyCare] 保底触发：silent 被强制转为 act")
            elif decision == "plan":
                in_dnd = self._in_dnd()
                if not in_dnd:
                    # 非勿扰时段：plan 直接转 act，静默区间内必须立刻开口
                    decision = "act"
                    if not bg:
                        bg = "我们有一阵子没说话了，有点惦记你"
                    logger.info("[DailyCare] 保底触发：非勿扰时段，plan 强制转为 act")
                else:
                    # 勿扰时段：允许 plan 到今天最近窗口，绝不推到明天
                    try:
                        pd = datetime.strptime(plan_date, "%Y-%m-%d").date()
                    except Exception:
                        pd = None
                    today = date.today()
                    cur_h = datetime.now().hour
                    if pd != today or window not in WINDOWS or cur_h >= WINDOWS[window][1]:
                        plan_date = today.isoformat()
                        for wn, (sh, eh) in WINDOWS.items():
                            if cur_h < eh:
                                window = wn
                                break
                        else:
                            decision = "act"
                            logger.info("[DailyCare] 保底触发：勿扰中但今天无可用窗口，plan 转为 act")
                        if decision != "act":
                            logger.info(f"[DailyCare] 保底触发：勿扰时段，plan 已收敛到今天 {window}")
            result["decision"] = decision
            result["plan_date"] = plan_date
            result["trigger_window"] = window
        # 记录决策日志
        self.db.add_decision_log(
            source=source, decision=decision, background=bg,
            plan_date=plan_date, trigger_window=window, reason=reason,
        )
        logger.info(
            f"[DailyCare] 决策结果: {decision}"
            + (f" 背景={bg[:40]}..." if bg else "")
            + (f" 计划={plan_date}@{window}" if decision == "plan" else "")
        )
        return result

    # ---------- 计划落地 ----------
    def apply_plan(self, target: dict, decision: dict) -> Optional[int]:
        """把 plan 决策落地为关怀计划（care_plans），随机触发时刻在窗口内。"""
        if decision.get("decision") != "plan":
            return None
        bg = decision.get("background", "")
        plan_date = decision.get("plan_date", "")
        window = decision.get("trigger_window", "")
        if not bg or not plan_date or not window:
            return None
        # 校验日期格式，确保是未来日期（至少今天）
        try:
            d = datetime.strptime(plan_date, "%Y-%m-%d")
        except Exception:
            d = datetime.now()
            plan_date = d.strftime("%Y-%m-%d")
        if d.date() < date.today():
            d = datetime.now() + timedelta(days=1)
            plan_date = d.strftime("%Y-%m-%d")
        # 关联事件：找背景最匹配的活跃事件（无则 0）
        event_id = 0
        for ev in self.db.get_active_events(target["id"]):
            if ev["summary"] and bg and (ev["summary"][:12] in bg or bg[:12] in ev["summary"]):
                event_id = ev["id"]
                break
        # 生成窗口内随机触发时刻
        trigger_ts = random_ts_in_window(plan_date, window)
        pid = self.db.add_plan(
            event_id=event_id,
            target_id=target["id"],
            plan_date=plan_date,
            trigger_window=window,
            task_type="care",
            content_summary=bg,
        )
        # 补写随机触发时刻
        if pid:
            with self.db._connect() as conn:
                conn.execute(
                    "UPDATE care_plans SET trigger_ts=? WHERE id=?",
                    (trigger_ts, pid),
                )
            logger.info(f"[DailyCare] 已规划关怀: {plan_date}@{window}（{datetime.fromtimestamp(trigger_ts).strftime('%m-%d %H:%M')}）: {bg[:40]}...")
        return pid

    # ---------- 工具 ----------
    def _in_dnd(self) -> bool:
        """是否处于勿扰时段（与 executor.in_dnd 对齐）"""
        dnd_start = str(self.config.get("dnd_start", "23:00"))
        dnd_end = str(self.config.get("dnd_end", "08:00"))
        try:
            sh, sm = map(int, dnd_start.split(":"))
            eh, em = map(int, dnd_end.split(":"))
        except Exception:
            return False
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        s = sh * 60 + sm
        e = eh * 60 + em
        if s < e:
            return s <= cur < e
        return cur >= s or cur < e

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