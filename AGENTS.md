# 微光-Daily Care · 开发者文档

面向插件开发者/贡献者的开发说明。用户使用文档见 README.md，调试记录见内部开发日志。

## 目录结构

```
main.py                 插件入口：@register、任务循环（天气/反思/决策/探测/计划）、
                        LLM 请求注入、channel 分轨与硬提醒
metadata.yaml           插件元信息（名称/版本/描述）
_conf_schema.json       配置 schema（WebUI 渲染 + 校验的唯一来源）
core/
  database.py           SQLite 数据层：事件/病因/计划/流水/天气缓存/kv
  monitor.py            感知层：天气检查（显著变化检测 + 常态天气提示）、冷场感知
  reflection.py         理解层：状态反思（ChatReflector）、天气判断（WeatherJudge）、窗口外补扫
  decision.py           决策层：act/plan/silent 决策、category 分轨、保底约束
  executor.py           执行层：唤醒 bot 本人、计划执行、发送日志、沉默类互斥
  wake.py               唤醒通道：AstrBot 事件注入（recent_topic 可开关）
  rest.py               休息窗口：勿扰判定单点（in_quiet）+ 晚安识别（关键词+LLM兜底）
  weather.py            天气数据获取（Open-Meteo / 和风）+ 预警解析
  weather_tool.py       天气查询工具（bot 对话中可自然调用）
  geoip.py              IP 定位
  city_map.py           城市中文名映射
  webapi.py             WebUI API
pages/关怀面板/           WebUI 前端（白色淡紫主题）
tests/test_core.py      单元测试（不依赖 astrbot 运行时）
```

## 架构原则

- **三层结构**：监测层（眼睛）→ 理解层（大脑）→ 执行层（嘴）。
- **开口永远是 bot 本人**：插件只负责感知、反思、决策，最终通过事件注入唤醒 bot，由 bot 依据自身人格与记忆开口，消息写入真实历史。插件不直接生成话术、不直接发送。
- **动态化 + 成本控制**：在成本可控的前提下多用 LLM；算法（关键词粗筛、规则层变化检测）只做兜底和成本控制，结论永远由 LLM 下。
- **事件生命周期**：写入按 (type, cause) 归因去重 → 恢复按病因精确关闭 → 天气按日期维度合并 → 过期自动清理 → 地点切换失效。信息无孤儿（seen 方案）、事件无堆积、恢复不误伤。

## 开发约定

- 修改配置项必须同步 `_conf_schema.json`（WebUI 唯一来源），hint ≤ 25 字。
- 新增事件类型/病因时，检查 care_causes 种子是否需要补充（UPSERT 自动同步）。
- 新增每日计数类逻辑，优先复用 send_log + count_send_today 的 channel 分轨。
- 新增功能必须在 `tests/test_core.py` 补测试（不依赖 astrbot 运行时，可独立跑）。
- 勿扰/休息判定一律走 `core/rest.py` 的 `in_quiet()`，禁止各处重复实现时间判定。
- 沉默类板块（proactive/care）新增发送路径时，需经过 `_mutex_blocked` 互斥检查（天气豁免）。

## 测试

```bash
python3 tests/test_core.py
```

## 发布约定

- 发布版本号采用语义化版本（metadata + @register 两处一致）：bug 修复升补丁位（如 1.1.2 → 1.1.3），新功能升次版本位。README 只放用户功能说明，更新日志/技术调整一律归档到内部开发日志，不进 README。
- 发布前五查：编译 / 测试全绿 / schema 对齐 / hint ≤25字 / 个性化残留零。
- 每次发布生成纯净安装包（不含 data/、备份、__pycache__），删除旧包，替换新包。
- 许可证：MIT。

## 数据流速查

```
天气: IP定位 → 和风/Open-Meteo → 规则层显著变化检测 → (LLM判断) → weather事件
      → 硬提醒(预警/显著变化,不受上限) 或 决策层(常态提示/普通关怀)
状态: 对话监听 → 流水表 → 反思(LLM提炼) → state事件(按cause归因) → 恢复信号按cause精确关闭
开口: 决策(act/plan/silent) → 事件注入唤醒 → bot本人 → 写入真实历史
计数: proactive / weather / care / weather_alert 各自独立，互不挤占
安静: 勿扰时段 OR 休息窗口(晚安识别) → in_quiet 单点判定；用户发消息打破休息窗口
互斥: proactive ⇄ care 窗口内只一类开口（silence_exclude_window_min）；weather 豁免
```
