# -*- coding: utf-8 -*-
"""
微光-Daily Care - WebUI 后端 API
通过 astrbot 的 register_web_api 注册 REST 接口，供「关怀面板」前端调用。
"""
import json
from datetime import date, datetime
from typing import Any

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_daily_care"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"

# 允许通过 WebUI 修改的配置白名单（v2 + v1.1.2 补齐）
# v1.1.2：修复「天气相关配置保存后变回默认」——常态天气提示四件套
# （enable_daily_weather_note/limit/window/gap_min）与 platform_id 此前
# 只在 overview 返回、可显示可修改，但不在白名单内，保存时被静默过滤。
SETTABLE_KEYS = {
    "care_level", "care_daily_limit", "enable_chat_monitor", "chat_reflect_interval",
    "dnd_start", "dnd_end", "rest_after_sleep_hours", "weather_check_interval", "enable_weather_judge",
    "weather_cooldown_hours", "care_cooldown_minutes", "decision_interval",
    "enable_qweather_alerts", "weather_tool_enabled", "decision_llm_id",
    "enable_proactive", "probe_min_silence_min", "probe_max_silence_min",
    "probe_daily_limit", "probe_interval", "enable_recent_topic",
    "silence_exclude_window_min",
    "relation_cities", "timezone", "target_user_id", "target_city",
    "target_group_id", "qweather_key",
    "enable_daily_weather_note", "daily_weather_note_limit",
    "daily_weather_note_window", "daily_weather_note_gap_min",
    "platform_id",
}


class CareWebAPI:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/overview", self.get_overview, ["GET"], "Daily Care overview"),
            ("/targets", self.list_targets, ["GET"], "Daily Care targets"),
            ("/targets/add", self.add_target, ["POST"], "Daily Care add target"),
            ("/targets/delete", self.delete_target, ["POST"], "Daily Care delete target"),
            ("/events", self.list_events, ["GET"], "Daily Care events"),
            ("/events/resolved", self.list_resolved_events, ["GET"], "Daily Care resolved events"),
            ("/events/resolve", self.resolve_event, ["POST"], "Daily Care resolve event"),
            ("/plans", self.list_plans, ["GET"], "Daily Care plans"),
            ("/plans/cancel", self.cancel_plan, ["POST"], "Daily Care cancel plan"),
            ("/sends", self.list_sends, ["GET"], "Daily Care send log"),
            ("/locations", self.list_locations, ["GET"], "Daily Care locations"),
            ("/settings", self.update_settings, ["POST"], "Daily Care update settings"),
            ("/actions/weather", self.action_weather, ["POST"], "Daily Care manual weather check"),
            ("/actions/reflect", self.action_reflect, ["POST"], "Daily Care manual reflect"),
            ("/actions/send", self.action_send, ["POST"], "Daily Care manual send"),
            ("/actions/decision", self.action_decision, ["POST"], "Daily Care manual decision"),
            ("/actions/locate", self.action_locate, ["POST"], "Daily Care manual IP locate"),
            ("/decisions", self.list_decisions, ["GET"], "Daily Care decision log"),
        ]
        for path, handler, methods, desc in routes:
            register(f"{PAGE_API_PREFIX}{path}", handler, methods, desc)
        logger.info(f"[DailyCare] WebUI API 已注册 {len(routes)} 条路由")

    # ---------- 工具 ----------
    def _cfg(self, key: str, default=None):
        return self.plugin._cfg(key, default)

    async def _body(self, params: dict = None) -> dict:
        """获取请求体。

        AstrBot 的 register_web_api 不会把 POST body 作为参数传入 handler，
        handler 必须自己从 Quart 的 request 读取 JSON（参考 livingmemory 插件）。
        这里优先使用传入 params（兼容旧路径），为空时从 request 读取。
        """
        if isinstance(params, dict) and params:
            return params
        try:
            from quart import request
            raw = await request.get_json(silent=True)
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
        return params if isinstance(params, dict) else {}

    def _ok(self, data: Any = None, msg: str = "ok") -> dict:
        return {"code": 0, "status": "ok", "message": msg, "data": data}

    def _err(self, msg: str) -> dict:
        return {"code": 1, "status": "error", "message": msg, "data": None}

    # ---------- 总览 ----------
    async def get_overview(self, params: dict = None) -> dict:
        db = self.plugin.db
        stats = db.stats()
        profile = db.profile_all()
        # 今日已开口统计
        today = date.today().isoformat()
        default_target = db.get_default_target()
        today_sent = db.count_send_today(default_target["id"], today) if default_target else 0
        pending_today = len(db.get_pending_plans(today))
        # 活跃事件按来源统计
        events = db.get_active_events()
        by_source = {}
        for e in events:
            by_source[e["source"]] = by_source.get(e["source"], 0) + 1
        return self._ok({
            "stats": stats,
            "pending_today": pending_today,
            "today_sent": today_sent,
            "events_by_source": by_source,
            "config": {
                "care_level": self._cfg("care_level", 5),
                "care_daily_limit": self._cfg("care_daily_limit", 2),
                "enable_chat_monitor": self._cfg("enable_chat_monitor", True),
                "chat_reflect_interval": self._cfg("chat_reflect_interval", 60),
                "dnd_start": self._cfg("dnd_start", "23:00"),
                "dnd_end": self._cfg("dnd_end", "08:00"),
                "weather_check_interval": self._cfg("weather_check_interval", 30),
                "enable_weather_judge": self._cfg("enable_weather_judge", True),
                "weather_cooldown_hours": self._cfg("weather_cooldown_hours", 6),
                "care_cooldown_minutes": self._cfg("care_cooldown_minutes", 240),
                "decision_interval": self._cfg("decision_interval", 25),
                "enable_qweather_alerts": self._cfg("enable_qweather_alerts", True),
                "weather_tool_enabled": self._cfg("weather_tool_enabled", True),
                "decision_llm_id": self._cfg("decision_llm_id", ""),
                "enable_proactive": self._cfg("enable_proactive", True),
                "probe_min_silence_min": self._cfg("probe_min_silence_min", 180),
                "probe_max_silence_min": self._cfg("probe_max_silence_min", 600),
                "probe_daily_limit": self._cfg("probe_daily_limit", 2),
                "probe_interval": self._cfg("probe_interval", 10),
                "timezone": self._cfg("timezone", "Asia/Shanghai"),
                "target_user_id": self._cfg("target_user_id", ""),
                "target_group_id": self._cfg("target_group_id", ""),
                "qweather_key": self._cfg("qweather_key", ""),
                "enable_daily_weather_note": self._cfg("enable_daily_weather_note", False),
                "daily_weather_note_limit": self._cfg("daily_weather_note_limit", 1),
                "daily_weather_note_gap_min": self._cfg("daily_weather_note_gap_min", 60),
                "daily_weather_note_window": self._cfg("daily_weather_note_window", '["morning"]'),
                "platform_id": self._cfg("platform_id", "auto"),
                "relation_cities": self._cfg("relation_cities", "[]"),
            },
            "profile": profile,
            "persona_loaded": bool(self.plugin.persona_prompt),
            "runtime": {
                "db_path": db.db_path,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_reflect_ts": db.kv_get("last_reflect_ts", 0),
                "last_weather_ts": db.kv_get("last_weather_ts", 0),
                "last_weather_remind_ts": db.kv_get("last_weather_remind_ts", 0),
            },
        })

    # ---------- 关怀对象 ----------
    async def list_targets(self, params: dict = None) -> dict:
        db = self.plugin.db
        targets = db.get_all_targets()
        result = []
        for t in targets:
            loc = db.get_location(t["location_id"]) if t["location_id"] else None
            result.append({
                "id": t["id"],
                "name": t["name"],
                "relation": t.get("relation", ""),
                "user_id": t.get("user_id", ""),
                "is_default": t.get("is_default", 0),
                "is_dynamic": t.get("is_dynamic", 0),
                "location": loc["name"] if loc else "未配置",
                "lat": loc["lat"] if loc else None,
                "lon": loc["lon"] if loc else None,
            })
        return self._ok(result)

    async def action_locate(self, params: dict = None) -> dict:
        """手动触发一次 IP 定位，并刷新动态地点（绕过 12 小时缓存）。

        定位结果落库到 locations（dynamic），供天气链路使用。
        """
        try:
            import aiohttp
            from . import geoip
            loc = None
            # v1.1.6：手动指定城市优先，否则 IP 定位
            target_city = str(self.plugin._cfg("target_city", "") or "").strip()
            if target_city:
                gc = await geoip.geocode_city(target_city)
                if gc:
                    loc = {"city": gc["city"], "lat": gc["lat"], "lon": gc["lon"],
                           "region": gc.get("region", "")}
            if not loc:
                async with aiohttp.ClientSession() as s:
                    loc = await geoip.locate_by_ip(s)
            if not loc:
                return self._err("IP 定位失败（可能是出口 IP 无城市信息）")
            db = self.plugin.db
            default = db.get_default_target()
            # v1.1.6：动态定位唯一化——修复旧版 upsert 城市名不同会 INSERT 新记录、
            # 且 update_target_location 用了旧 dyn id 导致默认目标仍指向旧城市的问题
            old_dyn = db.get_dynamic_location()
            if default:
                new_id = db.sync_dynamic_location(
                    loc["city"], loc["lat"], loc["lon"], loc["region"], default["id"]
                )
                if old_dyn and old_dyn["name"] != loc["city"]:
                    db.expire_weather_events_by_location(default["id"], old_dyn["id"])
            else:
                db.sync_dynamic_location(loc["city"], loc["lat"], loc["lon"], loc["region"])
            db.kv_set("last_ip_locate_ts", int(time.time()))
            return self._ok({
                "city": loc["city"], "region": loc.get("region", ""),
                "lat": loc["lat"], "lon": loc["lon"],
            }, f"IP 定位成功：{loc['city']}（{loc.get('region','')}）")
        except Exception as e:
            return self._err(f"IP 定位失败: {e}")

    async def add_target(self, params: dict = None) -> dict:
        body = await self._body(params)
        name = str(body.get("name", "")).strip()
        city = str(body.get("city", "")).strip()
        relation = str(body.get("relation", "")).strip()
        user_id = str(body.get("user_id", "")).strip()
        if not name or not city:
            return self._err("name 和 city 不能为空")
        try:
            from . import geoip
            gc = await geoip.geocode_city(city)
            if not gc:
                return self._err(f"无法定位城市「{city}」，请检查城市名")
            db = self.plugin.db
            loc_id = db.upsert_location(gc["city"], gc["lat"], gc["lon"], "static", gc["region"])
            db.add_target(name, relation, loc_id, user_id=user_id)
            # 同步到配置
            await self._sync_relation_cities()
            return self._ok({"name": name, "city": gc["city"], "user_id": user_id, "message": "已添加关怀对象"})
        except Exception as e:
            logger.error(f"[DailyCare] 添加关怀对象失败: {e}")
            return self._err(f"添加失败: {e}")

    async def delete_target(self, params: dict = None) -> dict:
        body = await self._body(params)
        try:
            tid = int(body.get("id", 0))
        except Exception:
            return self._err("id 无效")
        db = self.plugin.db
        t = db.get_target(tid)
        if not t:
            return self._err("关怀对象不存在")
        if t.get("is_default"):
            return self._err("默认对象（用户自己）不可删除")
        with db._connect() as conn:
            conn.execute("DELETE FROM care_targets WHERE id=?", (tid,))
            conn.execute("UPDATE care_events SET status='expired' WHERE target_id=?", (tid,))
            conn.execute("UPDATE care_plans SET status='cancelled' WHERE target_id=? AND status='pending'", (tid,))
        await self._sync_relation_cities()
        return self._ok({"message": "已删除"})

    async def _sync_relation_cities(self) -> None:
        """将数据库中的关系人物同步回 relation_cities 配置"""
        db = self.plugin.db
        targets = [t for t in db.get_all_targets() if not t.get("is_default")]
        rels = []
        for t in targets:
            loc = db.get_location(t["location_id"]) if t["location_id"] else None
            if loc:
                rels.append({
                    "qq": t.get("user_id", ""),
                    "name": t["name"],
                    "relation": t.get("relation", ""),
                    "city": loc["name"],
                })
        await self._update_config({"relation_cities": json.dumps(rels, ensure_ascii=False)})

    # ---------- 事件 ----------
    async def list_events(self, params: dict = None) -> dict:
        db = self.plugin.db
        events = db.get_active_events()
        targets = {t["id"]: t for t in db.get_all_targets()}
        result = []
        for e in events:
            t = targets.get(e["target_id"], {})
            result.append({
                "id": e["id"],
                "target": t.get("name", "?"),
                "source": e["source"],
                "summary": e["summary"],
                "detail": e["detail"],
                "intensity": e["intensity"],
                "priority": e["priority"],
                "created": datetime.fromtimestamp(e["created_at"]).strftime("%m-%d %H:%M"),
                "expire": datetime.fromtimestamp(e["expire_at"]).strftime("%m-%d %H:%M") if e["expire_at"] else "持续",
                "status": e["status"],
            })
        return self._ok(result)

    async def list_resolved_events(self, params: dict = None) -> dict:
        db = self.plugin.db
        events = db.get_resolved_events(limit=50)
        targets = {t["id"]: t for t in db.get_all_targets()}
        result = []
        for e in events:
            t = targets.get(e["target_id"], {})
            result.append({
                "id": e["id"],
                "target": t.get("name", "?"),
                "source": e["source"],
                "summary": e["summary"],
                "detail": e["detail"],
                "intensity": e["intensity"],
                "priority": e["priority"],
                "created": datetime.fromtimestamp(e["created_at"]).strftime("%m-%d %H:%M"),
                "resolved": datetime.fromtimestamp(e["resolved_at"]).strftime("%m-%d %H:%M") if e["resolved_at"] else "—",
                "status": e["status"],
            })
        return self._ok(result)

    async def resolve_event(self, params: dict = None) -> dict:
        body = await self._body(params)
        try:
            eid = int(body.get("id", 0))
        except Exception:
            return self._err("id 无效")
        db = self.plugin.db
        if not db.get_event(eid):
            return self._err("事件不存在")
        db.resolve_event(eid)
        db.cancel_plans_by_event(eid)
        return self._ok({"message": "事件已标记为已解决，相关计划已取消"})

    # ---------- 计划 ----------
    async def list_plans(self, params: dict = None) -> dict:
        db = self.plugin.db
        targets = {t["id"]: t for t in db.get_all_targets()}
        today = date.today().isoformat()
        plans = db.get_pending_plans(today)
        result = []
        for p in plans:
            t = targets.get(p["target_id"], {})
            result.append({
                "id": p["id"],
                "target": t.get("name", "?"),
                "date": p["plan_date"],
                "window": p["trigger_window"],
                "task_type": p["task_type"],
                "content": p["content_summary"],
            })
        return self._ok(result)

    async def cancel_plan(self, params: dict = None) -> dict:
        body = await self._body(params)
        try:
            pid = int(body.get("id", 0))
        except Exception:
            return self._err("id 无效")
        db = self.plugin.db
        db.mark_plan(pid, "cancelled")
        return self._ok({"message": "计划已取消"})

    # ---------- 发送记录 ----------
    async def list_sends(self, params: dict = None) -> dict:
        db = self.plugin.db
        targets = {t["id"]: t for t in db.get_all_targets()}
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM send_log ORDER BY sent_at DESC LIMIT 50"
            ).fetchall()
        result = []
        for r in rows:
            t = targets.get(r["target_id"], {})
            result.append({
                "id": r["id"],
                "target": t.get("name", "?"),
                "content": r["content"],
                "sent_at": datetime.fromtimestamp(r["sent_at"]).strftime("%m-%d %H:%M:%S"),
            })
        return self._ok(result)

    # ---------- 地点/天气 ----------
    async def list_locations(self, params: dict = None) -> dict:
        db = self.plugin.db
        locations = db.get_all_locations()
        result = []
        today = date.today().isoformat()
        for loc in locations:
            wx = db.get_weather_cache(loc["id"], today) or {}
            today_wx = wx.get("today") or {}
            result.append({
                "id": loc["id"],
                "name": loc["name"],
                "type": loc["type"],
                "region": loc.get("region", ""),
                "lat": loc["lat"],
                "lon": loc["lon"],
                "weather_desc": today_wx.get("desc", "暂无数据"),
                "tmax": today_wx.get("tmax"),
                "tmin": today_wx.get("tmin"),
                "precip_prob": today_wx.get("precip_prob"),
            })
        return self._ok(result)

    # ---------- 配置更新 ----------
    async def update_settings(self, params: dict = None) -> dict:
        body = await self._body(params)
        updates = {}
        for k, v in body.items():
            if k in SETTABLE_KEYS:
                updates[k] = v
        if not updates:
            return self._err("没有可更新的配置项")
        await self._update_config(updates)
        # 同步到运行时组件，保证改动立即生效（修复快照问题）
        try:
            self.plugin._refresh_runtime_config()
        except Exception as e:
            logger.warning(f"[DailyCare] 运行时配置刷新失败: {e}")
        updates = dict(updates) if isinstance(updates, dict) else {}
        updates["message"] = "配置已更新"
        return self._ok(updates)

    async def _update_config(self, updates: dict) -> None:
        """更新内存配置 + 持久化到 AstrBot 配置文件"""
        cfg = self.plugin.config
        try:
            for k, v in updates.items():
                cfg[k] = v
        except Exception as e:
            logger.debug(f"[DailyCare] 内存配置更新失败: {e}")
        # 持久化
        try:
            import os
            conf_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "..", "config")
            conf_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "..", "config", "astrbot_plugin_daily_care_config.json")
            if os.path.exists(conf_path):
                # AstrBot 生成的配置文件带 UTF-8 BOM，必须用 utf-8-sig 读取，
                # 否则 json.load 会抛 "Unexpected UTF-8 BOM"。
                with open(conf_path, encoding="utf-8-sig") as f:
                    data = json.load(f)
                data.update(updates)
                with open(conf_path, "w", encoding="utf-8-sig") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                logger.info(f"[DailyCare] 配置已持久化: {list(updates.keys())}")
        except Exception as e:
            logger.warning(f"[DailyCare] 配置持久化失败: {e}")

    # ---------- 手动测试 ----------
    async def action_weather(self, params: dict = None) -> dict:
        try:
            evs = await self.plugin.monitor.check_weather(judge=self.plugin._weather_judge)
            return self._ok({"events": evs, "message": f"天气检查完成，新增/更新 {len(evs)} 条事件"})
        except Exception as e:
            return self._err(f"天气检查失败: {e}")

    async def action_reflect(self, params: dict = None) -> dict:
        try:
            if not self.plugin._reflection or not self.plugin._executor:
                return self._err("反思引擎未初始化")
            r = await self.plugin._do_chat_reflect()
            return self._ok({"events": r, "message": f"状态反思完成：写入 {r['created']} 条事件，关闭 {r['resolved']} 条旧事件"})
        except Exception as e:
            return self._err(f"反思失败: {e}")

    async def action_send(self, params: dict = None) -> dict:
        """测试按钮：实时走一遍完整关怀链路。

        检测 IP → 查询天气（规则层过滤 + LLM 判断）→ 由 bot 本人开口。
        """
        try:
            if not self.plugin._executor:
                return self._err("执行引擎未初始化")
            import aiohttp
            async with aiohttp.ClientSession() as s:
                evs = await self.plugin.monitor.check_weather(
                    s, judge=self.plugin._weather_judge, force_judge=True)
            # 基于活跃事件触发 bot 本人开口
            target = self.plugin.db.get_default_target()
            if not target:
                return self._err("无关怀对象")
            events = self.plugin.db.get_active_events(target["id"])
            bg = self.plugin._executor._compose_background(target, events) if events else ""
            if not bg:
                return self._ok({"events": evs, "sent": [], "message": "天气检查完成，当前无值得开口的事件"})
            sent = await self.plugin._executor.test_send(bg)
            return self._ok({"events": evs, "sent": sent, "message": f"天气检查 {len(evs)} 条，已唤醒开口 {len(sent)} 条"})
        except Exception as e:
            return self._err(f"测试关怀失败: {e}")

    async def action_decision(self, params: dict = None) -> dict:
        """测试按钮：手动触发一次 LLM 决策（act/plan/silent）。"""
        try:
            if not self.plugin._decision:
                return self._err("决策引擎未初始化")
            d = await self.plugin._run_decision(source="manual")
            label = {"act": "立即开口", "plan": "规划未来", "silent": "沉默", "act_cooled": "开口被冷却拦截"}.get(d, d)
            return self._ok({"decision": d, "message": f"决策结果：{label}"})
        except Exception as e:
            return self._err(f"决策失败: {e}")

    async def list_decisions(self, params: dict = None) -> dict:
        """决策日志（v5）：最近 LLM 决策记录。"""
        db = self.plugin.db
        rows = db.get_recent_decisions(limit=30)
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "source": r["source"],
                "decision": r["decision"],
                "background": r["background"],
                "plan_date": r["plan_date"],
                "trigger_window": r["trigger_window"],
                "reason": r["reason"],
                "created": datetime.fromtimestamp(r["created_at"]).strftime("%m-%d %H:%M:%S"),
            })
        return self._ok(result)
