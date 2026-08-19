# -*- coding: utf-8 -*-
"""
微光-Daily Care - IP 定位
优先 pconline（国内权威库，对中国移动等运营商 IP 段归属准确，免费无 key，GBK 编码），
失败降级 ip-api.com（免费无 key）。
"""
import asyncio
import json
from typing import Optional

from astrbot.api import logger

from .city_map import COORD_FIX, to_english

try:
    import aiohttp
except ImportError:
    aiohttp = None


def _clean_city(city: str) -> str:
    """pconline 返回的城市名清洗：去掉「市」等行政区后缀（成都 市 → 成都）"""
    city = (city or "").strip()
    for suffix in ("市", "地区", "盟", "自治州"):
        if city.endswith(suffix):
            city = city[: -len(suffix)]
    return city


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
        # 优先 pconline（国内，免费无需 key，返回 GBK 编码 JSON）
        try:
            async with session.get(
                "https://whois.pconline.com.cn/ipJson.jsp?json=true",
                timeout=aiohttp.ClientTimeout(total=10),
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Referer": "https://whois.pconline.com.cn/",
                },
            ) as resp:
                if resp.status == 200:
                    raw = await resp.read()
                    data = json.loads(raw.decode("gbk", errors="ignore"))
                    city = _clean_city(data.get("city") or "")
                    region = data.get("pro") or data.get("addr") or ""
                    if city:
                        gc = await geocode_city(city, session)
                        if gc:
                            lat, lon = gc["lat"], gc["lon"]
                            logger.info(f"[DailyCare] IP定位成功(pconline): {city} ({lat},{lon})")
                            return {"city": city, "lat": float(lat), "lon": float(lon), "region": region}
        except Exception as e:
            logger.debug(f"[DailyCare] pconline 定位失败: {e}")
        # 兜底 ip-api.com（国外库）
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
        import urllib.request

        req = urllib.request.Request(
            "https://whois.pconline.com.cn/ipJson.jsp?json=true",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("gbk", errors="ignore"))
        city = _clean_city(data.get("city") or "")
        region = data.get("pro") or data.get("addr") or ""
        if city:
            gc = geocode_city_sync(city)
            if gc:
                return {"city": city, "lat": float(gc["lat"]), "lon": float(gc["lon"]), "region": region}
    except Exception as e:
        logger.debug(f"[DailyCare] urllib 定位失败: {e}")
    return None


def geocode_city_sync(city: str) -> Optional[dict]:
    """同步版城市坐标（仅内置表，供 urllib 降级用）"""
    city = (city or "").strip()
    if city in COORD_FIX:
        lat, lon = COORD_FIX[city]
        return {"city": city, "lat": lat, "lon": lon, "region": ""}
    return None


def _city_candidates(city: str) -> list:
    """生成城市名候选（支持『省 市 区县』多级路径，从精确到模糊逐级回退）。

    例：『四川省 成都市 武侯区』→ [四川省 成都市 武侯区, 四川省成都市武侯区,
    成都市武侯区, 武侯区, 成都市, 成都]
    例：『北京市 市辖区 海淀区』→ […, 市辖区海淀区, 海淀区, 北京市, 北京]
    """
    import re

    city = (city or "").strip()
    if not city:
        return []
    parts = [p for p in re.split(r"[\s]+", city) if p]
    cands = []
    cands.append(city)
    joined = "".join(parts)
    if joined != city:
        cands.append(joined)
    # 逐级去掉前缀（省→市→区县）
    for i in range(1, len(parts)):
        cands.append("".join(parts[i:]))
    if any(p in ("市辖区", "县") for p in parts):
        # 直辖市结构：真正的城市名是省名
        prov = parts[0]
        cands.append(prov)
        if prov.endswith("市"):
            cands.append(prov[:-1])
    elif len(parts) >= 2:
        city_part = parts[-2]
        cands.append(city_part)
        if city_part.endswith("市"):
            cands.append(city_part[:-1])
    cands.append(parts[-1])
    # 统一：市级名去「市」字（单级成都→成都，北京→北京）
    last = parts[-1]
    if last.endswith("市"):
        cands.append(last[:-1])
    seen, out = set(), []
    for c in cands:
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def geocode_city(city: str, session=None) -> Optional[dict]:
    """城市名 → 经纬度。
    1. COORD_FIX 内置坐标直接命中
    2. CITY_MAP 转英文查 Open-Meteo（countryCode=CN 过滤）
    3. 多级城市名（省 市 区县）逐级回退
    """
    city = (city or "").strip()
    if not city:
        return None
    if aiohttp is None:
        for name in _city_candidates(city):
            if name in COORD_FIX:
                lat, lon = COORD_FIX[name]
                return {"city": name, "lat": lat, "lon": lon, "region": ""}
        return None
    if session is None:
        session = aiohttp.ClientSession()
        close = True
    else:
        close = False
    try:
        for name in _city_candidates(city):
            # 内置坐标
            if name in COORD_FIX:
                lat, lon = COORD_FIX[name]
                return {"city": name, "lat": lat, "lon": lon, "region": ""}
            candidates = []
            en = to_english(name)
            if en and en != name:
                candidates.append(en)
            candidates.append(name)
            for c in candidates:
                try:
                    url = (
                        "https://geocoding-api.open-meteo.com/v1/search?"
                        f"name={c}&count=1&countryCode=CN&language=zh&format=json"
                    )
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        results = data.get("results") or []
                        if results:
                            r = results[0]
                            return {
                                "city": r.get("name") or name,
                                "lat": float(r["latitude"]),
                                "lon": float(r["longitude"]),
                                "region": r.get("admin1") or r.get("country") or "",
                            }
                except Exception as e:
                    logger.debug(f"[DailyCare] 地理编码候选 {c} 失败: {e}")
                    continue
        return None
    finally:
        if close:
            await session.close()
