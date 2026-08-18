# -*- coding: utf-8 -*-
"""
微光-Daily Care - 休息窗口模块（v1.1 晚安识别）

背景：用户深夜说「晚安/睡了」后，若仅依赖勿扰时段（如 00:00-01:00），
插件会把「睡着没回消息」当成普通冷场，凌晨 5 点就开始发主动消息——严重打扰。

本模块实现「动态休息窗口」：
- 用户消息命中晚安关键词 → 记录休息起点，恢复时间 = 说晚安时刻 + rest_after_sleep_hours。
- 熬夜越晚恢复越晚（如凌晨 4 点说晚安，7 点不会去问早），与勿扰时段取较晚者。
- 用户主动发消息 → 立即打破休息窗口，恢复正常判定。
- 三处时间判定（monitor/decision/executor）统一调用本模块，避免重复实现。

设计取舍（尊重用户意见）：
- 只有检测到用户明确说晚安才进入动态窗口；没说晚安则仅依赖勿扰时段。
- 说晚安后用户又发消息 → 视为已醒，打破窗口。
"""
import re
import time
from typing import Optional


# ---------- 晚安/睡觉 关键词 ----------
_SLEEP_PATTERNS = [
    r"晚安",
    r"睡觉觉",
    r"碎觉",
    r"睡了睡了",
    r"我先睡",
    r"要睡(了|啦|觉)",
    r"去睡(觉)?([啦了]|吧)?",
    r"睡觉(觉)?([啦了]|吧)?",
    r"睡(了|啦|吧)([啦了]|吧|哦)?",
    r"困了",
    r"休息(了|啦)?",
    r"睡了哦",
    r"睡咯",
    r"眯一会",
]

# 排除：语义相反的表述
_EXCLUDE_PATTERNS = [
    r"还没睡",
    r"没睡",
    r"睡不着",
    r"睡不著",
    r"睡醒",
    r"不用睡",
    r"不睡了",
    r"熬夜",
    r"睡不着了",
]


def detect_sleep_text(text: str) -> bool:
    """检测用户消息是否表示「要去睡了」。命中返回 True。"""
    if not text:
        return False
    t = str(text).strip()
    if not t:
        return False
    for ex in _EXCLUDE_PATTERNS:
        if re.search(ex, t):
            return False
    for p in _SLEEP_PATTERNS:
        if re.search(p, t):
            return True
    return False


# ---------- 休息窗口 状态 ----------
def mark_sleep(db, now_ts: Optional[int] = None, after_hours: int = 7) -> int:
    """用户说晚安：记录休息窗口。返回 rest_until 时间戳。"""
    now_ts = int(now_ts or time.time())
    rest_until = now_ts + max(1, int(after_hours)) * 3600
    db.kv_set("rest_until", rest_until)
    db.kv_set("rest_since", now_ts)
    return rest_until


def break_rest(db) -> None:
    """用户主动发消息：打破休息窗口。"""
    db.kv_set("rest_until", 0)
    db.kv_set("rest_since", 0)


def rest_until_ts(db) -> int:
    """当前休息窗口截止时间戳；无窗口返回 0。"""
    try:
        return int(db.kv_get("rest_until", 0) or 0)
    except Exception:
        return 0


def rest_since_ts(db) -> int:
    try:
        return int(db.kv_get("rest_since", 0) or 0)
    except Exception:
        return 0


def in_rest_window(db, now_ts: Optional[int] = None) -> bool:
    """是否处于休息窗口内（说晚安后、恢复时间前）。"""
    until = rest_until_ts(db)
    if until <= 0:
        return False
    now_ts = int(now_ts or time.time())
    return now_ts < until


# ---------- 统一时间判定 ----------
def in_dnd_time_only(config: dict, now_ts: Optional[int] = None) -> bool:
    """纯勿扰时段判断（不包含休息窗口）。与旧实现语义一致。"""
    dnd_start = str(config.get("dnd_start", "23:00"))
    dnd_end = str(config.get("dnd_end", "08:00"))
    try:
        sh, sm = map(int, dnd_start.split(":"))
        eh, em = map(int, dnd_end.split(":"))
    except Exception:
        return False
    now = time.localtime(now_ts) if now_ts else time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    s = sh * 60 + sm
    e = eh * 60 + em
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


def in_quiet(db, config: dict, now_ts: Optional[int] = None) -> bool:
    """统一「安静期」判定 = 勿扰时段 OR 休息窗口。monitor/decision/executor 共用。"""
    if in_rest_window(db, now_ts=now_ts):
        return True
    return in_dnd_time_only(config, now_ts=now_ts)
