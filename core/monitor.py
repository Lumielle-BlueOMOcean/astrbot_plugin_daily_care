# -*- coding: utf-8 -*-
"""
微光-Daily Care - 感知层（v5）
1. 天气感知：IP 定位 + 各目标地点天气 → 缓存。
   高频轮询、低频判断：规则层先检测"显著变化"，只有变化才调 LLM 判断，
   绝大多数轮询零 LLM 成本；早/午/晚三个固定时段强制 LLM 评估一次。
   接入和风天气后可选开启预警窗口（预警直接转事件，无需 LLM）。
2. 冷场感知：记录最后一条用户消息时间，供主动消息决策使用。
"""
import asyncio
import json
import time
from datetime import date, datetime, timedelta
from typing import Optional

from astrbot.api import logger

from . import geoip, weather
from .database import CareDatabase

# 固定检查时段（时:分）——每天至少强制 LLM 评估一次
FIXED_CHECK_POINTS = ["08:30", "13:00", "19:30"]

# 天气代码类别（用于规则层"是否显著变化"判断）
_CAT_SUNNY = {0, 1, 2, 3, 45, 48}
_CAT_RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
_CAT_SNOW = {71, 73, 75, 77, 85, 86}
_CAT_STORM = {95, 96, 99}


def _code_category(code: int) -> str:
    if code in _CAT_STORM:
        return "storm"
    if code in _CAT_SNOW:
        return "snow"
    if code in _CAT_RAIN:
        return "rain"
    return "fine"


class CareMonitor:
    def __init__(self, db: CareDatabase, config: dict):
        self.db = db
        self.config = config

    # ---------- 初始化目标 ----------
    async def ensure_targets(self, session=None) -> None:
        """确保默认目标（用户自己）和配置的关系人物存在"""
        default = self.db.get_default_target()
        if default is None:
            loc_id = self.db.upsert_location("动态定位", 0, 0, "dynamic", "IP")
            self.db.add_target("你", "用户自己", loc_id, is_default=1, is_dynamic=1)
        try:
            relations = json.loads(self.config.get("relation_cities", "[]") or "[]")
        except Exception:
            relations = []
        existing = {t["name"] for t in self.db.get_all_targets()}
        for rel in relations:
            name = str(rel.get("name", "")).strip()
            city = str(rel.get("city", "")).strip()
            relation = str(rel.get("relation", "")).strip()
            if not name or not city or name in existing:
                continue
            gc = await geoip.geocode_city(city, session)
            if gc:
                loc_id = self.db.upsert_location(gc["city"], gc["lat"], gc["lon"], "static", gc["region"])
                self.db.add_target(name, relation, loc_id)
                existing.add(name)

    # ---------- v1.01 常态天气提示 ----------
    def _try_daily_note(self, target_id: int, location_id: int,
                       loc_name: str, wx: dict, today: str) -> Optional[str]:
        """尝试生成今日常态天气提示事件。

        生成条件（全部满足才生成）：
          1. enable_daily_weather_note 开启
          2. 当前不在勿扰时段（深夜不生成，等白天的下一次天气检查）
          3. 当天该地点尚无特殊天气提醒（预警/LLM判断）——不重复打扰
          4. 当天生成次数未达 daily_weather_note_limit（kv 计数，跨天重置）
        返回生成的事件 summary；未生成返回 None。
        """
        if not self.config.get("enable_daily_weather_note", False):
            return None
        if self._in_dnd():
            return None
        with self.db._connect() as conn:
            dup = conn.execute(
                "SELECT id FROM care_events WHERE status='active' "
                "AND source='weather' AND target_id=? AND location_id=? "
                "AND (cause LIKE ? OR cause LIKE ?)",
                (target_id, location_id, f"{today}:alert-%", f"{today}:general%"),
            ).fetchone()
        if dup:
            return None
        limit = int(self.config.get("daily_weather_note_limit", 1) or 1)
        key = f"daily_note_{target_id}"
        val = self.db.kv_get(key, "") or ""
        cnt = 0
        if val.startswith(today + ":"):
            try:
                cnt = int(val.split(":", 1)[1] or 0)
            except ValueError:
                cnt = 0
        if cnt >= limit:
            return None
        # 1.0.0：间隔控制——距上次常态提示不足 gap 分钟不生成（防短时间连续）
        gap_min = self.config.get("daily_weather_note_gap_min", 60)
        gap_min = int(gap_min) if gap_min is not None else 60  # 0 合法：不限制间隔
        last_note_ts = int(self.db.kv_get("last_daily_note_ts", 0) or 0)
        if last_note_ts and (time.time() - last_note_ts) < gap_min * 60:
            return None
        self.db.kv_set(key, f"{today}:{cnt + 1}")
        self.db.kv_set("last_daily_note_ts", int(time.time()))
        note = self.daily_note_text(loc_name, wx)
        self.db.add_event(
            target_id=target_id,
            source="weather",
            summary=note,
            detail=note,
            event_type="weather",
            cause=f"{today}:daily-note",
            location_id=location_id,
            intensity=1,
            priority=1,
            ttl_hours=24,
        )
        return note

    # ---------- 勿扰时段 ----------
    def _in_dnd(self) -> bool:
        """是否处于勿扰时段（与 decision/executor 对齐）。"""
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

    # ---------- v1.01 常态天气提示 ----------
    @staticmethod
    def daily_note_text(loc_name: str, wx: dict) -> str:
        """由天气数据生成『客观天气事实』文本，供常态天气提示事件使用。

        只产出事实（温度/天气/温差/降水），不含话术——开口永远是 bot 本人，
        它看到这段事实后会用自己的语气自然表达。
        温度体感区间按当日最高温映射：
          >=35 炎热 / >=30 热 / >=24 温暖 / >=16 适宜 / >=8 偏凉 / <8 很冷
        """
        today = wx.get("today") or {}
        desc = today.get("desc") or ""
        tmax = today.get("tmax")
        tmin = today.get("tmin")
        precip = today.get("precip_prob")
        parts = [f"今天{loc_name}"]
        if desc:
            parts.append(desc)
        if tmax is not None:
            parts.append(f"最高{tmax:.0f}°C")
        if tmin is not None:
            parts.append(f"最低{tmin:.0f}°C")
        if precip is not None and precip > 0:
            parts.append(f"降水概率{precip:.0f}%")
        fact = "，".join(parts) + "。"
        # 体感区间（追加为客观描述，供 bot 判断语气）
        if tmax is not None:
            t = float(tmax)
            if t >= 35:
                fact += f"体感炎热（最高{t:.0f}°C以上）。"
            elif t >= 30:
                fact += f"体感偏热（最高{t:.0f}°C）。"
            elif t >= 24:
                fact += "体感温暖舒适。"
            elif t >= 16:
                fact += "体感温度适宜。"
            elif t >= 8:
                fact += "体感偏凉，注意添衣。"
            else:
                fact += "体感寒冷，注意保暖。"
        return fact

    # ---------- 规则层：显著变化检测 ----------
    @staticmethod
    def _is_significant_change(prev: Optional[dict], cur: dict) -> bool:
        """纯代码比较：天气是否有显著变化（零 LLM 成本）。"""
        if not prev:
            return True  # 无上次快照（新的一天），需要评估
        try:
            p_today = prev.get("today") or {}
            c_today = cur.get("today") or {}
            # 1. 天气类别变化（晴↔雨、无雷↔雷）
            p_code = int(p_today.get("weather_code") or 0)
            c_code = int(c_today.get("weather_code") or 0)
            if _code_category(p_code) != _code_category(c_code):
                return True
            # 2. 降水概率大幅变化（≥30%）
            p_prob = p_today.get("precip_prob")
            c_prob = c_today.get("precip_prob")
            if p_prob is not None and c_prob is not None:
                try:
                    if abs(float(c_prob) - float(p_prob)) >= 30:
                        return True
                except (TypeError, ValueError):
                    pass
            # 3. 温度骤变（≥8°C）
            p_tmax = p_today.get("tmax")
            c_tmax = c_today.get("tmax")
            if p_tmax is not None and c_tmax is not None:
                try:
                    if abs(float(c_tmax) - float(p_tmax)) >= 8:
                        return True
                except (TypeError, ValueError):
                    pass
            # 4. 预警变化（新增/级别提升）
            p_alerts = prev.get("alerts") or []
            c_alerts = cur.get("alerts") or []
            p_keys = {a.get("id") or a.get("title", "") for a in p_alerts}
            c_keys = {a.get("id") or a.get("title", "") for a in c_alerts}
            if c_keys - p_keys:
                return True
        except Exception as e:
            logger.debug(f"[DailyCare] 变化检测异常(视为有变化): {e}")
            return True
        return False

    # ---------- 天气感知 ----------
    async def check_weather(self, session=None, judge=None,
                            force_judge: bool = False) -> list[str]:
        """检查所有地点的天气。

        judge: WeatherJudge（可选）。规则层检测到显著变化（或 force_judge）
        时才调 LLM 判断。返回新增事件摘要列表。
        force_judge: 测试/固定时段时强制 LLM 判断。
        """
        if session is None:
            import aiohttp
            session = aiohttp.ClientSession()
            close = True
        else:
            close = False
        new_events = []
        try:
            # v5.8.3：先清理已过 TTL 的过期事件（天气提醒当天有效，过了就清）
            self.db.expire_stale_events()

            # 1. 用户动态地点：IP 定位（每天最多一次，缓存）
            loc = None
            last_ip_locate = self.db.kv_get("last_ip_locate_ts", 0)
            if time.time() - last_ip_locate > 12 * 3600:
                loc = await geoip.locate_by_ip(session)
                self.db.kv_set("last_ip_locate_ts", int(time.time()))
            default = self.db.get_default_target()
            if loc and default:
                dyn_loc = self.db.get_dynamic_location()
                if not dyn_loc:
                    loc_id = self.db.upsert_location(loc["city"], loc["lat"], loc["lon"], "dynamic", loc["region"])
                    self.db.update_target_location(default["id"], loc_id)
                elif dyn_loc["name"] != loc["city"]:
                    # v5.8.3：地点切换 → 旧地点的天气事件全部失效（人走了，旧天气提醒没意义）
                    old_loc_id = dyn_loc["id"]
                    with self.db._connect() as conn:
                        conn.execute(
                            "UPDATE locations SET name=?, lat=?, lon=?, region=? WHERE id=?",
                            (loc["city"], loc["lat"], loc["lon"], loc["region"], dyn_loc["id"]),
                        )
                    if default:
                        n = self.db.expire_weather_events_by_location(default["id"], old_loc_id)
                        if n:
                            logger.info(f"[DailyCare] 地点切换 {loc['city']}，旧地点天气事件失效 {n} 条")
                    logger.info(f"[DailyCare] 动态地点更新为: {loc['city']}")

            # 2. 遍历所有地点查天气
            locations = self.db.get_all_locations()
            today = date.today().isoformat()
            qkey = str(self.config.get("qweather_key", "") or "").strip()
            enable_alerts = self.config.get("enable_qweather_alerts", True)
            self.db.kv_set("last_weather_ts", int(time.time()))
            for loc_row in locations:
                if not loc_row["lat"] or not loc_row["lon"]:
                    continue
                wx = await weather.fetch_weather_auto(
                    loc_row["lat"], loc_row["lon"], qkey, session,
                    self.config.get("timezone", "Asia/Shanghai"))
                if not wx:
                    continue
                # 规则层变化检测
                prev = self.db.get_weather_cache(loc_row["id"], today)
                significant = self._is_significant_change(prev, wx)
                # 写入缓存
                self.db.set_weather_cache(loc_row["id"], today, wx)

                # 2a. 和风预警：直接转事件（无需 LLM）
                alerts = wx.get("alerts") or []
                if alerts and qkey and enable_alerts:
                    for a in weather.analyze_alerts(alerts):
                        # v5.8.3：预警事件绑定地点 + 日期维度 cause（同一天同类合并）
                        eid = self.db.add_event(
                            target_id=default["id"] if default else 0,
                            source="weather",
                            summary=a["summary"],
                            detail=a["detail"],
                            event_type="weather",
                            cause=f"{today}:alert-{a['summary'][:10]}",
                            location_id=loc_row["id"],
                            intensity=a["intensity"],
                            priority=a["priority"],
                            ttl_hours=a["ttl_hours"],
                        )
                        new_events.append(f"[预警] {a['summary']}")

                # 2b. LLM 判断（显著变化 或 强制 且 未在冷却内）
                if judge is not None and self.config.get("enable_weather_judge", True):
                    if force_judge or significant:
                        try:
                            background = await judge.judge(
                                loc_row["id"], loc_row["name"], wx,
                                int(self.config.get("weather_cooldown_hours", 6)),
                            )
                        except Exception as e:
                            logger.warning(f"[DailyCare] 天气判断异常: {e}")
                            background = None
                        if background:
                            with self.db._connect() as conn:
                                targets = conn.execute(
                                    "SELECT id FROM care_targets WHERE location_id=?",
                                    (loc_row["id"],),
                                ).fetchall()
                            for t in targets:
                                eid = self.db.add_event(
                                    target_id=t["id"],
                                    source="weather",
                                    summary=background,
                                    detail=background,
                                    event_type="weather",
                                    cause=f"{today}:general",
                                    location_id=loc_row["id"],
                                    intensity=3,
                                    priority=3,
                                    ttl_hours=24,
                                )
                                new_events.append(f"{loc_row['name']}: {background}")

                # 2c. v1.01 常态天气提示：无特殊天气也每天问候天气
                if default:
                    note = self._try_daily_note(default["id"], loc_row["id"],
                                                loc_row["name"], wx, today)
                    if note:
                        new_events.append(f"[常态天气] {note[:40]}")
            return new_events
        finally:
            if close:
                await session.close()

    # ---------- 冷场感知 ----------
    def record_user_message(self, ts: Optional[int] = None) -> None:
        """记录用户最后发言时间（由 main 的消息入口调用）。

        v1.1.1：静默基准改为「任何一方交流」——用户发言同时更新
        last_activity_ts，bot 主动开口成功也会更新它（见 executor）。
        last_user_msg_ts 保留，供决策 prompt 展示「用户静默时长」。
        """
        now = int(ts or time.time())
        self.db.kv_set("last_user_msg_ts", now)
        self.db.kv_set("last_activity_ts", now)

    def silence_minutes(self) -> int:
        """当前已静默的分钟数（以最后任何一方交流为基准）。"""
        last = self.db.kv_get("last_activity_ts", 0)
        if not last:
            return 0
        return max(0, int((time.time() - last) / 60))

    def should_probe(self, min_silence: int, max_silence: int,
                     daily_limit: int, today_count: int) -> str:
        """冷场主动消息的触发判定（随机概率 + 保底边界）。

        返回触发类型：
        - "guarantee"：达到 max_silence 保底触发（最长等待窗口的保证线，
          静默区间内【必定】要开口一次，决策层不得无限后推）
        - "prob"：min~max 之间概率触发（15% 平滑上升到接近 100%）
        - ""：不触发

        语义（v5.4.1 / v1.1.1）：静默小于 min_silence 不打扰；达到 max_silence
        保底触发，保证不会让人等超过 max_silence 还没有主动消息；min~max 之间
        概率从 15% 平滑上升到接近 100%，静默越久越应该被关心。

        v1.1.1：删除「两次主动最小间隔」配置。由于静默以任何一方交流为基准
        （bot 开口成功也会重置静默），发完一条主动消息后静默归零，必须重新
        攒够 min_silence 才有下一轮资格——间隔语义已由 min_silence 完全承载，
        无需单独的 min_gap；每日上限保留作最终防线。
        """
        import random as _r
        silence = self.silence_minutes()
        if silence < min_silence:
            return ""
        if today_count >= daily_limit:
            return ""
        # 达到最大静默：保底触发（最长等待窗口的保证线）
        if silence >= max_silence:
            return "guarantee"
        # min~max 之间：概率从 15% 平滑涨到接近 100%
        ratio = min(1.0, (silence - min_silence) / max(1, (max_silence - min_silence)))
        prob = 0.15 + 0.85 * ratio
        return "prob" if _r.random() < prob else ""

    # ---------- 固定时段判断 ----------
    @staticmethod
    def is_fixed_check_point(tolerance_min: int = 15) -> bool:
        """当前时刻是否接近固定检查时段（早/午/晚）。"""
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        for point in FIXED_CHECK_POINTS:
            h, m = map(int, point.split(":"))
            p = h * 60 + m
            if abs(cur - p) <= tolerance_min:
                return True
        return False
