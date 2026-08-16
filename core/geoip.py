# -*- coding: utf-8 -*-
"""
微光-Daily Care - IP 定位
优先 ip-api.com（免费无key），失败降级 ipapi.co
"""
import asyncio
from typing import Optional

from astrbot.api import logger

from .city_map import COORD_FIX, to_english

try:
    import aiohttp
except ImportError:
    aiohttp = None


async def locate_by_ip(session=None) -> Optional[dict]:
    """返回 {city, lat, lon, region}；失败返回 None"""
    if aiohttp is None:
        # 尝试用标准库 urllib 同步定位（极简降级）
        return _locate_urllib()
    if session is None:
        session = aiohttp.ClientSession()
        close = True
    else:
        close = False
    try:
        # 优先 https 的 ipapi.co，失败再试 http 的 ip-api.com
        try:
            async with session.get(
                "https://ipapi.co/json/", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    city = data.get("city") or ""
                    lat = data.get("latitude")
                    lon = data.get("longitude")
                    region = data.get("region") or data.get("country_name") or ""
                    if city and lat is not None and lon is not None:
                        logger.info(f"[DailyCare] IP定位成功(ipapi.co): {city} ({lat},{lon})")
                        return {"city": city, "lat": float(lat), "lon": float(lon), "region": region}
        except Exception as e:
            logger.debug(f"[DailyCare] ipapi.co 定位失败: {e}")
        try:
            async with session.get(
                "http://ip-api.com/json/", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "success":
                        city = data.get("city") or ""
                        lat = data.get("lat")
                        lon = data.get("lon")
                        region = data.get("regionName") or data.get("country") or ""
                        logger.info(f"[DailyCare] IP定位成功(ip-api.com): {city} ({lat},{lon})")
                        return {"city": city, "lat": float(lat), "lon": float(lon), "region": region}
        except Exception as e:
            logger.debug(f"[DailyCare] ip-api.com 定位失败: {e}")
        return None
    finally:
        if close:
            await session.close()


def _locate_urllib() -> Optional[dict]:
    """标准库降级定位（无 aiohttp 时）"""
    try:
        import json
        import urllib.request

        req = urllib.request.Request(
            "https://ipapi.co/json/", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        city = data.get("city") or ""
        lat = data.get("latitude")
        lon = data.get("longitude")
        if city and lat is not None and lon is not None:
            return {"city": city, "lat": float(lat), "lon": float(lon), "region": data.get("region") or ""}
    except Exception as e:
        logger.debug(f"[DailyCare] urllib 定位失败: {e}")
    return None


async def geocode_city(city: str, session=None) -> Optional[dict]:
    """城市名 → 经纬度。
    1. COORD_FIX 内置坐标直接命中
    2. CITY_MAP 转英文查 Open-Meteo（countryCode=CN 过滤）
    3. 原中文名兜底
    """
    city = (city or "").strip()
    if not city:
        return None
    # 内置坐标优先
    if city in COORD_FIX:
        lat, lon = COORD_FIX[city]
        return {"city": city, "lat": lat, "lon": lon, "region": ""}
    if aiohttp is None:
        return None
    if session is None:
        session = aiohttp.ClientSession()
        close = True
    else:
        close = False
    try:
        candidates = []
        en = to_english(city)
        if en and en != city:
            candidates.append(en)
        candidates.append(city)

        for name in candidates:
            try:
                url = (
                    "https://geocoding-api.open-meteo.com/v1/search?"
                    f"name={name}&count=1&countryCode=CN&language=zh&format=json"
                )
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    results = data.get("results") or []
                    if results:
                        r = results[0]
                        return {
                            "city": r.get("name") or city,
                            "lat": float(r["latitude"]),
                            "lon": float(r["longitude"]),
                            "region": r.get("admin1") or r.get("country") or "",
                        }
            except Exception as e:
                logger.debug(f"[DailyCare] 地理编码候选 {name} 失败: {e}")
                continue
        return None
    finally:
        if close:
            await session.close()
