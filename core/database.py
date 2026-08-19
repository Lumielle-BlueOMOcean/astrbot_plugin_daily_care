# -*- coding: utf-8 -*-
"""
微光-Daily Care - 数据层
基于 SQLite 实现关怀表：locations / care_targets / profile / care_events / care_plans / send_log / weather_cache / kv
"""
import json
import os
import sqlite3
import time
from typing import Any, Optional


class CareDatabase:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "daily_care.db")
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                -- 地点维度表（一个城市一份天气数据）
                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,            -- 地点名（城市）
                    lat REAL,
                    lon REAL,
                    type TEXT DEFAULT 'static',    -- dynamic=随IP漂移 / static=固定配置
                    region TEXT DEFAULT ''
                );

                -- 关怀对象表（人对地点）
                CREATE TABLE IF NOT EXISTS care_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,            -- 称呼（自己/老妈/对象…）
                    relation TEXT DEFAULT '',
                    location_id INTEGER,
                    user_id TEXT DEFAULT '',       -- 绑定的QQ号（可选）
                    is_default INTEGER DEFAULT 0,  -- 是否默认对象（用户自己）
                    is_dynamic INTEGER DEFAULT 0   -- 是否随IP漂移
                );

                -- 用户画像（key-value）
                CREATE TABLE IF NOT EXISTS profile (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- 临时关怀项（监测结果）
                CREATE TABLE IF NOT EXISTS care_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER NOT NULL,
                    source TEXT DEFAULT 'chat',    -- weather / chat / config
                    summary TEXT NOT NULL,         -- 内容摘要
                    detail TEXT DEFAULT '',
                    type TEXT DEFAULT '',          -- 一级大类：sick/mood/late_night/tired/diet/injury
                    cause TEXT DEFAULT '',         -- 二级病因：感冒/咳嗽嗓子/胃部…（动态）
                    location_id INTEGER DEFAULT 0, -- v5.8.3：事件绑定地点（weather 事件必填）
                    intensity INTEGER DEFAULT 1,   -- 严重程度 1-5
                    priority INTEGER DEFAULT 1,    -- 优先级 1-5
                    status TEXT DEFAULT 'active',  -- active / resolved / expired
                    created_at INTEGER NOT NULL,
                    expire_at INTEGER DEFAULT 0,   -- 0=不过期（如极端天气当天）
                    resolved_at INTEGER DEFAULT 0
                );

                -- 病因表（v5.8.0 动态化）：LLM 归因时从现有 cause 收敛选择，
                -- 语义确实对不上时新增一条。同类自然归并，恢复匹配稳定。
                CREATE TABLE IF NOT EXISTS care_causes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,            -- 所属大类
                    cause TEXT NOT NULL,           -- 病因名（二级标签）
                    keywords TEXT DEFAULT '',      -- 症状参考词（逗号分隔，供归因/恢复参考）
                    UNIQUE(type, cause)
                );

                -- 多日关怀计划（反思层产物）
                CREATE TABLE IF NOT EXISTS care_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER DEFAULT 0,
                    target_id INTEGER NOT NULL,
                    plan_date TEXT NOT NULL,       -- YYYY-MM-DD
                    trigger_window TEXT NOT NULL,  -- morning/noon/evening/night
                    task_type TEXT DEFAULT 'reminder', -- reminder陈述 / inquiry询问
                    content_summary TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending', -- pending / sent / skipped / cancelled
                    trigger_ts INTEGER DEFAULT 0,   -- 窗口内随机触发时刻（0=未定）
                    sent_at INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                -- 发送记录
                CREATE TABLE IF NOT EXISTS send_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER NOT NULL,
                    plan_id INTEGER DEFAULT 0,
                    content TEXT DEFAULT '',
                    channel TEXT DEFAULT '',
                    sent_at INTEGER NOT NULL
                );

                -- 天气缓存（防止重复提醒 + 状态对比）
                CREATE TABLE IF NOT EXISTS weather_cache (
                    location_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    payload TEXT DEFAULT '{}',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (location_id, date)
                );

                -- 决策日志（决策层产物，供防重复与调试）
                CREATE TABLE IF NOT EXISTS decision_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT DEFAULT '',      -- weather / state / proactive / plan
                    decision TEXT DEFAULT '',    -- act / silent / plan
                    background TEXT DEFAULT '',
                    plan_date TEXT DEFAULT '',
                    trigger_window TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    created_at INTEGER NOT NULL
                );

                -- 通用 kv
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- 消息流水（插件侧全量历史副本，突破框架 50 轮窗口限制）
                -- v5.7.1：每次反思把看到的 user 消息落库，供「窗口外粗筛+精判」补扫
                CREATE TABLE IF NOT EXISTS event_stream (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,          -- 该 target 内全局递增序号
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,    -- 去重锚（md5）
                    seen INTEGER DEFAULT 0,        -- v5.8.2：1=已被 LLM 覆盖（主分析/粗筛精判/全量精判）
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_event_stream_tseq ON event_stream(target_id, seq);
                """
            )

            # 迁移：旧库补 trigger_ts 列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(care_plans)").fetchall()]
            if "trigger_ts" not in cols:
                conn.execute("ALTER TABLE care_plans ADD COLUMN trigger_ts INTEGER DEFAULT 0")

            # 迁移：旧库补 type/cause 列（v5.8.0）
            ev_cols = [r[1] for r in conn.execute("PRAGMA table_info(care_events)").fetchall()]
            if "type" not in ev_cols:
                conn.execute("ALTER TABLE care_events ADD COLUMN type TEXT DEFAULT ''")
            if "cause" not in ev_cols:
                conn.execute("ALTER TABLE care_events ADD COLUMN cause TEXT DEFAULT ''")
            # 迁移：旧库补 location_id 列（v5.8.3，天气事件绑定地点）
            if "location_id" not in ev_cols:
                conn.execute("ALTER TABLE care_events ADD COLUMN location_id INTEGER DEFAULT 0")

            # 迁移：流水表补 seen 列（v5.8.2）；存量历史消息标 seen=1
            # （v5.7.1 逻辑下落库的消息均已被主分析或补扫覆盖过）
            es_cols = [r[1] for r in conn.execute("PRAGMA table_info(event_stream)").fetchall()]
            if "seen" not in es_cols:
                conn.execute("ALTER TABLE event_stream ADD COLUMN seen INTEGER DEFAULT 0")
                conn.execute("UPDATE event_stream SET seen=1")

            self._seed_causes(conn)

    @staticmethod
    def _seed_causes(conn: sqlite3.Connection) -> None:
        """写入种子病因（INSERT OR IGNORE，不覆盖 LLM 动态新增的 cause）。"""
        seeds = {
            "sick": [
                ("感冒", "感冒,着凉,流鼻涕,鼻塞,打喷嚏,受凉,风寒,风热,伤风"),
                ("咳嗽嗓子", "咳嗽,嗓子,喉咙,咽,清嗓子,干咳,咳痰,咽痛,声音哑,失声"),
                ("发烧", "发烧,发热,高烧,低烧,体温,发烫"),
                ("头痛", "头疼,头痛,头晕,偏头痛,太阳穴,眩晕"),
                ("胃部", "胃疼,胃痛,反酸,烧心,胃炎,肚子疼,胃胀,恶心,想吐,呕吐,消化不良,食欲不振"),
                ("其他不适", "不舒服,难受,没精神,乏力,嗜睡,畏寒,发冷,盗汗,浑身酸痛"),
            ],
            "mood": [
                ("失落难过", "难过,伤心,失落,想哭,沮丧,低落"),
                ("焦虑压力", "焦虑,压力,烦躁,不安,紧张,心慌,担忧"),
                ("孤独", "孤独,寂寞,一个人,没人陪"),
            ],
            "late_night": [
                ("工作晚睡", "工作,加班,调试,写代码,项目,配置,赶工"),
                ("失眠", "失眠,睡不着,睡不好,难以入睡,多梦"),
                ("娱乐晚睡", "刷手机,看剧,打游戏,玩,追剧"),
            ],
            "diet": [
                ("吃辣", "辣,火锅,烧烤,麻辣,辣椒"),
                ("吃冰", "冰,雪糕,冰淇淋,冷饮,冰镇"),
                ("不规律", "没吃饭,没吃,暴食,饿了,外卖,三餐不定"),
            ],
            "tired": [
                ("劳累", "累,疲惫,干活,加班,腰酸背痛,乏力"),
                ("游玩疲乏", "玩,走,旅游,爬山,逛,暴走"),
            ],
            "injury": [
                ("扭伤", "扭,崴,拉伤,挫伤"),
                ("烫伤", "烫,烧伤,灼伤"),
                ("摔伤", "摔,磕,碰伤,擦破"),
                ("划伤", "划,割,擦伤,划破"),
            ],
        }
        for t, causes in seeds.items():
            for cause, kws in causes:
                conn.execute(
                    "INSERT INTO care_causes(type, cause, keywords) VALUES(?,?,?) "
                    "ON CONFLICT(type, cause) DO UPDATE SET keywords=excluded.keywords",
                    (t, cause, kws),
                )


    # ---------- 通用 kv ----------
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]

    def kv_set(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # ---------- locations ----------
    def upsert_location(self, name: str, lat: float, lon: float,
                        loc_type: str = "static", region: str = "") -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM locations WHERE name=? AND type=?",
                (name, loc_type),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE locations SET lat=?, lon=?, region=?, type=? WHERE id=?",
                    (lat, lon, region, loc_type, row["id"]),
                )
                return row["id"]
            cur = conn.execute(
                "INSERT INTO locations(name,lat,lon,type,region) VALUES(?,?,?,?,?)",
                (name, lat, lon, loc_type, region),
            )
            return cur.lastrowid

    def get_location(self, loc_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM locations WHERE id=?", (loc_id,)).fetchone()
            return dict(row) if row else None

    def get_dynamic_location(self) -> Optional[dict]:
        """获取随IP漂移的用户动态地点"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT l.* FROM locations l JOIN care_targets t ON t.location_id=l.id "
                "WHERE l.type='dynamic' LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_all_locations(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM locations").fetchall()
            return [dict(r) for r in rows]

    # ---------- care_targets ----------
    def add_target(self, name: str, relation: str = "", location_id: Optional[int] = None,
                   user_id: str = "", is_default: int = 0, is_dynamic: int = 0) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO care_targets(name,relation,location_id,user_id,is_default,is_dynamic) "
                "VALUES(?,?,?,?,?,?)",
                (name, relation, location_id, user_id, is_default, is_dynamic),
            )
            return cur.lastrowid

    def get_target(self, target_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM care_targets WHERE id=?", (target_id,)).fetchone()
            return dict(row) if row else None

    def get_all_targets(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM care_targets ORDER BY is_default DESC, id").fetchall()
            return [dict(r) for r in rows]

    def get_default_target(self) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM care_targets WHERE is_default=1 LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def update_target_location(self, target_id: int, location_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE care_targets SET location_id=? WHERE id=?", (location_id, target_id))

    def sync_dynamic_location(self, city: str, lat: float, lon: float, region: str = "",
                              default_target_id: Optional[int] = None) -> int:
        """v1.1.6：动态定位唯一化 + 默认目标绑定同步。

        保证 locations 表最多一条 type='dynamic' 记录，且默认关怀目标始终指向最新城市：
        - 默认目标已指向某条动态记录 → 原地 UPDATE（id 稳定，weather_cache 主键不漂移），
          城市变化时清空该地点的天气缓存（旧城市天气无意义）；
        - 否则 → 删除全部孤儿动态记录，新建一条并绑定默认目标。
        返回动态地点 id。
        """
        with self._connect() as conn:
            cur = None
            if default_target_id:
                cur = conn.execute(
                    "SELECT l.id, l.name FROM locations l "
                    "JOIN care_targets t ON t.location_id=l.id "
                    "WHERE l.type='dynamic' AND t.id=? LIMIT 1",
                    (default_target_id,),
                ).fetchone()
            if cur:
                if cur["name"] != city:
                    conn.execute(
                        "UPDATE locations SET name=?, lat=?, lon=?, region=? WHERE id=?",
                        (city, lat, lon, region, cur["id"]),
                    )
                    conn.execute("DELETE FROM weather_cache WHERE location_id=?", (cur["id"],))
                # 清理孤儿动态记录（如旧版 INSERT 出的 Guangzhou 残留）+ 连带其天气缓存
                for o in conn.execute(
                    "SELECT id FROM locations WHERE type='dynamic' AND id<>?",
                    (cur["id"],),
                ).fetchall():
                    conn.execute("DELETE FROM weather_cache WHERE location_id=?", (o["id"],))
                    conn.execute("DELETE FROM locations WHERE id=?", (o["id"],))
                return cur["id"]
            # 无被引用的动态记录：清掉全部孤儿，新建并绑定
            orphan_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM locations WHERE type='dynamic'"
            ).fetchall()]
            if orphan_ids:
                conn.execute("DELETE FROM weather_cache WHERE location_id IN (%s)" % ",".join("?" * len(orphan_ids)), orphan_ids)
            conn.execute("DELETE FROM locations WHERE type='dynamic'")
            cur2 = conn.execute(
                "INSERT INTO locations(name,lat,lon,type,region) VALUES(?,?,?,?,?)",
                (city, lat, lon, "dynamic", region),
            )
            new_id = cur2.lastrowid
            if default_target_id:
                conn.execute("UPDATE care_targets SET location_id=? WHERE id=?", (new_id, default_target_id))
            return new_id

    def cleanup_orphan_dynamic_locations(self) -> int:
        """v1.1.6：升级自愈——清理未被任何 care_targets 引用的孤儿动态定位记录。

        旧版 webapi 手动定位可能 INSERT 出第二条动态记录但默认目标仍指向旧城市，
        启动时执行一次，恢复 locations 表唯一动态记录 + 清理其天气缓存。
        返回清理条数。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM locations WHERE type='dynamic' AND id NOT IN "
                "(SELECT location_id FROM care_targets WHERE location_id IS NOT NULL)"
            ).fetchall()
            for r in rows:
                conn.execute("DELETE FROM weather_cache WHERE location_id=?", (r["id"],))
                conn.execute("DELETE FROM locations WHERE id=?", (r["id"],))
            return len(rows)

    def update_target_user_id(self, target_id: int, user_id: str) -> None:
        """v1.1.6：回填默认目标的 user_id（旧版本建默认目标时未传 user_id）"""
        with self._connect() as conn:
            conn.execute("UPDATE care_targets SET user_id=? WHERE id=?", (user_id, target_id))

    # ---------- profile ----------
    def profile_set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO profile(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def profile_get(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM profile WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def profile_all(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM profile").fetchall()
            return {r["key"]: r["value"] for r in rows}

    # ---------- care_events ----------
    def add_event(self, target_id: int, source: str, summary: str, detail: str = "",
                  intensity: int = 1, priority: int = 1, ttl_hours: int = 0,
                  event_type: str = "", cause: str = "", location_id: int = 0) -> int:
        """写入关怀事件。

        去重策略（v5.8.0）：
        - 传入 event_type+cause 时：按 (target_id, type, cause) 去重——同病因
          的重复事件合并更新，不再因摘要措辞不同而爆炸。
        - 未传时（旧调用，如天气事件）：回退按 summary 完全匹配。
        """
        now = int(time.time())
        expire = now + ttl_hours * 3600 if ttl_hours > 0 else 0
        with self._connect() as conn:
            if event_type and cause:
                # v5.8.3：天气事件按 (type, cause, location_id) 去重——同一天同地点同类合并
                row = conn.execute(
                    "SELECT id FROM care_events WHERE target_id=? AND type=? AND cause=? "
                    "AND source=? AND location_id=? AND status='active'",
                    (target_id, event_type, cause, source, location_id or 0),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM care_events WHERE target_id=? AND summary=? AND status='active'",
                    (target_id, summary),
                ).fetchone()
            if row:
                conn.execute(
                    "UPDATE care_events SET detail=?, intensity=?, expire_at=?, created_at=? WHERE id=?",
                    (detail, intensity, expire, now, row["id"]),
                )
                return row["id"]
            cur = conn.execute(
                "INSERT INTO care_events(target_id,source,summary,detail,type,cause,location_id,intensity,priority,status,created_at,expire_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
                (target_id, source, summary, detail, event_type or "", cause or "",
                 location_id or 0, intensity, priority, now, expire),
            )
            return cur.lastrowid

    # ---------- 病因（v5.8.0 动态化） ----------
    def list_causes(self, event_type: str = "") -> list:
        """列出病因。event_type 为空时返回全部；否则只返回该大类下的。"""
        with self._connect() as conn:
            if event_type:
                rows = conn.execute(
                    "SELECT * FROM care_causes WHERE type=? ORDER BY id", (event_type,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM care_causes ORDER BY type, id").fetchall()
            return [dict(r) for r in rows]

    def upsert_cause(self, event_type: str, cause: str, keywords: str = "") -> int:
        """新增或更新病因（LLM 语义对不上现有 cause 时动态新增）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM care_causes WHERE type=? AND cause=?",
                (event_type, cause),
            ).fetchone()
            if row:
                if keywords:
                    conn.execute(
                        "UPDATE care_causes SET keywords=? WHERE id=?",
                        (keywords, row["id"]),
                    )
                return row["id"]
            cur = conn.execute(
                "INSERT INTO care_causes(type, cause, keywords) VALUES(?,?,?)",
                (event_type, cause, keywords),
            )
            return cur.lastrowid

    def get_active_events_by_cause(self, target_id: int, event_type: str, cause: str) -> list:
        """按 (type, cause) 查 active 状态事件（恢复兜底用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM care_events WHERE status='active' AND target_id=? "
                "AND type=? AND cause=? ORDER BY created_at DESC",
                (target_id, event_type, cause),
            ).fetchall()
            return [dict(r) for r in rows]

    def resolve_event(self, event_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE care_events SET status='resolved', resolved_at=? WHERE id=?",
                (int(time.time()), event_id),
            )

    def get_active_events(self, target_id: Optional[int] = None) -> list[dict]:
        now = int(time.time())
        with self._connect() as conn:
            if target_id is not None:
                rows = conn.execute(
                    "SELECT * FROM care_events WHERE status='active' AND target_id=? "
                    "AND (expire_at=0 OR expire_at>?) ORDER BY priority DESC, created_at DESC",
                    (target_id, now),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM care_events WHERE status='active' "
                    "AND (expire_at=0 OR expire_at>?) ORDER BY priority DESC, created_at DESC",
                    (now,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_resolved_events(self, target_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        """查询已解决/已过期事件，按解决时间倒序（供 WebUI 已解决列表）。"""
        with self._connect() as conn:
            if target_id is not None:
                rows = conn.execute(
                    "SELECT * FROM care_events WHERE status IN ('resolved','expired') AND target_id=? "
                    "ORDER BY resolved_at DESC, created_at DESC LIMIT ?",
                    (target_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM care_events WHERE status IN ('resolved','expired') "
                    "ORDER BY resolved_at DESC, created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_event(self, event_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM care_events WHERE id=?", (event_id,)).fetchone()
            return dict(row) if row else None

    # ---------- care_plans ----------
    def add_plan(self, event_id: int, target_id: int, plan_date: str, trigger_window: str,
                 task_type: str, content_summary: str) -> int:
        now = int(time.time())
        with self._connect() as conn:
            # 同事件同日期同窗口去重
            row = conn.execute(
                "SELECT id FROM care_plans WHERE event_id=? AND plan_date=? AND trigger_window=? "
                "AND status IN ('pending','sent')",
                (event_id, plan_date, trigger_window),
            ).fetchone()
            if row:
                return row["id"]
            cur = conn.execute(
                "INSERT INTO care_plans(event_id,target_id,plan_date,trigger_window,task_type,content_summary,status,created_at) "
                "VALUES(?,?,?,?,?,?,'pending',?)",
                (event_id, target_id, plan_date, trigger_window, task_type, content_summary, now),
            )
            return cur.lastrowid

    def get_due_plans(self, plan_date: str, trigger_window: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM care_plans WHERE plan_date=? AND trigger_window=? AND status='pending'",
                (plan_date, trigger_window),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pending_plans(self, plan_date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM care_plans WHERE plan_date=? AND status='pending'",
                (plan_date,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_plan(self, plan_id: int, status: str) -> None:
        with self._connect() as conn:
            if status == "sent":
                conn.execute(
                    "UPDATE care_plans SET status=?, sent_at=? WHERE id=?",
                    (status, int(time.time()), plan_id),
                )
            else:
                conn.execute("UPDATE care_plans SET status=? WHERE id=?", (status, plan_id))

    def cancel_plans_by_event(self, event_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE care_plans SET status='cancelled' WHERE event_id=? AND status='pending'",
                (event_id,),
            )

    # ---------- send_log ----------
    def add_send_log(self, target_id: int, plan_id: int, content: str, channel: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO send_log(target_id,plan_id,content,channel,sent_at) VALUES(?,?,?,?,?)",
                (target_id, plan_id, content, channel, int(time.time())),
            )

    # ---------- weather_cache ----------
    def set_weather_cache(self, location_id: int, date: str, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO weather_cache(location_id,date,payload,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(location_id,date) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (location_id, date, json.dumps(payload, ensure_ascii=False), int(time.time())),
            )

    def get_weather_cache(self, location_id: int, date: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM weather_cache WHERE location_id=? AND date=?",
                (location_id, date),
            ).fetchone()
            if not row:
                return None
            try:
                return json.loads(row["payload"])
            except Exception:
                return None

    # ---------- 决策日志 ----------
    def add_decision_log(self, source: str, decision: str, background: str = "",
                         plan_date: str = "", trigger_window: str = "",
                         reason: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO decision_log(source,decision,background,plan_date,trigger_window,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (source, decision, background, plan_date, trigger_window, reason, int(time.time())),
            )

    def get_recent_decisions(self, limit: int = 20) -> list[dict]:
        """最近的决策记录（供防重复与调试）"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_reminders(self, target_id: int, hours: int = 48) -> list[str]:
        """最近 N 小时内对该目标发送过的关怀内容（供决策 LLM 判断是否重复）"""
        since = int(time.time()) - hours * 3600
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM send_log WHERE target_id=? AND sent_at>=? "
                "ORDER BY sent_at DESC LIMIT 30",
                (target_id, since),
            ).fetchall()
            return [r["content"] for r in rows if r["content"]]

    # ---------- 消息流水（v5.7.1 补扫用） ----------
    def stream_append(self, target_id: int, contents: list) -> int:
        """把消息追加进流水表（该 target 内 seq 全局递增，按内容 hash 去重）。返回新增条数。"""
        import hashlib
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(seq) m FROM event_stream WHERE target_id=?", (target_id,)
            ).fetchone()
            seq = row["m"] or 0
            existing = set(
                r["content_hash"] for r in conn.execute(
                    "SELECT content_hash FROM event_stream WHERE target_id=?", (target_id,)
                ).fetchall()
            )
            now = int(time.time())
            added = 0
            for c in contents:
                c = str(c or "").strip()
                if not c:
                    continue
                h = hashlib.md5(c.encode("utf-8")).hexdigest()
                if h in existing:
                    continue
                seq += 1
                conn.execute(
                    "INSERT INTO event_stream(target_id,seq,content,content_hash,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (target_id, seq, c, h, now),
                )
                existing.add(h)
                added += 1
            return added

    def stream_after_seq(self, target_id: int, seq: int, limit: int = 500) -> list:
        """获取该 target 流水表中 seq 之后的消息（按 seq 升序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM event_stream WHERE target_id=? AND seq>? ORDER BY seq LIMIT ?",
                (target_id, seq, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def stream_between(self, target_id: int, lo: int, hi: int) -> list:
        """获取该 target 流水表中 seq 在 [lo, hi] 区间的消息（按 seq 升序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM event_stream WHERE target_id=? AND seq>=? AND seq<=? ORDER BY seq",
                (target_id, lo, hi),
            ).fetchall()
            return [dict(r) for r in rows]

    def stream_max_seq(self, target_id: int) -> int:
        """该 target 流水表当前最大 seq。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(seq) m FROM event_stream WHERE target_id=?", (target_id,)
            ).fetchone()
            return row["m"] or 0

    def stream_hashes(self, target_id: int) -> set:
        """该 target 流水表全部内容 hash（用于排除重复）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content_hash FROM event_stream WHERE target_id=?", (target_id,)
            ).fetchall()
            return {r["content_hash"] for r in rows}

    # ---------- 消息流水 seen 标记（v5.8.2） ----------
    def stream_mark_seen_by_hashes(self, target_id: int, hashes: set) -> int:
        """把指定 content_hash 的流水消息标记为 seen（已被 LLM 覆盖）。返回更新条数。"""
        if not hashes:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                "UPDATE event_stream SET seen=1 WHERE target_id=? AND content_hash=? AND seen=0",
                [(target_id, h) for h in hashes],
            )
            return cur.rowcount

    def stream_mark_seen_by_ids(self, target_id: int, ids: list) -> int:
        """按 id 列表标记 seen（精判块内全部消息视为已覆盖）。返回更新条数。"""
        if not ids:
            return 0
        with self._connect() as conn:
            cur = conn.executemany(
                "UPDATE event_stream SET seen=1 WHERE target_id=? AND id=? AND seen=0",
                [(target_id, i) for i in ids],
            )
            return cur.rowcount

    def stream_unseen(self, target_id: int, limit: int = 500) -> list:
        """获取该 target 流水表中 seen=0 的消息（按 seq 升序）——从未被任何 LLM 覆盖。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM event_stream WHERE target_id=? AND seen=0 ORDER BY seq LIMIT ?",
                (target_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def stream_unseen_old(self, target_id: int, before_seq: int, limit: int = 500) -> list:
        """获取 seen=0 且 seq<=before_seq 的消息（已出框架窗口、从未被覆盖）——未看型全量精判目标。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM event_stream WHERE target_id=? AND seen=0 AND seq<=? ORDER BY seq LIMIT ?",
                (target_id, before_seq, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- v5.8.3 天气事件生命周期 ----------
    def expire_stale_events(self, target_id: Optional[int] = None) -> int:
        """把所有已过 TTL 的 active 事件标为 expired，并取消关联计划。返回清理条数。"""
        now = int(time.time())
        with self._connect() as conn:
            if target_id is not None:
                rows = conn.execute(
                    "SELECT id FROM care_events WHERE status='active' AND target_id=? "
                    "AND expire_at>0 AND expire_at<=?",
                    (target_id, now),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM care_events WHERE status='active' "
                    "AND expire_at>0 AND expire_at<=?",
                    (now,),
                ).fetchall()
            ids = [r["id"] for r in rows]
            for eid in ids:
                conn.execute(
                    "UPDATE care_events SET status='expired', resolved_at=? WHERE id=?",
                    (now, eid),
                )
                conn.execute(
                    "UPDATE care_plans SET status='cancelled' WHERE event_id=? AND status='pending'",
                    (eid,),
                )
            return len(ids)

    def expire_weather_events_by_location(self, target_id: int, location_id: int) -> int:
        """把某地点（或旧地点）的 active 天气事件标为 expired——地点切换后旧天气失效。返回清理条数。"""
        now = int(time.time())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM care_events WHERE status='active' AND source='weather' "
                "AND target_id=? AND location_id=?",
                (target_id, location_id),
            ).fetchall()
            ids = [r["id"] for r in rows]
            for eid in ids:
                conn.execute(
                    "UPDATE care_events SET status='expired', resolved_at=? WHERE id=?",
                    (now, eid),
                )
                conn.execute(
                    "UPDATE care_plans SET status='cancelled' WHERE event_id=? AND status='pending'",
                    (eid,),
                )
            return len(ids)

    def expire_all_weather_events(self, target_id: int) -> int:
        """把该 target 全部 active 天气事件标为 expired（存量清理 / 全量失效）。返回清理条数。"""
        now = int(time.time())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM care_events WHERE status='active' AND source='weather' AND target_id=?",
                (target_id,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            for eid in ids:
                conn.execute(
                    "UPDATE care_events SET status='expired', resolved_at=? WHERE id=?",
                    (now, eid),
                )
                conn.execute(
                    "UPDATE care_plans SET status='cancelled' WHERE event_id=? AND status='pending'",
                    (eid,),
                )
            return len(ids)

    # ---------- 统计 ----------
    def count_send_today(self, target_id: int, date_str: str, channel: str = "") -> int:
        """统计某目标某天已发送的关怀条数（按本地日期）。
        channel 非空时只统计该来源（proactive=主动 / care=关怀 / test=测试），
        用于主动消息与关怀消息的两套独立每日上限。"""
        day_start = int(time.mktime(time.strptime(date_str, "%Y-%m-%d")))
        day_end = day_start + 86400
        with self._connect() as conn:
            if channel:
                row = conn.execute(
                    "SELECT COUNT(*) c FROM send_log WHERE target_id=? AND sent_at>=? AND sent_at<? AND channel=?",
                    (target_id, day_start, day_end, channel),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) c FROM send_log WHERE target_id=? AND sent_at>=? AND sent_at<?",
                    (target_id, day_start, day_end),
                ).fetchone()
            return row["c"] if row else 0

    def stats(self) -> dict:
        with self._connect() as conn:
            targets = conn.execute("SELECT COUNT(*) c FROM care_targets").fetchone()["c"]
            locations = conn.execute("SELECT COUNT(*) c FROM locations").fetchone()["c"]
            events = conn.execute(
                "SELECT COUNT(*) c FROM care_events WHERE status='active'"
            ).fetchone()["c"]
            plans = conn.execute(
                "SELECT COUNT(*) c FROM care_plans WHERE status='pending'"
            ).fetchone()["c"]
            sent = conn.execute("SELECT COUNT(*) c FROM send_log").fetchone()["c"]
            return {
                "targets": targets, "locations": locations,
                "active_events": events, "pending_plans": plans, "sent_logs": sent,
            }
