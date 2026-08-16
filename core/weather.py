# -*- coding: utf-8 -*-
"""
微光-Daily Care - 天气模块
Open-Meteo 免费 API：当前天气 + 3日预报
WMO 天气代码 → 中文描述；阈值判断 → 关怀事件
"""
import asyncio
import json
from typing import Optional

from astrbot.api import logger

try:
    import aiohttp
except ImportError:
    aiohttp = None

# WMO 天气代码映射
WMO_CODES = {
    0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}


def describe_weather(code: int) -> str:
    return WMO_CODES.get(code, f"天气代码{code}")


def _is_precip(code: int) -> bool:
    return code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99)


def _is_snow(code: int) -> bool:
    return code in (71, 73, 75, 77, 85, 86)


def _is_storm(code: int) -> bool:
    return code in (95, 96, 99)


async def fetch_weather(lat: float, lon: float, session=None, timezone: str = "Asia/Shanghai") -> Optional[dict]:
    """
    获取天气。返回结构：
    {
      "today": {"date","weather_code","desc","tmax","tmin","precip_prob","precip_sum","wind_max","uv_max"},
      "tomorrow": {...},
      "alerts": [...]
    }
    """
    if aiohttp is None:
        return None
    if session is None:
        session = aiohttp.ClientSession()
        close = True
    else:
        close = False
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code,relative_humidity_2m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max,precipitation_sum,"
                     "wind_speed_10m_max,uv_index_max",
            "timezone": timezone,
            "forecast_days": 3,
        }
        url = "https://api.open-meteo.com/v1/forecast?" + "&".join(
            f"{k}={v}" for k, v in params.items()
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"[DailyCare] 天气API返回 {resp.status}")
                return None
            data = await resp.json()

        daily = data.get("daily") or {}
        times = daily.get("time") or []
        codes = daily.get("weather_code") or []
        tmaxs = daily.get("temperature_2m_max") or []
        tmins = daily.get("temperature_2m_min") or []
        probs = daily.get("precipitation_probability_max") or []
        sums = daily.get("precipitation_sum") or []
        winds = daily.get("wind_speed_10m_max") or []
        uvs = daily.get("uv_index_max") or []

        days = []
        for i in range(min(len(times), 3)):
            code = int(codes[i]) if i < len(codes) else 0
            days.append({
                "date": times[i],
                "weather_code": code,
                "desc": describe_weather(code),
                "tmax": tmaxs[i] if i < len(tmaxs) else None,
                "tmin": tmins[i] if i < len(tmins) else None,
                "precip_prob": probs[i] if i < len(probs) else None,
                "precip_sum": sums[i] if i < len(sums) else None,
                "wind_max": winds[i] if i < len(winds) else None,
                "uv_max": uvs[i] if i < len(uvs) else None,
            })

        current = data.get("current") or {}
        result = {
            "today": days[0] if days else None,
            "tomorrow": days[1] if len(days) > 1 else None,
            "current_temp": current.get("temperature_2m"),
            "current_humidity": current.get("relative_humidity_2m"),
        }
        return result
    except Exception as e:
        logger.warning(f"[DailyCare] 获取天气失败: {e}")
        return None
    finally:
        if close:
            await session.close()


def analyze_weather(weather: dict, thresholds: Optional[dict] = None) -> list[dict]:
    """
    分析天气数据，返回关怀事件列表：
    [{"summary","detail","intensity","priority","ttl_hours"}, ...]
    """
    if not weather:
        return []
    t = thresholds or {}
    heat_threshold = float(t.get("heat", 35))
    cold_threshold = float(t.get("cold", 0))
    precip_threshold = int(t.get("precip_prob", 60))
    rain_threshold = float(t.get("rain", 0.5))
    wind_threshold = float(t.get("wind", 40))
    uv_threshold = float(t.get("uv", 7))

    events = []
    today = weather.get("today")
    tomorrow = weather.get("tomorrow")
    if not today:
        return events

    # 今日高温
    if today["tmax"] is not None and today["tmax"] >= heat_threshold:
        events.append({
            "summary": f"今天{today['desc']}，最高温{today['tmax']:.0f}℃",
            "detail": f"高温{today['tmax']:.0f}℃，注意防暑补水，避免长时间暴晒",
            "intensity": 3 if today["tmax"] >= heat_threshold + 3 else 2,
            "priority": 3,
            "ttl_hours": 24,
        })
    # 今日低温
    if today["tmin"] is not None and today["tmin"] <= cold_threshold:
        events.append({
            "summary": f"今天最低温{today['tmin']:.0f}℃，注意保暖",
            "detail": f"低温{today['tmin']:.0f}℃，出门多穿点，小心感冒",
            "intensity": 3 if today["tmin"] <= cold_threshold - 3 else 2,
            "priority": 3,
            "ttl_hours": 24,
        })
    # 今日降水
    if _is_precip(today["weather_code"]) or (today["precip_prob"] or 0) >= precip_threshold:
        prob = today["precip_prob"]
        kind = "雪" if _is_snow(today["weather_code"]) else "雨"
        level = "雷阵雨" if _is_storm(today["weather_code"]) else today["desc"]
        # 降水量级判断（兼容和风无降水概率的情况）
        code = today["weather_code"]
        heavy = (code in (63, 65, 66, 67, 75, 77, 80, 81, 82, 95, 96, 99)
                 or (prob or 0) >= 80)
        if prob is not None:
            summary = f"今天有{level}，降水概率{prob:.0f}%"
        else:
            summary = f"今天有{level}"
        if heavy:
            summary += "，记得带伞"
        # 强度：暴雨/雷暴/高概率=4，中雨以上=3，小雨=2
        if _is_storm(today["weather_code"]) or (prob or 0) >= 80:
            intens = 4
        elif heavy:
            intens = 3
        else:
            intens = 2
        events.append({
            "summary": summary,
            "detail": f"今天{level}，记得带伞；若外出注意{kind}天路滑",
            "intensity": intens,
            "priority": 4 if _is_storm(today["weather_code"]) else 3,
            "ttl_hours": 24,
        })
    # 明日降水（提前提醒）
    if tomorrow and (_is_precip(tomorrow["weather_code"]) or (tomorrow["precip_prob"] or 0) >= precip_threshold):
        prob = tomorrow["precip_prob"] or 0
        level = tomorrow["desc"]
        events.append({
            "summary": f"明天有{level}，降水概率{prob}%",
            "detail": f"明天{level}，可以提前准备雨具",
            "intensity": 2,
            "priority": 2,
            "ttl_hours": 48,
        })
    # 大风
    if today["wind_max"] is not None and today["wind_max"] >= wind_threshold:
        events.append({
            "summary": f"今天风力较大，最大{today['wind_max']:.0f}km/h",
            "detail": "大风天气，注意高空坠物，骑行注意安全",
            "intensity": 2,
            "priority": 2,
            "ttl_hours": 24,
        })
    # 强紫外线
    if today["uv_max"] is not None and today["uv_max"] >= uv_threshold:
        events.append({
            "summary": f"今天紫外线强度高（{today['uv_max']:.0f}）",
            "detail": "紫外线强，外出注意防晒",
            "intensity": 1,
            "priority": 1,
            "ttl_hours": 24,
        })
    return events


async def main_test():
    """独立测试入口"""
    w = await fetch_weather(28.23, 112.94)  # 长沙
    if w:
        print(json.dumps(w, ensure_ascii=False, indent=2))
        evs = analyze_weather(w)
        for e in evs:
            print("→", e["summary"], "| intensity:", e["intensity"])
    else:
        print("天气获取失败")


# ============================================================
# 和风天气（QWeather）数据源
# 国内用户可配置 qweather_key，自动切换为国内数据源
# ============================================================

# 和风 icon 代码 → WMO 天气代码（归一化，保证 analyze_weather 判断一致）
QW_TO_WMO = {
    "100": 0, "101": 1, "102": 2, "103": 1, "104": 3,
    "150": 0, "151": 1, "152": 2, "153": 1, "154": 3,
    "300": 80, "301": 82, "302": 95, "303": 96, "304": 67,
    "305": 61, "306": 63, "307": 65, "308": 82, "309": 51,
    "310": 65, "311": 65, "312": 65, "313": 66, "314": 61,
    "315": 63, "316": 65, "317": 65, "318": 65, "399": 61,
    "400": 71, "401": 73, "402": 75, "403": 75, "404": 67,
    "405": 67, "406": 66, "407": 85, "408": 71, "409": 73,
    "410": 75, "499": 71,
    "500": 45, "501": 45, "502": 45, "503": 45, "504": 45,
    "507": 45, "508": 45, "509": 45, "510": 45, "511": 45,
    "512": 45, "513": 45, "514": 45, "515": 45,
}


async def fetch_weather_qweather(lat: float, lon: float, api_key: str, session=None,
                                 timezone: str = "Asia/Shanghai") -> Optional[dict]:
    """
    和风天气 3 日预报。返回与 Open-Meteo 相同结构的 dict（额外含 alerts 预警）。
    和风接口 location 参数为 "经度,纬度"。
    """
    if not api_key or aiohttp is None:
        return None
    if session is None:
        session = aiohttp.ClientSession()
        close = True
    else:
        close = False
    try:
        loc_str = f"{lon},{lat}"
        url = ("https://devapi.qweather.com/v7/weather/3d?"
               f"location={loc_str}&key={api_key}&lang=zh")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning(f"[DailyCare] 和风天气API返回 {resp.status}")
                return None
            data = await resp.json()

        if data.get("code") != "200":
            logger.warning(f"[DailyCare] 和风天气错误: code={data.get('code')} {data.get('msg') or data.get('message') or ''}")
            return None

        daily = data.get("daily") or []
        days = []
        for i in range(min(len(daily), 3)):
            d = daily[i]
            icon = str(d.get("iconDay", ""))
            wmo = QW_TO_WMO.get(icon, 0)
            desc = d.get("textDay") or describe_weather(wmo)
            def _num(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            days.append({
                "date": d.get("fxDate", ""),
                "weather_code": wmo,
                "desc": desc,
                "tmax": _num(d.get("tempMax")),
                "tmin": _num(d.get("tempMin")),
                "precip_prob": None,  # 和风 3d 接口无降水概率，靠天气代码判断
                "precip_sum": _num(d.get("precip")),
                "wind_max": _num(d.get("windSpeedDay")),
                "uv_max": _num(d.get("uvIndex")),
            })

        result = {
            "today": days[0] if days else None,
            "tomorrow": days[1] if len(days) > 1 else None,
            "current_temp": None,
            "current_humidity": None,
            "source": "qweather",
        }
        # 同时拉取灾害预警
        try:
            warn_url = ("https://devapi.qweather.com/v7/warning/now?"
                        f"location={loc_str}&key={api_key}&lang=zh")
            async with session.get(warn_url, timeout=aiohttp.ClientTimeout(total=15)) as wresp:
                if wresp.status == 200:
                    wdata = await wresp.json()
                    if wdata.get("code") == "200":
                        result["alerts"] = wdata.get("warning") or []
        except Exception as e:
            logger.debug(f"[DailyCare] 和风预警获取失败: {e}")
        return result
    except Exception as e:
        logger.warning(f"[DailyCare] 和风天气获取失败: {e}")
        return None
    finally:
        if close:
            await session.close()


def analyze_alerts(alerts: list) -> list[dict]:
    """
    解析和风灾害预警为关怀事件。
    alert: {"title","type","typeName","level","text","pubTime",...}
    """
    if not alerts:
        return []
    events = []
    level_map = {"蓝色": 2, "黄色": 3, "橙色": 4, "红色": 5}
    for a in alerts:
        title = a.get("title") or ""
        type_name = a.get("typeName") or a.get("type") or ""
        level = a.get("level") or ""
        text = a.get("text") or ""
        intensity = level_map.get(level, 2)
        # 台风/暴雨红色最高优先级
        if ("台风" in type_name or "暴雨" in type_name) and level == "红色":
            intensity = 5
        summary = title or f"{type_name}{level}预警"
        events.append({
            "summary": summary,
            "detail": f"{type_name}{level}预警：{(text or title)[:120]}",
            "intensity": intensity,
            "priority": intensity,
            "ttl_hours": 48,
        })
    return events


async def fetch_weather_auto(lat: float, lon: float, api_key: str = "", session=None,
                             timezone: str = "Asia/Shanghai") -> Optional[dict]:
    """统一入口：配置了和风 key 则用和风，否则用 Open-Meteo。"""
    if api_key and api_key.strip():
        return await fetch_weather_qweather(lat, lon, api_key.strip(), session, timezone)
    return await fetch_weather(lat, lon, session, timezone)


if __name__ == "__main__":
    asyncio.run(main_test())
