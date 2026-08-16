# -*- coding: utf-8 -*-
"""
微光-Daily Care - 天气工具（v5.1）
把天气注册为 bot 本人的能力（llm_tool）：用户随口问天气时，
bot 本人在对话中自然调用此工具查询，而不是插件介入。

与主动提醒互补：
- 主动提醒（插件监测 → LLM 决策 → 唤醒 bot 本人）：我主动想起，插件当闹钟
- 被动查询（用户问 → bot 本人调工具）：这是"我的能力"，对话里自然回答

实现方式：FunctionTool dataclass（参数 schema 显式声明），避免 @filter.llm_tool
装饰器从 docstring 解析 schema 的坑——格式不对参数会被静默丢弃。
数据源与插件监测共用（weather.py 统一入口 + geoip.py 定位 + qweather_key 配置），
没有第二套配置，不会出现两套数据打架。
"""
import asyncio
from typing import Optional

from astrbot.api import logger
from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from . import geoip, weather

try:
    import aiohttp
except ImportError:
    aiohttp = None


@dataclass
class WeatherTool(FunctionTool[AstrAgentContext]):
    name: str = "query_weather"
    description: str = (
        "查询指定城市的天气：当前温度、今天/明天的天气状况、降水概率、风力、紫外线指数，"
        "以及当地灾害预警。当用户询问天气、要不要带伞、冷不冷热不热时使用；"
        "location 不填时使用用户当前所在城市。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "城市名，如 北京、上海、长沙；不填则使用用户当前所在城市",
                },
                "days": {
                    "type": "string",
                    "description": "查询范围：today=今天（默认），tomorrow=明天",
                },
            },
            "required": [],
        }
    )

    # 无类型注解的类属性：普通类变量，不是 dataclass field，用于注入 db/config
    _db = None
    _config = None

    @classmethod
    def configure(cls, db, config: Optional[dict] = None) -> None:
        """由插件初始化时注入数据库与配置引用（避免工具内重复构造）。"""
        cls._db = db
        cls._config = config or {}

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        try:
            return await self._query(kwargs)
        except Exception as e:
            logger.warning(f"[DailyCare] 天气工具调用异常: {e}")
            return f"查询天气时出了点问题：{e}"

    async def _query(self, kwargs: dict) -> str:
        location = str(kwargs.get("location") or "").strip()
        days = str(kwargs.get("days") or "today").strip().lower()
        db = self._db
        config = self._config or {}
        qkey = str(config.get("qweather_key", "") or "").strip()
        tz = config.get("timezone", "Asia/Shanghai")

        # 确定坐标与城市名
        lat = lon = None
        city_name = location or ""
        if city_name:
            gc = await geoip.geocode_city(city_name)
            if gc:
                lat, lon = gc["lat"], gc["lon"]
                city_name = gc["city"]
        else:
            if db is not None:
                default = db.get_default_target()
                if default and default.get("location_id"):
                    locs = {l["id"]: l for l in db.get_all_locations()}
                    loc = locs.get(default["location_id"])
                    if loc and loc.get("lat") and loc.get("lon"):
                        lat, lon = loc["lat"], loc["lon"]
                        city_name = loc.get("name") or "你所在的城市"

        if lat is None or lon is None:
            return "无法确定查询位置，请告诉我一个城市名（如：北京、上海、长沙）。"

        if aiohttp is None:
            return "天气查询暂不可用（缺少网络库）。"

        async with aiohttp.ClientSession() as session:
            wx = await weather.fetch_weather_auto(lat, lon, qkey, session, tz)

        if not wx:
            return "天气数据获取失败，请稍后再试。"

        lines = [f"{city_name} 的天气："]
        cur = wx.get("current_temp")
        if cur is not None:
            try:
                lines.append(f"当前温度 {float(cur):.0f}℃")
            except (TypeError, ValueError):
                pass
        today = wx.get("today") or {}
        if today:
            parts = [str(today.get("desc") or "")]
            if today.get("tmax") is not None:
                parts.append(f"最高{today['tmax']:.0f}℃")
            if today.get("tmin") is not None:
                parts.append(f"最低{today['tmin']:.0f}℃")
            if today.get("precip_prob") is not None:
                parts.append(f"降水概率{today['precip_prob']:.0f}%")
            if today.get("wind_max") is not None:
                parts.append(f"风力{today['wind_max']:.0f}km/h")
            if today.get("uv_max") is not None:
                parts.append(f"紫外线{today['uv_max']:.0f}")
            lines.append("今天：" + "，".join(p for p in parts if p))
        if days in ("tomorrow", "明天"):
            tomorrow = wx.get("tomorrow")
            if tomorrow:
                tparts = [str(tomorrow.get("desc") or "")]
                if tomorrow.get("tmax") is not None:
                    tparts.append(f"最高{tomorrow['tmax']:.0f}℃")
                if tomorrow.get("precip_prob") is not None:
                    tparts.append(f"降水概率{tomorrow['precip_prob']:.0f}%")
                lines.append("明天：" + "，".join(p for p in tparts if p))
        alerts = wx.get("alerts") or []
        if alerts:
            for a in alerts[:3]:
                title = a.get("title") or ""
                level = a.get("level") or ""
                lines.append(f"⚠ {title}（{level}预警）")
        return "\n".join(lines)
