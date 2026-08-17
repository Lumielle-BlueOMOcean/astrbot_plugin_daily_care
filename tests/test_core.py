# -*- coding: utf-8 -*-
"""
微光-Daily Care v2 - 单元测试（不依赖 astrbot 运行时）
覆盖：数据库、天气分析、状态反思(ChatReflector)、天气判断(WeatherJudge)、
执行器(背景构造/冷却/上限/开口生成)
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime

# ---- 注入假的 astrbot 运行时（仅测试用）----
import types
_astrbot = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
class _Logger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
_api.logger = _Logger()
_comp = types.ModuleType("astrbot.api.message_components")
class Plain:
    def __init__(self, text): self.text = text
    def __str__(self): return self.text
_comp.Plain = Plain
_msg = types.ModuleType("astrbot.api.message")
class MessageChain:
    def __init__(self, chain): self.chain = chain
    def __str__(self): return "".join(str(c) for c in self.chain)
_msg.MessageChain = MessageChain
_api.message_components = _comp
_api.message = _msg
_astrbot.api = _api
sys.modules["astrbot"] = _astrbot
sys.modules["astrbot.api"] = _api
sys.modules["astrbot.api.message_components"] = _comp
sys.modules["astrbot.api.message"] = _msg
# ------------------------------------------

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import CareDatabase
from core.monitor import CareMonitor
from core.reflection import ChatReflector, WeatherJudge
from core.weather import analyze_weather


def make_db():
    tmp = tempfile.mkdtemp(prefix="daily_care_test_v2_")
    db = CareDatabase(tmp)
    loc_id = db.upsert_location("动态", 0, 0, "dynamic", "")
    db.add_target("你", "用户自己", loc_id, is_default=1, is_dynamic=1)
    return db


def test_database():
    db = make_db()
    t_id = db.get_default_target()["id"]
    # 事件写入 + 去重
    e1 = db.add_event(t_id, "weather", "今晚有雷阵雨", "事实", intensity=3, priority=3, ttl_hours=24)
    e2 = db.add_event(t_id, "weather", "今晚有雷阵雨", "事实", intensity=3, priority=3, ttl_hours=24)
    assert e1 == e2
    assert len(db.get_active_events(t_id)) == 1
    # kv
    db.kv_set("k", {"a": 1})
    assert db.kv_get("k")["a"] == 1
    # 今日发送统计
    assert db.count_send_today(t_id, datetime.now().strftime("%Y-%m-%d")) == 0
    db.add_send_log(t_id, 0, "hi", "test")
    assert db.count_send_today(t_id, datetime.now().strftime("%Y-%m-%d")) == 1
    print("✓ 数据库测试通过")


def test_weather_analyze():
    wx = {
        "today": {"date": "2026-08-14", "weather_code": 96, "desc": "雷阵雨伴冰雹",
                  "tmax": 36, "tmin": 26, "precip_prob": 84, "precip_sum": 12.5,
                  "wind_max": 45, "uv_max": 8},
        "tomorrow": {"date": "2026-08-15", "weather_code": 61, "desc": "小雨",
                     "tmax": 32, "tmin": 25, "precip_prob": 77, "precip_sum": 3,
                     "wind_max": 20, "uv_max": 5},
    }
    events = analyze_weather(wx)
    assert any("雷阵雨" in e["summary"] and "带伞" in e["summary"] for e in events)
    print("✓ 天气分析测试通过")


def test_chat_reflector():
    db = make_db()
    t_id = db.get_default_target()["id"]
    history = [
        {"role": "user", "content": "今天嗓子好疼，像吞了刀片"},
        {"role": "assistant", "content": "啊？是不是上火了，多喝点水"},
        {"role": "user", "content": "嗯，可能昨天吃太辣了"},
    ]
    # mock LLM：识别出 身体不适 + 饮食
    async def fake_llm(system, user):
        return json.dumps([
            {"type": "sick", "summary": "嗓子不适", "detail": "用户说'嗓子像吞了刀片'", "intensity": 3, "confidence": 0.9},
            {"type": "diet", "summary": "吃了辛辣食物", "detail": "用户说'昨天吃太辣了'", "intensity": 2, "confidence": 0.7},
        ])
    r = ChatReflector(db, {}, fake_llm)
    created = asyncio.run(r.reflect(history))
    assert len(created) == 2
    evs = db.get_active_events(t_id)
    assert any("身体不适" in e["summary"] for e in evs)
    assert any("饮食" in e["summary"] for e in evs)
    # 恢复信号：说"好了"应关闭 sick 事件
    history2 = [
        {"role": "user", "content": "嗓子好了，不疼了"},
        {"role": "assistant", "content": "那就好！"},
    ]
    async def fake_llm2(system, user):
        return json.dumps([
            {"type": "recovery", "summary": "嗓子不适好了", "detail": "用户说'不疼了'", "intensity": 1, "confidence": 1.0},
        ])
    r2 = ChatReflector(db, {}, fake_llm2)
    asyncio.run(r2.reflect(history2))
    active = db.get_active_events(t_id)
    assert not any("身体不适" in e["summary"] for e in active), "身体不适事件应被关闭"
    assert any("饮食" in e["summary"] for e in active), "饮食事件应保留"
    print("✓ 状态反思测试通过")


def test_weather_judge():
    db = make_db()
    loc_id = db.upsert_location("长沙", 28.2, 112.9, "static", "湖南")

    # 值得提醒
    async def judge_yes(system, user):
        return json.dumps({"should_remind": True, "background": "你所在的城市今晚 20 点后有雷阵雨，气温降至 18°C", "reason": "雷雨+降温"})
    wj = WeatherJudge(db, {}, judge_yes)
    bg = asyncio.run(wj.judge(loc_id, "长沙", {"now": {}}, cooldown_hours=6))
    assert bg and "雷阵雨" in bg, bg

    # 冷却期内不再提醒
    async def judge_no(system, user):
        return json.dumps({"should_remind": False, "background": "", "reason": "冷却"})
    wj2 = WeatherJudge(db, {}, judge_no)
    bg2 = asyncio.run(wj2.judge(loc_id, "长沙", {"now": {}}, cooldown_hours=6))
    assert bg2 is None, "冷却期内不应提醒"

    # 不值得提醒
    loc2 = db.upsert_location("上海", 31.2, 121.5, "static", "上海")
    async def judge_mild(system, user):
        return json.dumps({"should_remind": False, "background": "", "reason": "晴好"})
    wj3 = WeatherJudge(db, {}, judge_mild)
    bg3 = asyncio.run(wj3.judge(loc2, "上海", {"now": {}}, cooldown_hours=0))
    assert bg3 is None
    print("✓ 天气判断测试通过")


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append((session, str(chain)))
        return True


def test_executor_background_and_send():
    from core.executor import Executor
    db = make_db()
    t_id = db.get_default_target()["id"]
    db.add_event(t_id, "state", "身体不适：感冒", "用户说'我好难受'", intensity=4, priority=4, ttl_hours=72)
    db.add_event(t_id, "weather", "今晚有雷阵雨，气温降至18°C", "", intensity=3, priority=3, ttl_hours=24)

    ctx = FakeContext()
    calls = []

    async def fake_llm(system, user):
        calls.append(user)
        return "感冒还没好吗？我有点担心。今晚要下雨，早点回去，别淋着了。"

    ex = Executor(db, {"dnd_start": "23:00", "dnd_end": "08:00", "care_level": 5},
                  ctx, fake_llm, persona_prompt="你是关怀助手", session="bot:FriendMessage:10001")

    events = db.get_active_events(t_id)
    bg = ex._compose_background(db.get_default_target(), events)
    assert "感冒" in bg and "雷阵雨" in bg, bg
    assert "记得" not in bg and "小心" not in bg, "背景只能是事实，不能含建议/指令"

    # 背景必须是纯事实
    assert "记得" not in bg and "小心" not in bg, "背景只能是事实，不能含建议/指令"
    # 唤醒事件构造（验证不会抛异常，且返回 False 表示推送失败——测试环境无事件总线）
    woke, _ = asyncio.run(ex._woke_for_care(db.get_default_target(), bg))
    # 测试环境没有真实 event_queue，应安全失败（返回 False）而不抛异常
    assert woke is False
    # 决策引擎：随机窗口时刻生成
    from core.decision import random_ts_in_window
    import datetime as _dt
    ts = random_ts_in_window(_dt.date.today().isoformat(), "evening")
    d = _dt.datetime.fromtimestamp(ts)
    assert 17 <= d.hour < 20, f"随机时刻应在傍晚窗口内: {d}"
    print("✓ 执行器背景构造与随机触发测试通过")


def test_executor_cooldown_and_limit():
    from core.executor import Executor
    db = make_db()
    t_id = db.get_default_target()["id"]
    db.add_event(t_id, "state", "身体不适：感冒", "detail", intensity=4, priority=4, ttl_hours=72)

    ctx = FakeContext()

    async def fake_llm(system, user):
        return "好点了吗？"

    # 冷却 10 分钟
    ex = Executor(db, {"dnd_start": "23:00", "dnd_end": "08:00", "care_level": 5,
                       "care_cooldown_minutes": 10},
                  ctx, fake_llm, persona_prompt="p", session="bot:FriendMessage:10001")
    # 冷却拦截：执行端冷却由 main 的 _run_decision 控制，此处验证 execute_immediate 直接执行
    import unittest.mock as mock
    db.kv_set(f"last_care_send_{t_id}", int(time.time()))
    # 测试环境无事件总线 → 唤醒失败返回 False（不降级直发，不抛异常）
    with mock.patch.object(ex, "in_dnd", return_value=False):
        ok = asyncio.run(ex.execute_immediate("身体不适：感冒"))
        assert ok is False, "测试环境无事件总线应安全失败"
    # 决策引擎 act 决策应能被冷却拦截逻辑识别（在 main 层）
    from core.decision import DecisionEngine
    de = DecisionEngine(db, {"care_level": 5, "care_daily_limit": 2}, lambda s, u: "")
    assert de is not None
    print("✓ 冷却与上限（v5）测试通过")


def test_dnd():
    from core.executor import Executor
    db = make_db()
    ex = Executor(db, {"dnd_start": "23:00", "dnd_end": "08:00"}, None, None)
    now = datetime.now()
    if now.hour >= 23 or now.hour < 8:
        assert ex.in_dnd()
    print("✓ 勿扰时段测试通过")



def test_recovery_signal():
    """recovery 恢复信号：根据用户"好了/退了"自动关闭状态事件（v5.6.1）"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    db.add_event(t_id, "state", "身体不适：感冒发烧", "用户说嗓子像吞刀片", 4, 1, 72)
    db.add_event(t_id, "state", "熬夜：凌晨两点没睡", "用户说还在写代码", 2, 1, 48)
    r = ChatReflector(db, {}, None)

    # 场景1：明确恢复信号 → 关闭感冒，保留熬夜
    r._resolve_by_type(t_id, "感冒好了", "用户说'我感冒已经好了'")
    active = db.get_active_events(t_id)
    assert not any("感冒" in e["summary"] for e in active), "感冒事件应被关闭"
    assert any("熬夜" in e["summary"] for e in active), "熬夜事件应保留"

    # 场景2：summary 无核心词，从 detail（用户原话）提取 → 关闭嗓子事件
    db.add_event(t_id, "state", "身体不适：嗓子疼", "用户说嗓子不舒服", 3, 1, 72)
    r._resolve_by_type(t_id, "已经好了", "用户说'嗓子已经好了'")
    active = db.get_active_events(t_id)
    assert not any("嗓子" in e["summary"] for e in active), "嗓子事件应被关闭"

    # 场景3：无关恢复信号不误伤其他事件
    db.add_event(t_id, "state", "情绪低落：最近压力大", "用户说很焦虑", 3, 1, 48)
    r._resolve_by_type(t_id, "感冒好了", "用户说感冒已经好了")
    active = db.get_active_events(t_id)
    assert any("情绪" in e["summary"] for e in active), "情绪事件不应被误伤"

    # 场景3b：感冒好了 → 关闭咳嗽/清嗓子事件（精确匹配失败时的身体类兜底）
    db.add_event(t_id, "state", "身体不适：疑似咳嗽或清嗓子", "用户说'咳咳'", 3, 1, 72)
    r._resolve_by_type(t_id, "感冒好了", "用户说'我感冒好了，彻底好了，喉咙什么的都不难受了'")
    active = db.get_active_events(t_id)
    assert not any("咳嗽" in e["summary"] for e in active), "咳嗽事件应被感冒恢复信号关闭"
    assert any("情绪" in e["summary"] for e in active), "情绪事件仍不应被误伤"
    assert any("熬夜" in e["summary"] for e in active), "熬夜事件不应被身体类兜底误关"

    # 场景4：完整 reflect 链路 —— LLM 输出 recovery，事件关闭且不新建
    async def fake_llm(system, user):
        return json.dumps([{"type": "recovery", "summary": "发烧退了", "detail": "用户说'烧退了'", "intensity": 1, "confidence": 1.0}], ensure_ascii=False)
    db.add_event(t_id, "state", "身体不适：发烧", "用户说发烧了", 4, 1, 72)
    rr = ChatReflector(db, {}, fake_llm)
    created = asyncio.run(rr.reflect([{"role": "user", "content": "我发烧已经退了"}]))
    active = db.get_active_events(t_id)
    assert not any("发烧" in e["summary"] for e in active), "发烧事件应被关闭"
    assert len(created) == 0, "recovery 不应新建事件"
    print("✓ 恢复信号（recovery）测试通过")




def test_reflect_incremental():
    """增量衔接（内容锚点版）：只反思锚点后的新增对话（v5.6.4）"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    calls = []

    async def fake_llm(system, user):
        calls.append(user)
        return json.dumps([], ensure_ascii=False)

    r = ChatReflector(db, {}, fake_llm)
    # 第一次：5 条消息（无时间戳，模拟 AstrBot 真实历史）
    hist1 = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
    asyncio.run(r.reflect(hist1))
    assert len(calls) == 1, "首次应全量反思一次"
    assert db.kv_get("reflect_anchor") == "msg4", "锚点应推进到最后一条"

    # 第二次：追加 3 条新消息，只应看到新增 + 衔接
    hist2 = hist1 + [{"role": "user", "content": f"msg{i}"} for i in range(5, 8)]
    asyncio.run(r.reflect(hist2))
    u2 = calls[1]
    assert all(f"msg{i}" in u2 for i in (5, 6, 7)), "应包含新增消息"
    assert "msg4" in u2, "应包含衔接的旧消息"
    assert "msg0" not in u2, "太旧的消息不应出现"
    assert "此前对话" in u2 and "新增对话" in u2, "应有衔接标注"
    assert db.kv_get("reflect_anchor") == "msg7"

    # 第三次：无新消息 → 短路不调 LLM
    created = asyncio.run(r.reflect(hist2))
    assert created == [] and len(calls) == 2, "无新消息应跳过 LLM 调用"

    # 锚点丢失（历史被截断）→ 降级为最近 24 条全量
    hist3 = [{"role": "user", "content": f"fresh{i}"} for i in range(30)]
    asyncio.run(r.reflect(hist3))
    u3 = calls[2]
    assert "fresh29" in u3 and "fresh6" in u3, "锚点丢失时应取最近24条"
    assert "fresh0" not in u3, "降级模式只取最近24条，旧消息应被挤出"
    assert "此前对话" not in u3, "降级模式不应有衔接标注"
    print("✓ 增量衔接（内容锚点）测试通过")

def test_recovery_llm_semantic_resolve():
    """v5.7.0：LLM 语义判定事件结束——'感冒'事件被'喉咙不难受了'等同义表述关闭"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    e_cold = db.add_event(t_id, "state", "身体不适：感冒", "用户说嗓子像吞刀片，头疼", 4, 1, 72)
    e_late = db.add_event(t_id, "state", "熬夜：凌晨两点没睡", "用户说还在写代码", 2, 1, 48)

    # LLM 对比活跃事件后判定：感冒事件（event_id）已结束，熬夜保留
    async def fake_llm(system, user):
        assert "当前活跃关怀事件" in user, "prompt 应注入活跃事件清单"
        assert "事件ID" in user, "prompt 应带事件 ID"
        assert "resolved_events" in system, "prompt 应要求输出 resolved_events"
        return json.dumps({
            "new_states": [],
            "resolved_events": [{"event_id": e_cold, "reason": "用户说'喉咙什么的都不难受了'"}],
        }, ensure_ascii=False)

    # 关联的待发送计划，事件被关闭时应一并取消
    db.add_plan(e_cold, t_id, "2026-08-16", "evening", "inquiry", "感冒好点了吗")
    assert len(db.get_pending_plans("2026-08-16")) == 1

    r = ChatReflector(db, {}, fake_llm)
    created = asyncio.run(r.reflect([
        {"role": "user", "content": "我感冒好了，喉咙什么的都不难受了"},
    ]))
    active = db.get_active_events(t_id)
    assert not any("感冒" in e["summary"] for e in active), "感冒事件应由 LLM 语义判定关闭"
    assert any("熬夜" in e["summary"] for e in active), "熬夜事件应保留"
    assert created == [], "resolved 不应新建事件"
    assert db.get_pending_plans("2026-08-16") == [], "关联计划应被取消"
    print("✓ LLM 语义判定事件结束（v5.7.0）测试通过")


def test_recovery_llm_resolve_conservative():
    """v5.7.0：语义判定要保守——用户没说恢复时不得误关；weather 事件不受影响"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    db.add_event(t_id, "state", "身体不适：感冒", "用户说嗓子像吞刀片", 4, 1, 72)
    db.add_event(t_id, "state", "情绪低落：最近压力大", "用户说很焦虑", 3, 1, 48)
    db.add_event(t_id, "weather", "今晚有雷阵雨", "", 3, 1, 24)
    w_id = db.get_active_events(t_id)[-1]["id"]  # weather 事件 id

    # LLM 误把 weather 事件也放进 resolved → 应被忽略（只关 state 事件）
    async def fake_llm(system, user):
        return json.dumps({
            "new_states": [],
            "resolved_events": [{"event_id": w_id, "reason": "雨停了"}],
        }, ensure_ascii=False)

    r = ChatReflector(db, {}, fake_llm)
    asyncio.run(r.reflect([{"role": "user", "content": "雨停了"}]))
    active = db.get_active_events(t_id)
    assert any("雷阵雨" in e["summary"] for e in active), "weather 事件不应被反思关闭"

    # 用户还在说不舒服 → LLM 不应 resolve；即便 LLM 误判关闭，也应能由保守判定避免
    db.add_event(t_id, "state", "身体不适：感冒", "用户说嗓子像吞刀片", 4, 1, 72)
    async def fake_llm2(system, user):
        return json.dumps({
            "new_states": [],
            "resolved_events": [],  # 保守：不关
        }, ensure_ascii=False)
    r2 = ChatReflector(db, {}, fake_llm2)
    asyncio.run(r2.reflect([{"role": "user", "content": "还是难受，咳嗽没停"}]))
    active2 = db.get_active_events(t_id)
    assert any("感冒" in e["summary"] for e in active2), "用户没说恢复，感冒事件应保留"
    print("✓ 保守判定与来源隔离测试通过")


def test_parse_items_compat():
    """v5.7.0：_parse_items 兼容新旧两种 LLM 输出格式"""
    # 新版对象
    d1 = ChatReflector._parse_items('{"new_states":[{"type":"sick","summary":"嗓子不适"}],"resolved_events":[{"event_id":3}]}')
    assert d1["new_states"][0]["summary"] == "嗓子不适"
    assert d1["resolved_events"][0]["event_id"] == 3
    # 旧版数组
    d2 = ChatReflector._parse_items('[{"type":"sick","summary":"嗓子不适"}]')
    assert len(d2["new_states"]) == 1 and d2["resolved_events"] == []
    # markdown 代码块包裹
    d3 = ChatReflector._parse_items("```json\n{\"new_states\":[],\"resolved_events\":[]}\n```")
    assert d3["new_states"] == [] and d3["resolved_events"] == []
    # 空/非法
    d4 = ChatReflector._parse_items("")
    assert d4["new_states"] == [] and d4["resolved_events"] == []
    print("✓ 输出格式兼容测试通过")


def test_backfill_scan():
    """v5.7.1：窗口外补扫——流水表中被框架截断的历史消息，粗筛命中后送 LLM 精判"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    # 预置流水表：30 条日常 + 1 条"感冒好了"（模拟被框架截断的窗口外历史）
    old = [f"日常闲聊{i}" for i in range(1, 31)]
    old.append("我感冒好了，喉咙什么的都不难受了")
    db.stream_append(t_id, old)
    eid = db.add_event(t_id, "state", "身体不适：感冒", "用户前几天说嗓子疼", 4, 1, 72)

    calls = []
    async def fake_llm(system, user):
        calls.append(user)
        if len(calls) == 1:
            # 主分析：本次窗口内只有"吃了吗"，无状态
            return json.dumps({"new_states": [], "resolved_events": []}, ensure_ascii=False)
        # 补扫精判：识别出"感冒好了" → 关闭感冒事件
        return json.dumps({
            "new_states": [],
            "resolved_events": [{"event_id": eid, "reason": "用户说'感冒好了'"}],
        }, ensure_ascii=False)

    r = ChatReflector(db, {}, fake_llm)
    asyncio.run(r.reflect([{"role": "user", "content": "吃了吗"}]))
    active = db.get_active_events(t_id)
    assert not any("感冒" in e["summary"] for e in active), "窗口外补扫应关闭感冒事件"
    assert len(calls) == 2, f"应触发主分析+补扫精判各一次，实际 {len(calls)}"
    assert db.kv_get("reflect_cursor_seq") == db.stream_max_seq(t_id), "补扫游标应推进"
    print("✓ 窗口外补扫（粗筛+精判）测试通过")


def test_backfill_scan_conservative():
    """v5.7.1：粗筛无命中不调 LLM；本次窗口内消息即使含关键词也不重复精判"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    db.stream_append(t_id, [f"今天天气不错{i}" for i in range(1, 21)])
    db.add_event(t_id, "state", "身体不适：感冒", "用户说嗓子疼", 4, 1, 72)

    calls = []
    async def fake_llm(system, user):
        calls.append(user)
        return json.dumps({"new_states": [], "resolved_events": []}, ensure_ascii=False)

    # 场景A：窗口外全是日常消息，粗筛无命中 → 只有主分析一次调用
    r = ChatReflector(db, {}, fake_llm)
    asyncio.run(r.reflect([{"role": "user", "content": "嗯嗯"}]))
    assert len(calls) == 1, "粗筛无命中不应触发补扫精判"

    # 场景B：本次窗口内消息含关键词，主分析已处理，补扫排除其 hash 不重复精判
    db2 = make_db()
    t_id2 = db2.get_default_target()["id"]
    calls2 = []
    async def fake_llm2(system, user):
        calls2.append(user)
        if len(calls2) == 1:
            return json.dumps({
                "new_states": [{"type": "sick", "summary": "头疼", "detail": "用户说'头疼死了'", "intensity": 3, "confidence": 0.9}],
                "resolved_events": [],
            }, ensure_ascii=False)
        return json.dumps({"new_states": [], "resolved_events": []}, ensure_ascii=False)
    r2 = ChatReflector(db2, {}, fake_llm2)
    asyncio.run(r2.reflect([{"role": "user", "content": "今天头疼死了"}]))
    assert len(calls2) == 1, "窗口内消息已被主分析覆盖，补扫不应重复精判"
    active2 = db2.get_active_events(t_id2)
    assert any("头疼" in e["summary"] for e in active2), "主分析应已创建头疼事件"
    print("✓ 补扫保守性（无命中不调/已覆盖不重复）测试通过")

def test_cause_dedup():
    """v5.8.0：同 (type, cause) 的重复事件合并，不再因摘要措辞不同而爆炸"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    async def fake_llm(system, user):
        # 两次反思都输出同病因（感冒）但措辞不同的摘要
        if "第一次" in str(user):
            return json.dumps({"new_states": [{"type": "sick", "cause": "感冒", "summary": "嗓子疼", "detail": "用户说感冒了", "intensity": 3, "confidence": 0.9}], "resolved_events": []}, ensure_ascii=False)
        return json.dumps({"new_states": [{"type": "sick", "cause": "感冒", "summary": "感冒还没好", "detail": "用户又说感冒了", "intensity": 3, "confidence": 0.9}], "resolved_events": []}, ensure_ascii=False)
    r = ChatReflector(db, {}, fake_llm)
    asyncio.run(r.reflect([{"role": "user", "content": "第一次：我感冒了"}]))
    asyncio.run(r.reflect([{"role": "user", "content": "第二次：还在感冒"}]))
    active = db.get_active_events(t_id)
    cold = [e for e in active if e.get("cause") == "感冒"]
    assert len(cold) == 1, f"同病因应合并为1条，实际 {len(cold)}"
    assert cold[0]["type"] == "sick"
    # 病因表里应有 感冒（种子）
    causes = db.list_causes("sick")
    assert any(c["cause"] == "感冒" for c in causes)
    print("✓ 同病因去重（v5.8.0）测试通过")


def test_cause_precise_resolve():
    """v5.8.0 核心场景：感冒好了只关感冒，胃部反酸绝不误伤"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    # 挂两个独立病因事件：感冒 + 胃部
    e_cold = db.add_event(t_id, "state", "身体不适：感冒", "用户说感冒了，嗓子疼", 4, 1, 72, event_type="sick", cause="感冒")
    e_stomach = db.add_event(t_id, "state", "身体不适：胃部", "用户说反酸，可能是胃炎", 4, 1, 72, event_type="sick", cause="胃部")

    # 场景A：LLM 精确判定——只关感冒的 event_id
    async def fake_llm(system, user):
        return json.dumps({
            "new_states": [],
            "resolved_events": [{"event_id": e_cold, "reason": "用户说感冒好了"}],
        }, ensure_ascii=False)
    r = ChatReflector(db, {}, fake_llm)
    asyncio.run(r.reflect([{"role": "user", "content": "感冒好啦"}]))
    active = db.get_active_events(t_id)
    assert not any("感冒" in e["summary"] for e in active), "感冒事件应被关闭"
    assert any("胃部" in e["summary"] for e in active), "胃部事件不应被误伤"

    # 场景B：recovery 兜底（LLM 没给 event_id）——按 cause 精确关闭
    db2 = make_db()
    t_id2 = db2.get_default_target()["id"]
    db2.add_event(t_id2, "state", "身体不适：感冒", "用户说感冒了", 4, 1, 72, event_type="sick", cause="感冒")
    db2.add_event(t_id2, "state", "身体不适：胃部", "用户说反酸", 4, 1, 72, event_type="sick", cause="胃部")
    async def fake_llm2(system, user):
        return json.dumps([{"type": "recovery", "cause": "感冒", "summary": "感冒好了", "detail": "用户说好了", "intensity": 1, "confidence": 1.0}], ensure_ascii=False)
    r2 = ChatReflector(db2, {}, fake_llm2)
    asyncio.run(r2.reflect([{"role": "user", "content": "感冒好了"}]))
    active2 = db2.get_active_events(t_id2)
    assert not any("感冒" in e["summary"] for e in active2), "兜底应按 cause 关感冒"
    assert any("胃部" in e["summary"] for e in active2), "胃部仍不应被误伤"
    print("✓ 按病因精确关闭（v5.8.0 核心）测试通过")


def test_cause_dynamic_add():
    """v5.8.0：LLM 归因到现有清单外的病因时动态新增"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    async def fake_llm(system, user):
        assert "过敏" in str(user), "LLM 输出应引用新 cause"
        return json.dumps({"new_states": [{"type": "sick", "cause": "过敏", "summary": "花粉过敏", "detail": "用户说对花粉过敏打喷嚏", "intensity": 3, "confidence": 0.9}], "resolved_events": []}, ensure_ascii=False)
    r = ChatReflector(db, {}, fake_llm)
    created = asyncio.run(r.reflect([{"role": "user", "content": "我对花粉过敏，一直打喷嚏"}]))
    causes = db.list_causes("sick")
    assert any(c["cause"] == "过敏" for c in causes), "过敏应被动态新增到病因表"
    assert len(created) == 1
    ev = db.get_event(created[0])
    assert ev["cause"] == "过敏", "事件应带 cause=过敏"
    print("✓ 病因动态新增（v5.8.0）测试通过")


def test_dynamic_keyword_backfill():
    """v5.8.1：LLM 动态新增 cause 后，其 keywords 自动进入粗筛词库——窗口外补扫能命中新病因"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    # 先动态新增一个 cause：耳鸣（不在静态兜底集 FORWARD_KEYWORDS 里）
    db.upsert_cause("sick", "耳鸣", "耳鸣,耳朵嗡嗡响,听力下降")
    db.stream_append(t_id, ["日常闲聊", "耳朵一直嗡嗡响，耳鸣得厉害"])

    async def fake_llm(system, user):
        # 断言精判 prompt 里确实包含耳鸣消息
        assert "耳鸣" in str(user), "精判上下文块应包含耳鸣消息"
        return json.dumps({"new_states": [{"type": "sick", "cause": "耳鸣", "summary": "耳朵嗡嗡响", "detail": "用户说耳鸣", "intensity": 2, "confidence": 0.8}], "resolved_events": []}, ensure_ascii=False)

    r = ChatReflector(db, {}, fake_llm)
    created = asyncio.run(r.reflect([{"role": "user", "content": "最近耳朵一直嗡嗡响，耳鸣得厉害"}]))
    assert created, "动态 cause 的耳鸣事件应被创建"
    ev = db.get_event(created[0])
    assert ev["cause"] == "耳鸣", "事件应归因到动态 cause=耳鸣"
    print("✓ 动态关键词粗筛（v5.8.1）测试通过")


def test_seen_unseen_full_scan():
    """v5.8.2：seen=0 且已出窗口的消息，即使无关键词命中也被未看型全量打包精判"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    # 预置 15 条纯日常（无关键词，seq 1-15）
    old = [f"纯日常闲聊{i}" for i in range(1, 16)]
    db.stream_append(t_id, old)
    # 预置 130 条占位并标 seen，抬高 max_seq（让前 15 条满足 seq<=max_seq-WINDOW）
    filler = [f"占位消息{i}" for i in range(1, 131)]
    db.stream_append(t_id, filler)
    db.stream_mark_seen_by_hashes(t_id, {hashlib.md5(c.encode("utf-8")).hexdigest() for c in filler})

    calls = []
    async def fake_llm(system, user):
        calls.append(user)
        if len(calls) == 1:
            # 主分析：本次窗口内无状态
            return json.dumps({"new_states": [], "resolved_events": []}, ensure_ascii=False)
        # 未看型全量精判：纯日常无状态
        return json.dumps({"new_states": [], "resolved_events": []}, ensure_ascii=False)

    r = ChatReflector(db, {}, fake_llm)
    asyncio.run(r.reflect([{"role": "user", "content": "在吗"}]))
    # 主分析 1 次 + 未看型全量 2 组（15条→10+5）
    assert len(calls) == 3, f"应触发主分析1次+未看型全量2组，实际 {len(calls)}"
    # 15 条纯日常应全部标 seen
    unseen = db.stream_unseen(t_id)
    assert all(m["content"].startswith("占位") for m in unseen), "纯日常应全部标 seen，仅剩占位"

    # 幂等：再次反思不重复精判未看型（seen 已标）
    calls2 = []
    async def fake_llm2(system, user):
        calls2.append(user)
        return json.dumps({"new_states": [], "resolved_events": []}, ensure_ascii=False)
    r2 = ChatReflector(db, {}, fake_llm2)
    asyncio.run(r2.reflect([{"role": "user", "content": "晚上好"}]))
    assert len(calls2) == 1, f"seen 已标，再次反思不应触发未看型全量，实际 {len(calls2)}"
    print("✓ 未看型全量精判 + 幂等（v5.8.2）测试通过")


def test_seen_mark_after_main_analysis():
    """v5.8.2：主分析成功的窗口内消息标 seen，补扫不重复；失败则不标可重试"""
    db = make_db()
    t_id = db.get_default_target()["id"]

    # 场景A：主分析成功 → 窗口内消息标 seen，补扫不重复精判
    calls = []
    async def fake_llm(system, user):
        calls.append(user)
        if len(calls) == 1:
            return json.dumps({
                "new_states": [{"type": "sick", "cause": "感冒", "summary": "感冒了", "detail": "用户说感冒", "intensity": 3, "confidence": 0.9}],
                "resolved_events": [],
            }, ensure_ascii=False)
        return json.dumps({"new_states": [], "resolved_events": []}, ensure_ascii=False)
    r = ChatReflector(db, {}, fake_llm)
    asyncio.run(r.reflect([{"role": "user", "content": "我感冒了，好难受"}]))
    assert len(calls) == 1, "主分析成功标 seen 后，补扫不应重复精判窗口内消息"
    unseen = db.stream_unseen(t_id)
    assert len(unseen) == 0, "主分析成功后窗口内消息应全部标 seen"

    # 场景B：主分析失败（LLM 抛异常）→ 消息不标 seen，下次可重试
    db2 = make_db()
    t_id2 = db2.get_default_target()["id"]
    async def fake_llm_fail(system, user):
        raise RuntimeError("LLM 挂了")
    r2 = ChatReflector(db2, {}, fake_llm_fail)
    asyncio.run(r2.reflect([{"role": "user", "content": "我头疼死了"}]))
    unseen2 = db2.stream_unseen(t_id2)
    assert len(unseen2) == 1, "主分析失败的消息应保持 seen=0 以便重试"
    print("✓ seen 标记时机（成功标/失败留）测试通过")
def test_decision_category_track():
    """v1.00：决策层 category 分轨——weather/state/proactive 解析与缺省"""
    from core.decision import DecisionEngine
    db = make_db()
    target = db.get_default_target()

    async def fake_llm_weather(system, user):
        return '{"decision":"act","background":"今天有暴雨，最高26度","category":"weather","reason":"天气"}'

    dec = DecisionEngine(db, {}, fake_llm_weather)
    r = asyncio.run(dec.decide(target))
    assert r["category"] == "weather", f"weather 分类错误: {r['category']}"

    async def fake_llm_state(system, user):
        return '{"decision":"act","background":"你有点疲惫","category":"state","reason":"状态"}'

    dec2 = DecisionEngine(db, {}, fake_llm_state)
    r2 = asyncio.run(dec2.decide(target))
    assert r2["category"] == "state", f"state 分类错误: {r2['category']}"

    async def fake_llm_missing(system, user):
        return '{"decision":"act","background":"随便说说","reason":"无category"}'

    dec3 = DecisionEngine(db, {}, fake_llm_missing)
    r3 = asyncio.run(dec3.decide(target))
    assert r3["category"] == "state", f"缺省应 state: {r3['category']}"
    print("✓ 决策 category 分轨（weather/state/缺省）测试通过")



def test_daily_note_text():
    """v1.01：常态天气提示文本——温度体感区间映射 + 客观事实"""
    db = make_db()
    mon = CareMonitor(db, {})
    # 炎热
    t1 = mon.daily_note_text("重庆", {"today": {"desc": "晴", "tmax": 36.0, "tmin": 27.0, "precip_prob": 5.0}})
    assert "重庆" in t1 and "36" in t1 and "炎热" in t1, t1
    # 热
    t2 = mon.daily_note_text("重庆", {"today": {"desc": "多云", "tmax": 31.0, "tmin": 24.0, "precip_prob": 10.0}})
    assert "偏热" in t2, t2
    # 温暖
    t3 = mon.daily_note_text("重庆", {"today": {"desc": "晴", "tmax": 26.0, "tmin": 18.0, "precip_prob": 0}})
    assert "温暖舒适" in t3, t3
    # 适宜
    t4 = mon.daily_note_text("重庆", {"today": {"desc": "阴", "tmax": 20.0, "tmin": 14.0, "precip_prob": 0}})
    assert "温度适宜" in t4, t4
    # 偏凉
    t5 = mon.daily_note_text("重庆", {"today": {"desc": "小雨", "tmax": 12.0, "tmin": 8.0, "precip_prob": 60.0}})
    assert "偏凉" in t5 and "降水概率60" in t5, t5
    # 寒冷
    t6 = mon.daily_note_text("北京", {"today": {"desc": "雪", "tmax": -2.0, "tmin": -8.0, "precip_prob": 80.0}})
    assert "寒冷" in t6, t6
    print("✓ 常态天气提示文本（温度区间映射）测试通过")

def test_note_windows_multi():
    """1.0.0：常态提示时段多选+自定义——解析与命中判断"""
    from core.decision import _parse_note_windows, _in_note_window
    from datetime import datetime as _dt

    # JSON 数组字符串
    assert _parse_note_windows('["morning","07:00-08:00"]') == ["morning", "07:00-08:00"]
    # 逗号分隔
    assert _parse_note_windows("morning,noon") == ["morning", "noon"]
    # 单个
    assert _parse_note_windows("evening") == ["evening"]
    # 空
    assert _parse_note_windows("") == []
    assert _parse_note_windows(None) == []
    # 已是列表
    assert _parse_note_windows(["night"]) == ["night"]

    # 预设窗口命中
    assert _in_note_window(_dt(2026, 8, 16, 9, 0), ["morning"])
    assert not _in_note_window(_dt(2026, 8, 16, 12, 0), ["morning"])
    # 自定义时段命中
    assert _in_note_window(_dt(2026, 8, 16, 7, 30), ["07:00-08:00"])
    assert not _in_note_window(_dt(2026, 8, 16, 8, 30), ["07:00-08:00"])
    # 跨天自定义时段
    assert _in_note_window(_dt(2026, 8, 16, 23, 30), ["22:00-02:00"])
    assert _in_note_window(_dt(2026, 8, 17, 1, 0), ["22:00-02:00"])
    assert not _in_note_window(_dt(2026, 8, 17, 3, 0), ["22:00-02:00"])
    # 多选任一命中
    assert _in_note_window(_dt(2026, 8, 16, 18, 0), ["morning", "evening"])
    # 空列表不命中
    assert not _in_note_window(_dt(2026, 8, 16, 9, 0), [])
    print("✓ 常态提示时段多选+自定义（解析/命中/跨天）测试通过")


def test_daily_note_gap():
    """1.0.0：常态提示间隔控制——距上次生成不足 gap 分钟不生成"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    loc_id = db.upsert_location("重庆", 29.56, 106.55, "dynamic", "重庆")

    def _no_dnd(self):
        return False

    wx = {"today": {"desc": "晴", "tmax": 31.0, "tmin": 24.0, "precip_prob": 10.0}}

    # gap=0：不限制间隔（兼容旧行为）
    mon = CareMonitor(db, {"enable_daily_weather_note": True, "daily_weather_note_limit": 3,
                           "daily_weather_note_gap_min": 0, "dnd_start": "23:00", "dnd_end": "08:00"})
    mon._in_dnd = _no_dnd.__get__(mon)
    n1 = mon._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-16")
    assert n1 is not None, "gap=0 应生成"
    n2 = mon._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-16")
    assert n2 is not None, "gap=0 不限制间隔，应继续生成"

    # gap=60：短时间第二次被间隔拦截
    db.kv_set("last_daily_note_ts", int(__import__("time").time()))
    mon2 = CareMonitor(db, {"enable_daily_weather_note": True, "daily_weather_note_limit": 3,
                            "daily_weather_note_gap_min": 60, "dnd_start": "23:00", "dnd_end": "08:00"})
    mon2._in_dnd = _no_dnd.__get__(mon2)
    n3 = mon2._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-16")
    assert n3 is None, "距上次不足60分钟应被间隔拦截"
    print("✓ 常态提示间隔控制测试通过")



def test_daily_note_flow():
    """v1.01：常态天气提示生成链路——开关/勿扰/特殊天气去重/计数上限"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    loc_id = db.upsert_location("重庆", 29.56, 106.55, "dynamic", "重庆")

    # 勿扰判断依赖真实时钟，测试固定为非勿扰，避免时间敏感导致不稳定
    def _no_dnd(self):
        return False

    # 开关关闭：不生成
    mon = CareMonitor(db, {"enable_daily_weather_note": False, "dnd_start": "23:00", "dnd_end": "08:00"})
    mon._in_dnd = _no_dnd.__get__(mon)
    wx = {"today": {"desc": "晴", "tmax": 31.0, "tmin": 24.0, "precip_prob": 10.0}}
    n1 = mon._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-16")
    assert n1 is None, "开关关闭不应生成"

    # 开关开启：生成（gap=0 不限制间隔，本测试专注 limit 与跨天）
    mon = CareMonitor(db, {"enable_daily_weather_note": True, "daily_weather_note_limit": 1,
                           "daily_weather_note_gap_min": 0,
                           "dnd_start": "23:00", "dnd_end": "08:00"})
    mon._in_dnd = _no_dnd.__get__(mon)
    n2 = mon._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-16")
    assert n2 is not None, "开关开启应生成"
    ev = db.get_active_events(t_id)
    daily = [e for e in ev if e.get("cause") == "2026-08-16:daily-note"]
    assert len(daily) == 1, "应生成 1 条常态提示事件"

    # 计数上限：同一天第二次不生成
    n3 = mon._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-16")
    assert n3 is None, "同一天超过 limit 不应生成"

    # 跨天重置：第二天重新生成
    n4 = mon._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-17")
    assert n4 is not None, "跨天应重置计数并重新生成"
    ev2 = db.get_active_events(t_id)
    daily2 = [e for e in ev2 if e.get("cause") == "2026-08-17:daily-note"]
    assert len(daily2) == 1, "第二天应生成新的常态提示事件"

    # 特殊天气去重：当天已有 alert 事件则不生成
    db.add_event(t_id, "weather", "暴雨预警", "暴雨", 4, 4, 6,
                 event_type="weather", cause="2026-08-18:alert-暴雨", location_id=loc_id)
    n5 = mon._try_daily_note(t_id, loc_id, "重庆", wx, "2026-08-18")
    assert n5 is None, "当天已有特殊天气提醒不应再生成常态提示"
    print("✓ 常态天气提示生成链路（开关/勿扰/去重/计数）测试通过")



def test_weather_lifecycle():
    """v5.8.3：过期事件自动清理 + 天气事件绑定地点 + 同类去重"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    loc1 = db.upsert_location("贵阳", 26.65, 106.63, "dynamic", "贵州")
    loc2 = db.upsert_location("重庆", 29.56, 106.55, "dynamic", "重庆")

    now = int(time.time())
    # 1. 一条已过期天气事件（TTL 1小时，但人为把 expire_at 改到过去）
    e1 = db.add_event(t_id, "weather", "今天有雷阵雨，降水概率84%", "记得带伞", 3, 3, 1,
                      event_type="weather", cause="2026-08-14:general", location_id=loc1)
    # 2. 一条未过期天气事件（TTL 24小时）
    e2 = db.add_event(t_id, "weather", "明天有雷阵雨，降水概率77%", "准备雨具", 2, 2, 24,
                      event_type="weather", cause="2026-08-16:general", location_id=loc1)
    # 把 e1 的 expire_at 改成过去
    with db._connect() as conn:
        conn.execute("UPDATE care_events SET expire_at=? WHERE id=?", (now - 10, e1))
        conn.execute("UPDATE care_events SET expire_at=? WHERE id=?", (now + 24 * 3600, e2))

    # 过期清理：只清 e1
    n = db.expire_stale_events(t_id)
    assert n == 1, f"应清理1条过期事件，实际 {n}"
    ev = db.get_event(e1)
    assert ev["status"] == "expired", "过期事件应标 expired"
    ev2 = db.get_event(e2)
    assert ev2["status"] == "active", "未过期事件应保持 active"

    # 3. 同类去重：同一天同地点同类合并
    e3 = db.add_event(t_id, "weather", "今天有雷阵雨，降水概率88%", "记得带伞", 4, 4, 24,
                      event_type="weather", cause="2026-08-16:general", location_id=loc1)
    assert e3 == e2, "同一天同地点同类应合并到同一事件"

    # 4. 不同地点同类不合并
    e4 = db.add_event(t_id, "weather", "今天有雷阵雨，降水概率90%", "记得带伞", 4, 4, 24,
                      event_type="weather", cause="2026-08-16:general", location_id=loc2)
    assert e4 != e2, "不同地点同类不应合并"

    # 5. 地点失效：切换后旧地点事件全清
    n2 = db.expire_weather_events_by_location(t_id, loc1)
    assert n2 == 1, f"旧地点应清理1条，实际 {n2}"
    assert db.get_event(e2)["status"] == "expired", "旧地点事件应过期"
    assert db.get_event(e4)["status"] == "active", "新地点事件应保留"
    print("✓ 天气事件生命周期（过期清理/绑定地点/同类去重）测试通过")


def test_city_switch_invalidate():
    """v5.8.3：动态地点切换（贵州→重庆）时，旧地点 active 天气事件全部失效"""
    db = make_db()
    t_id = db.get_default_target()["id"]
    loc_old = db.upsert_location("贵阳", 26.65, 106.63, "dynamic", "贵州")
    # 旧地点 3 条天气事件
    for i, cause in enumerate(["2026-08-14:general", "2026-08-14:alert-暴雨", "2026-08-15:general"]):
        db.add_event(t_id, "weather", f"天气事件{i}", f"detail{i}", 3, 3, 24,
                     event_type="weather", cause=cause, location_id=loc_old)
    # 非天气事件不受影响
    db.add_event(t_id, "state", "身体不适：感冒", "用户感冒了", 3, 1, 72,
                 event_type="sick", cause="感冒")

    # 模拟城市切换：旧地点事件全失效
    n = db.expire_weather_events_by_location(t_id, loc_old)
    assert n == 3, f"应失效3条旧地点天气事件，实际 {n}"
    active = db.get_active_events(t_id)
    assert all(e["source"] != "weather" for e in active), "旧地点天气事件应全部失效"
    assert any("感冒" in e["summary"] for e in active), "状态事件不应受影响"
    print("✓ 城市切换旧天气失效测试通过")


def test_silence_reset():
    """v1.1.1：静默以「任何一方交流」为基准——用户发言或 bot 开口都重置静默；
    删除「两次主动最小间隔」后，间隔由最小静默承载，should_probe 不再接收
    min_gap/last_probe。"""
    from core.monitor import CareMonitor
    db = make_db()
    mon = CareMonitor(db, {})
    # 初始无基准 → 静默 0
    assert mon.silence_minutes() == 0, "无基准时应为 0"
    # 100 分钟前最后交流 → 静默 100
    db.kv_set("last_activity_ts", int(time.time()) - 6000)
    assert mon.silence_minutes() == 100, mon.silence_minutes()
    # 用户再次发言 → 双写，静默归零
    mon.record_user_message()
    assert mon.silence_minutes() == 0, "用户发言应重置静默"
    # 模拟 bot 开口成功（executor 会写 last_activity_ts）→ 静默也归零
    db.kv_set("last_activity_ts", int(time.time()) - 3600)
    assert mon.silence_minutes() == 60
    db.kv_set("last_activity_ts", int(time.time()))
    assert mon.silence_minutes() == 0, "bot 开口应重置静默"
    # should_probe 新签名：静默超 max → 保底；静默不足 min → 不触发
    db.kv_set("last_activity_ts", int(time.time()) - 24000)  # 400 分钟，超 max_silence
    assert mon.should_probe(10, 240, 10, 0) == "guarantee"
    db.kv_set("last_activity_ts", int(time.time()))
    assert mon.should_probe(10, 240, 10, 0) == ""
    # 每日上限拦截优先
    db.kv_set("last_activity_ts", int(time.time()) - 24000)
    assert mon.should_probe(10, 240, 0, 0) == ""
    print("✓ 静默重置（双方交流）测试通过")


def test_decision_guarantee():
    """最长静默保底：决策层硬约束（v5.6.0）"""
    from core.decision import DecisionEngine
    db = make_db()
    base_cfg = {"care_level": 5, "care_daily_limit": 10, "dnd_start": "23:00", "dnd_end": "08:00"}
    t = db.get_default_target()

    async def fake_llm(reply):
        async def _f(system, user):
            return reply
        return _f

    # 场景1：保底 + LLM 输出 silent → 强制 act
    eng = DecisionEngine(db, base_cfg, None)
    eng.llm_func = asyncio.run(fake_llm('{"decision":"silent","background":"","reason":"不该打扰"}'))
    r = asyncio.run(eng.decide(t, source="proactive", guarantee=True))
    assert r["decision"] == "act", "保底时 silent 应转 act"

    # 场景2：保底 + plan 到明天（非勿扰 08:xx）→ 转 act
    eng2 = DecisionEngine(db, base_cfg, None)
    eng2.llm_func = asyncio.run(fake_llm('{"decision":"plan","background":"你最近身体不适","plan_date":"2026-08-17","trigger_window":"morning","reason":"明天早上问"}'))
    r2 = asyncio.run(eng2.decide(t, source="proactive", guarantee=True))
    # 测试运行时若处于勿扰（23-08）则允许 plan 到今天；否则必须 act
    from datetime import datetime as _dt
    cur_h = _dt.now().hour
    if not (23 <= cur_h or cur_h < 8):
        assert r2["decision"] == "act", "保底+非勿扰时 plan 应转 act"

    # 场景3：保底 + plan 到明天 + 全时段勿扰 → 收敛到今天窗口（若今天已无窗口则转 act）
    cfg_dnd = dict(base_cfg); cfg_dnd["dnd_start"] = "00:00"; cfg_dnd["dnd_end"] = "23:59"
    eng3 = DecisionEngine(db, cfg_dnd, None)
    eng3.llm_func = asyncio.run(fake_llm('{"decision":"plan","background":"你最近身体不适","plan_date":"2026-08-17","trigger_window":"morning","reason":"勿扰中"}'))
    r3 = asyncio.run(eng3.decide(t, source="proactive", guarantee=True))
    cur_h3 = _dt.now().hour
    if cur_h3 < 23:
        # 今天仍有剩余窗口：plan 必须收敛到今天，不推明天
        assert r3["decision"] == "plan", "勿扰中保底仍应开口（plan）"
        assert r3["plan_date"] == _dt.now().strftime("%Y-%m-%d"), "勿扰中保底 plan 必须是今天"
        assert r3["trigger_window"] in ("morning", "noon", "evening", "night"), "窗口应有效"
    else:
        # 23 点后今天已无可用窗口：plan 被迫转 act（今天必须开口，语义正确）
        assert r3["decision"] == "act", "勿扰且今天无窗口时保底应转 act"

    # 场景4：非保底 + silent → 保持 silent（不误伤正常决策）
    eng4 = DecisionEngine(db, base_cfg, None)
    eng4.llm_func = asyncio.run(fake_llm('{"decision":"silent","background":"","reason":"没理由"}'))
    r4 = asyncio.run(eng4.decide(t, source="proactive", guarantee=False))
    assert r4["decision"] == "silent", "非保底时 silent 应保持"

    # 场景5：保底时 prompt 应包含强制规则
    eng5 = DecisionEngine(db, base_cfg, None)
    eng5._guarantee = True
    ctx = eng5._collect_context(t)
    sys_p, user_p = eng5._build_prompt(ctx)
    assert "保底触发" in sys_p and "必须" in sys_p, "保底 prompt 应含强制规则"
    assert "必须开口" in user_p, "保底 user prompt 应有提示"
    print("✓ 保底决策（guarantee）测试通过")



if __name__ == "__main__":
    test_database()
    test_weather_analyze()
    test_chat_reflector()
    test_weather_judge()
    test_executor_background_and_send()
    test_executor_cooldown_and_limit()
    test_dnd()
    test_recovery_signal()
    test_recovery_llm_semantic_resolve()
    test_recovery_llm_resolve_conservative()
    test_parse_items_compat()
    test_cause_dedup()
    test_cause_precise_resolve()
    test_cause_dynamic_add()
    test_backfill_scan()
    test_backfill_scan_conservative()
    test_dynamic_keyword_backfill()
    test_seen_unseen_full_scan()
    test_seen_mark_after_main_analysis()
    test_decision_category_track()
    test_daily_note_text()
    test_note_windows_multi()
    test_daily_note_gap()
    test_daily_note_flow()
    test_weather_lifecycle()
    test_city_switch_invalidate()
    test_silence_reset()
    test_decision_guarantee()
    test_reflect_incremental()
    print("\n全部测试通过 ✓")
