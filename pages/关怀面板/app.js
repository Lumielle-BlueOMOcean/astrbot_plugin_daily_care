// 微光-Daily Care · 前端 v5
// API 端点（相对于 /api/plug/astrbot_plugin_daily_care/ 的路径）
const API = {
  overview: "page/overview",
  targets: "page/targets",
  addTarget: "page/targets/add",
  deleteTarget: "page/targets/delete",
  events: "page/events",
  resolvedEvents: "page/events/resolved",
  resolveEvent: "page/events/resolve",
  plans: "page/plans",
  cancelPlan: "page/plans/cancel",
  sends: "page/sends",
  locations: "page/locations",
  settings: "page/settings",
  decisions: "page/decisions",
  actWeather: "page/actions/weather",
  actReflect: "page/actions/reflect",
  actSend: "page/actions/send",
  actDecision: "page/actions/decision",
  actLocate: "page/actions/locate",
};

// 等待官方 bridge SDK 就绪（父页面注入，iframe 内唯一可靠的鉴权通道）
const bridgeReady = new Promise((resolve) => {
  const check = () => {
    if (window.AstrBotPluginPage && typeof window.AstrBotPluginPage.apiGet === "function") {
      resolve(window.AstrBotPluginPage);
    } else {
      setTimeout(check, 50);
    }
  };
  check();
});

async function api(endpoint, method = "GET", body = null) {
  // 主路径：官方 bridge（自动携带父页面鉴权）
  try {
    const bridge = await Promise.race([
      bridgeReady,
      new Promise((_, rej) => setTimeout(() => rej(new Error("bridge timeout")), 3000)),
    ]);
    const data = method === "GET"
      ? await bridge.apiGet(endpoint)
      : await bridge.apiPost(endpoint, body || {});
    // bridge 已解包 data，直接返回
    return { code: 0, status: "ok", data };
  } catch (e) {
    // 兜底：直接 fetch（仅当 bridge 不可用时）
    try {
      const opt = { method, headers: {} };
      if (body) {
        opt.headers["Content-Type"] = "application/json";
        opt.body = JSON.stringify(body);
      }
      const resp = await fetch(`/api/plug/astrbot_plugin_daily_care/${endpoint}`, opt);
      let j;
      try { j = await resp.json(); } catch (_) {
        throw new Error("服务端返回异常（HTTP " + resp.status + "），请查看 AstrBot 日志");
      }
      return j.code === 0 ? j : { code: j.code || 1, status: "error", message: j.message || "请求失败", data: j.data };
    } catch (e2) {
      throw new Error((e2?.message || "网络错误") + "（bridge 与兜底均失败）");
    }
  }
}

// ---------- 分区切换 ----------
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const sec = btn.dataset.section;
    document.querySelectorAll(".section").forEach((s) => s.classList.remove("active"));
    document.getElementById("sec-" + sec).classList.add("active");
  });
});

// ---------- 设置表单 ----------
const FORM_META = {
  weather: [
    ["weather_tool_enabled", "天气工具(对话中可用)", true, "对话中可查询天气"],
    ["weather_check_interval", "天气轮询间隔(分钟)", "30", "天气轮询间隔"],
    ["enable_weather_judge", "启用天气 LLM 判断", true, "启用天气LLM判断"],
    ["weather_cooldown_hours", "同类提醒冷却(小时)", "6", "同类天气提醒冷却"],
    ["qweather_key", "和风天气 Key(可选)", "", "和风天气Key（可选）"],
    ["enable_qweather_alerts", "启用和风预警窗口", true, "启用灾害预警"],
    ["enable_daily_weather_note", "常态天气提示", false, "无特殊天气也每天问候一次天气"],
    ["daily_weather_note_limit", "常态提示次数/天", "1", "每日常态天气提示次数上限"],
    ["daily_weather_note_gap_min", "常态提示间隔(分钟)", "60", "两次常态提示的最小间隔"],
    ["daily_weather_note_window", "常态提示时段", '["morning"]', "可多选预设时段，也可自定义 HH:MM-HH:MM", "window-multi"],
  ],
  decision: [
    ["decision_llm_id", "决策 LLM(可选)", "", "独立决策LLM；留空跟随会话"],
    ["decision_interval", "决策循环间隔(分钟)", "25", "决策循环间隔"],
  ],
  state: [
    ["enable_chat_monitor", "启用对话状态反思", true, "从聊天记录提炼关怀信号"],
    ["chat_reflect_interval", "状态反思间隔(分钟)", "60", "状态反思间隔"],
    ["care_level", "关怀积极度(1-10)", 5, "越高越愿意主动开口", "range"],
    ["care_daily_limit", "关怀消息每日上限(次)", "2", "每日关怀消息上限"],
    ["care_cooldown_minutes", "开口冷却(分钟)", "240", "两次开口最小间隔"],
  ],
  proactive: [
    ["enable_proactive", "启用主动消息", true, "静默后主动开启话题"],
    ["probe_min_silence_min", "最小静默(分钟)", "180", "静默多久才有资格主动"],
    ["probe_max_silence_min", "最长等待(分钟)", "600", "最多等多久必开口一次"],
    ["probe_daily_limit", "每日主动上限(次)", "2", "每日主动消息上限"],
    ["probe_interval", "主动检测间隔(分钟)", "10", "检测轮询间隔"],
  ],
  global: [
    ["dnd_start", "勿扰开始", "23:00", "勿扰开始时间", "time"],
    ["dnd_end", "勿扰结束", "08:00", "勿扰结束时间", "time"],
    ["timezone", "时区", "Asia/Shanghai", "天气接口时区"],
    ["platform_id", "平台实例ID", "auto", "auto 自动解析；多实例时手动指定"],
  ],
  targets: [
    ["target_user_id", "我的QQ", "", "默认关怀对象；留空则收到首条消息后自动捕获"],
    ["target_city", "我的城市", "", "优先使用；留空则自动IP定位"],
  ],
};

function buildForm(containerId, meta, values) {
  const wrap = document.getElementById(containerId);
  wrap.querySelectorAll(".form-item").forEach((el) => el.remove());
  meta.forEach(([key, label, def, hint, type]) => {
    const val = values[key] !== undefined ? values[key] : def;
    const isBool = typeof def === "boolean" || val === true || val === false;
    const div = document.createElement("div");
    div.className = "form-item";
    let input;
    if (isBool) {
      input = `<label class="switch"><input type="checkbox" data-key="${key}" ${val ? "checked" : ""} /><span class="slider"></span></label>`;
    } else if (type === "range") {
      input = `<div class="range-wrap"><input type="range" min="1" max="10" step="1" data-key="${key}" value="${esc(String(val))}" /><span class="range-val" data-range="${key}">${esc(String(val))}</span></div>`;
    } else if (type === "time") {
      input = `<input type="time" data-key="${key}" value="${esc(String(val))}" />`;
    } else if (type === "window-multi") {
      const wins = parseWindows(val);
      const presets = [
        ["morning", "早上 8-11"],
        ["noon", "中午 11-14"],
        ["evening", "傍晚 17-20"],
        ["night", "晚上 20-23"],
      ];
      const presetBoxes = presets.map(([w, label]) =>
        `<label class="win-chk"><input type="checkbox" data-key="${key}" data-win="${w}" ${wins.indexOf(w) >= 0 ? "checked" : ""} />${label}</label>`
      ).join("");
      const customs = wins.filter((w) => !["morning", "noon", "evening", "night"].includes(w));
      const customTags = customs.map((w) =>
        `<span class="win-tag" data-key="${key}" data-custom-win="${w}">${esc(w)} <b data-del-win="${w}">×</b></span>`
      ).join("");
      input = `<div class="win-multi" data-key="${key}" data-win-multi="1">
        <div class="win-presets">${presetBoxes}</div>
        <div class="win-custom">${customTags}<span class="muted" data-placeholder="${key}" style="${customs.length ? "display:none" : ""}">暂无自定义时段</span></div>
        <div class="win-add"><input type="text" data-add-win="${key}" placeholder="HH:MM-HH:MM，如 07:00-08:00" /><button type="button" class="btn btn-ghost btn-sm" data-add-btn="${key}">添加</button></div>
      </div>`;
    } else if (key === "target_city") {
      // v1.1.6：城市级联下拉（省 市 区县），替代裸输入框；旧值如「成都」自动反查回省市区
      const [curProv, curCity, curDist] = resolveCityPath(String(val || ""));
      const provs = Object.keys(AREA["中国"] || {});
      const cities = curProv ? Object.keys((AREA["中国"][curProv] || {})) : [];
      const dists = (curProv && curCity) ? ((AREA["中国"][curProv] || {})[curCity] || []) : [];
      const opt = (list, cur) => ["", ...list].map((x) => `<option value="${x}" ${x === cur ? "selected" : ""}>${x || "请选择"}</option>`).join("");
      input = `<div class="area-cascade" data-city-cascade="1">
        <select data-casc-prov style="flex:0 0 30%">${opt(provs, curProv)}</select>
        <select data-casc-city style="flex:0 0 30%">${opt(cities, curCity)}</select>
        <select data-casc-dist>${opt(dists, curDist)}</select>
        <input type="hidden" data-key="target_city" value="${esc(String(val || ""))}" />
      </div>`;
    } else {
      input = `<input type="text" data-key="${key}" value="${esc(String(val))}" />`;
    }
    // v1.1.6：关键配置空值提示（防静默失效）
    let warn = "";
    if (key === "target_user_id" && !String(val || "").trim()) {
      warn = `<p class="form-warn">⚠️ 未手动设置：收到你的首条消息后将自动捕获并锁定该 QQ</p>`;
    } else if (key === "platform_id" && !String(val || "").trim()) {
      warn = `<p class="form-warn">⚠️ 未设置平台实例：将尝试自动解析；若解析失败可能无法开口，建议填 auto</p>`;
    }
    div.innerHTML = `<label class="form-label">${label}</label>${input}${hint ? `<p class="form-hint">${hint}</p>` : ""}${warn}`;
    wrap.appendChild(div);
  });
  bindCityCascade(wrap);
}

// v1.1.6：把城市值解析为 [省, 市, 区县]；旧值如「成都」「武侯区」自动反查所属省市区
function resolveCityPath(val) {
  const parts = String(val || "").split(/\s+/).filter(Boolean);
  const provs = Object.keys(AREA["中国"] || {});
  const norm = (s) => String(s || "").replace(/市$|县$|区$|自治州$|地区$|盟$/, "");
  let prov = "", city = "", dist = "";
  if (!parts.length) return ["", "", ""];
  let rest = parts.slice();
  if (provs.includes(rest[0])) { prov = rest.shift(); }
  const scope = prov ? [prov] : provs;
  const findCity = (name) => {
    for (const p of scope) {
      const cObj = AREA["中国"][p] || {};
      for (const c of Object.keys(cObj)) {
        if (c === name || norm(c) === norm(name)) return [p, c];
      }
    }
    return null;
  };
  if (rest.length) {
    const hit = findCity(rest[0]);
    if (hit) { prov = hit[0]; city = hit[1]; rest.shift(); }
  }
  if (rest.length) {
    const cObj = (prov && city) ? ((AREA["中国"][prov] || {})[city] || []) : [];
    if (cObj.includes(rest[0])) { dist = rest[0]; rest.shift(); }
  }
  // 兜底：只有单个名字且没匹配到市——尝试区县级反查（如「武侯区」）
  if (!city && parts.length === 1) {
    const name = parts[0];
    for (const p of provs) {
      const cObj = AREA["中国"][p] || {};
      for (const c of Object.keys(cObj)) {
        if ((cObj[c] || []).includes(name)) { prov = p; city = c; dist = name; break; }
      }
      if (city) break;
    }
  }
  return [prov, city, dist];
}

// v1.1.6：target_city 级联下拉联动（省→市→区县），同步值到隐藏 data-key 输入框
function syncCityValue(box) {
  const prov = box.querySelector("[data-casc-prov]").value;
  const city = box.querySelector("[data-casc-city]").value;
  const dist = box.querySelector("[data-casc-dist]").value;
  const parts = [];
  if (prov) parts.push(prov);
  if (city && city !== "市辖区" && city !== "县") parts.push(city);
  if (dist) parts.push(dist);
  const input = box.querySelector("[data-key]");
  if (input) input.value = parts.join(" ");
}
function bindCityCascade(root) {
  root.querySelectorAll("[data-casc-prov]").forEach((sel) => {
    sel.addEventListener("change", () => {
      const box = sel.closest(".area-cascade");
      const prov = sel.value;
      const citySel = box.querySelector("[data-casc-city]");
      const distSel = box.querySelector("[data-casc-dist]");
      const cities = prov ? Object.keys(AREA["中国"][prov] || {}) : [];
      citySel.innerHTML = ["", ...cities].map((c) => `<option value="${c}">${c || "请选择"}</option>`).join("");
      distSel.innerHTML = '<option value="">请选择</option>';
      syncCityValue(box);
    });
  });
  root.querySelectorAll("[data-casc-city]").forEach((sel) => {
    sel.addEventListener("change", () => {
      const box = sel.closest(".area-cascade");
      const prov = box.querySelector("[data-casc-prov]").value;
      const city = sel.value;
      const distSel = box.querySelector("[data-casc-dist]");
      const dists = (prov && city) ? (AREA["中国"][prov][city] || []) : [];
      distSel.innerHTML = ["", ...dists].map((d) => `<option value="${d}">${d || "请选择"}</option>`).join("");
      syncCityValue(box);
    });
  });
  root.querySelectorAll("[data-casc-dist]").forEach((sel) => {
    sel.addEventListener("change", () => syncCityValue(sel.closest(".area-cascade")));
  });
}

function parseWindows(val) {
  if (!val) return [];
  if (Array.isArray(val)) return val;
  if (typeof val === "string") {
    try {
      const a = JSON.parse(val);
      if (Array.isArray(a)) return a;
    } catch (e) {}
    return val.split(",").map((s) => s.trim()).filter(Boolean);
  }
  return [];
}

function collectForm(containerId) {
  const out = {};
  const multi = {};
  document.querySelectorAll(`#${containerId} [data-key]`).forEach((el) => {
    const k = el.dataset.key;
    if (el.dataset.winMulti !== undefined) {
      const presets = Array.from(el.querySelectorAll("input[data-win]:checked")).map((x) => x.dataset.win);
      const customs = Array.from(el.querySelectorAll("[data-custom-win]")).map((x) => x.dataset.customWin);
      multi[k] = JSON.stringify(presets.concat(customs));
      return;
    }
    if (el.hasAttribute("data-win")) return;
    if (el.type === "checkbox") out[k] = el.checked;
    else if (el.type === "range") out[k] = parseInt(el.value, 10);
    else out[k] = el.value;
  });
  Object.assign(out, multi);
  return out;
}

// window-multi 自定义时段：添加/删除（事件委托）
document.addEventListener("click", (e) => {
  const del = e.target.closest("[data-del-win]");
  if (del) {
    const tag = del.closest("[data-custom-win]");
    if (tag) tag.remove();
    const container = tag.closest(".win-multi");
    const ph = container.querySelector("[data-placeholder]");
    if (ph && !container.querySelector("[data-custom-win]")) ph.style.display = "";
    return;
  }
  const addBtn = e.target.closest("[data-add-btn]");
  if (addBtn) {
    const key = addBtn.dataset.addBtn;
    const container = addBtn.closest(".win-multi");
    const input = container.querySelector(`[data-add-win="${key}"]`);
    const val = (input.value || "").trim();
    if (val && /^\d{2}:\d{2}-\d{2}:\d{2}$/.test(val)) {
      const dup = Array.from(container.querySelectorAll("[data-custom-win]")).some((x) => x.dataset.customWin === val);
      if (!dup) {
        const span = document.createElement("span");
        span.className = "win-tag";
        span.dataset.key = key;
        span.dataset.customWin = val;
        span.innerHTML = esc(val) + ` <b data-del-win="${esc(val)}">×</b>`;
        const area = container.querySelector(".win-custom");
        const ph = area.querySelector("[data-placeholder]");
        if (ph) ph.style.display = "none";
        area.insertBefore(span, ph);
      }
      input.value = "";
    } else {
      alert("自定义时段格式：HH:MM-HH:MM，如 07:00-08:00");
    }
  }
});

// ---------- 渲染 ----------
function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderOverview(data) {
  const s = data.stats || {};
  setText("statTargets", s.targets);
  setText("statLocations", s.locations);
  setText("statEvents", s.active_events);
  setText("statPlans", s.pending_plans);
  setText("statToday", data.today_sent);
  setText("statSent", s.sent_logs);
  // 事件来源
  const bars = document.getElementById("sourceBars");
  const src = data.events_by_source || {};
  const labels = { weather: "天气", state: "状态", chat: "对话", config: "配置" };
  const total = Object.values(src).reduce((a, b) => a + b, 0) || 1;
  bars.innerHTML = Object.entries(src).map(([k, v]) => `
    <div class="source-bar">
      <span class="source-label">${labels[k] || k}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(v / total) * 100}%"></div></div>
      <span class="source-count">${v}</span>
    </div>`).join("") || '<p class="empty">暂无事件</p>';
  // 运行状态
  const rt = data.runtime || {};
  const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN") : "—";
  document.getElementById("runtimeStatus").innerHTML = `
    <div class="rt-row"><span>人格</span><b>${data.persona_loaded ? "已加载" : "未加载"}</b></div>
    <div class="rt-row"><span>最后天气检查</span><b>${fmt(rt.last_weather_ts)}</b></div>
    <div class="rt-row"><span>最后反思</span><b>${fmt(rt.last_reflect_ts)}</b></div>
    <div class="rt-row"><span>当前时间</span><b>${rt.now || ""}</b></div>`;
  setText("personaBadge", data.persona_loaded ? "人格已加载" : "人格未加载");
  // 设置表单（各分区）
  const cfg = data.config || {};
  buildForm("weatherSettings", FORM_META.weather, cfg);
  buildForm("decisionSettings", FORM_META.decision, cfg);
  buildForm("proactiveSettings", FORM_META.proactive, cfg);
  buildForm("globalSettings", FORM_META.global, cfg);
  buildForm("targetsSettings", FORM_META.targets, cfg);
  document.querySelectorAll('input[type="range"]').forEach((el) => {
    el.addEventListener("input", () => {
      const span = document.querySelector(`[data-range="${el.dataset.key}"]`);
      if (span) span.textContent = el.value;
    });
  });
}

function renderTargets(list) {
  const el = document.getElementById("targetList");
  // 默认对象（用户自己）在「我的信息」卡片配置，列表只显示加号添加的关系对象
  const others = (list || []).filter((t) => !t.is_default);
  el.innerHTML = others.map((t) => `
    <div class="target-item">
      <div class="target-info">
        <b>${esc(t.name)}</b>
        <span class="tag">${esc(t.relation || "—")}</span>
        ${t.user_id ? `<span class="tag">QQ ${esc(t.user_id)}</span>` : '<span class="tag tag-warn">未填QQ</span>'}
        <span class="muted">📍 ${esc(t.location)}</span>
      </div>
      <button class="btn btn-danger btn-sm" data-del="${t.id}">删除</button>
    </div>`).join("") || '<p class="empty">还没有关怀对象，点右上角「+ 添加对象」添加你的家人朋友吧</p>';
  el.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(API.deleteTarget, "POST", { id: btn.dataset.del });
      load();
    });
  });
}

function renderEvents(list) {
  const el = document.getElementById("eventList");
  el.innerHTML = list.map((e) => `
    <div class="event-item pri-${e.priority}">
      <div class="event-main">
        <span class="tag ${e.source === "weather" ? "tag-weather" : "tag-state"}">${e.source === "weather" ? "天气" : "状态"}</span>
        <div class="event-text">${esc(e.summary)}</div>
        <span class="muted">${e.created}${e.expire !== "持续" ? " · " + e.expire + " 到期" : ""}</span>
      </div>
      <button class="btn btn-ghost btn-sm" data-resolve="${e.id}">解决</button>
    </div>`).join("") || '<p class="empty">暂无活跃事件</p>';
  el.querySelectorAll("[data-resolve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(API.resolveEvent, "POST", { id: btn.dataset.resolve });
      load();
    });
  });
}

function renderResolved(list) {
  const el = document.getElementById("resolvedList");
  el.innerHTML = list.map((e) => `
    <div class="event-item pri-${e.priority}">
      <div class="event-main">
        <span class="tag ${e.source === "weather" ? "tag-weather" : "tag-state"}">${e.source === "weather" ? "天气" : "状态"}</span>
        <div class="event-text">${esc(e.summary)}</div>
        <span class="muted">${e.created} · 已解决 ${e.resolved}</span>
      </div>
    </div>`).join("") || '<p class="empty">暂无已解决事件</p>';
}

document.getElementById("resolvedToggle").addEventListener("click", () => {
  const list = document.getElementById("resolvedList");
  const btn = document.getElementById("resolvedToggle");
  const collapsed = list.style.display === "none";
  list.style.display = collapsed ? "block" : "none";
  btn.textContent = collapsed ? "收起" : "展开";
  if (collapsed) {
    api(API.resolvedEvents).then((r) => renderResolved((r.data) || [])).catch(() => {});
  }
});

function renderPlans(list) {
  const el = document.getElementById("planList");
  el.innerHTML = list.map((p) => `
    <div class="plan-item">
      <div class="plan-main">
        <b>${esc(p.content)}</b>
        <span class="muted">${p.date} · ${windowLabel(p.window)} · ${esc(p.task_type)}</span>
      </div>
      <button class="btn btn-ghost btn-sm" data-cancel="${p.id}">取消</button>
    </div>`).join("") || '<p class="empty">暂无待发计划</p>';
  el.querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(API.cancelPlan, "POST", { id: btn.dataset.cancel });
      load();
    });
  });
}

function renderSends(list) {
  const el = document.getElementById("sendList");
  el.innerHTML = list.map((s) => `
    <div class="send-item">
      <div class="send-text">${esc(s.content)}</div>
      <span class="muted">${s.sent_at}</span>
    </div>`).join("") || '<p class="empty">还没有开口记录</p>';
}

function renderLocations(list) {
  const el = document.getElementById("locList");
  el.innerHTML = list.map((l) => `
    <div class="loc-item">
      <div class="loc-name">${esc(l.name)}<span class="tag ${l.type === "dynamic" ? "tag-primary" : ""}">${l.type === "dynamic" ? "动态" : "固定"}</span></div>
      <div class="loc-weather">${esc(l.weather_desc || "暂无数据")}
        ${l.tmax != null ? ` · <b>${l.tmax}°</b>/${l.tmin != null ? l.tmin + "°" : "–"}` : ""}
        ${l.precip_prob != null ? ` · 降水 ${l.precip_prob}%` : ""}
      </div>
    </div>`).join("") || '<p class="empty">暂无监测地点</p>';
}

function renderDecisions(list) {
  const el = document.getElementById("decisionList");
  el.innerHTML = list.map((d) => `
    <div class="decision-item dec-${d.decision}">
      <span class="tag dec-tag">${d.decision === "act" ? "立即开口" : d.decision === "plan" ? "规划未来" : "沉默"}</span>
      <div class="decision-text">
        ${d.background ? `<b>${esc(d.background)}</b>` : ""}
        ${d.plan_date ? `<span class="muted"> → ${d.plan_date} ${windowLabel(d.trigger_window)}</span>` : ""}
        <div class="muted">${esc(d.reason || "")} · ${d.source} · ${d.created}</div>
      </div>
    </div>`).join("") || '<p class="empty">暂无决策记录（决策循环启动后生成）</p>';
}

function windowLabel(w) {
  return { morning: "早上", noon: "中午", evening: "傍晚", night: "晚上" }[w] || w || "";
}

function setText(id, v) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = v ?? "–";
  return el;
}

// ---------- 加载 ----------
async function load() {
  const badge = document.getElementById("personaBadge");
  try {
    const data = await api(API.overview);
    if (data.code !== 0) throw new Error(data.message);
    const ov = data.data || {};
    renderOverview(ov);
    const cfg = ov.config || {};
    buildForm("weatherSettings", FORM_META.weather, cfg);
    buildForm("decisionSettings", FORM_META.decision, cfg);
    buildForm("stateSettings", FORM_META.state, cfg);
    buildForm("proactiveSettings", FORM_META.proactive, cfg);
    buildForm("globalSettings", FORM_META.global, cfg);
    badge.classList.remove("error");
    setText("personaBadge", "已连接");
  } catch (e) {
    console.error("overview 失败", e);
    badge.classList.add("error");
    setText("personaBadge", "加载失败：" + (e?.message || e));
  }
  try { renderTargets((await api(API.targets)).data || []); } catch (e) {}
  try { renderEvents((await api(API.events)).data || []); } catch (e) {}
  try { renderPlans((await api(API.plans)).data || []); } catch (e) {}
  try { renderSends((await api(API.sends)).data || []); } catch (e) {}
  try { renderLocations((await api(API.locations)).data || []); } catch (e) {}
  try { renderDecisions((await api(API.decisions)).data || []); } catch (e) {}
}

// ---------- 事件绑定 ----------
document.getElementById("refreshBtn").addEventListener("click", load);
document.getElementById("weatherRefreshBtn").addEventListener("click", async () => {
  const r = await api(API.actWeather, "POST", {});
  showAction(r.data?.message || r.message || "完成");
  load();
});
document.getElementById("testWeatherBtn").addEventListener("click", async () => {
  const r = await api(API.actWeather, "POST", {});
  showAction(r.data?.message || r.message || "完成");
  load();
});
document.getElementById("testReflectBtn").addEventListener("click", async () => {
  const r = await api(API.actReflect, "POST", {});
  showAction(r.data?.message || r.message || "完成");
  load();
});
document.getElementById("testDecisionBtn").addEventListener("click", async () => {
  const r = await api(API.actDecision, "POST", {});
  showAction(r.data?.message || r.message || "决策完成");
  setTimeout(load, 3000);
});
document.getElementById("testSendBtn").addEventListener("click", async () => {
  const r = await api(API.actSend, "POST", {});
  showAction(r.data?.message || r.message || "完成");
  setTimeout(load, 3000);
});
document.getElementById("testLocateBtn").addEventListener("click", async () => {
  const r = await api(API.actLocate, "POST", {});
  showAction(r.data?.message || r.message || "完成");
  setTimeout(load, 2000);
});

function showAction(msg) {
  // 全局 toast：无论当前在哪个分区都能看到保存/测试反馈
  let t = document.getElementById("globalToast");
  if (!t) {
    t = document.createElement("div");
    t.id = "globalToast";
    t.className = "global-toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 6000);
}

document.querySelectorAll("#weatherSettings, #decisionSettings, #proactiveSettings, #globalSettings, #targetsSettings").forEach((form) => {
  const save = document.createElement("button");
  save.className = "btn btn-primary btn-sm form-save";
  save.textContent = "保存本区设置";
  save.addEventListener("click", async () => {
    try {
      const cfg = collectForm(form.id);
      const r = await api(API.settings, "POST", cfg);
      showAction(r.data?.message || r.message || "已保存");
      load();
    } catch (e) {
      showAction("保存失败：" + (e?.message || e));
      console.error("保存失败", e);
    }
  });
  form.appendChild(save);
});

// 行政区划级联（中国 → 省 → 市 → 区县）
const AREA = window.AREAS_DATA || { 中国: {} };
const PROVINCE_DIRECT = ["北京市", "天津市", "上海市", "重庆市"];

function fillProvinceOptions() {
  const sel = document.getElementById("areaProvince");
  sel.innerHTML = '<option value="">请选择省</option>' +
    Object.keys(AREA["中国"] || {}).map((p) => `<option value="${p}">${p}</option>`).join("");
}
function fillCityOptions(prov) {
  const citySel = document.getElementById("areaCity");
  const distSel = document.getElementById("areaDistrict");
  distSel.innerHTML = '<option value="">请选择区县</option>';
  const cities = (AREA["中国"][prov] || {});
  citySel.innerHTML = '<option value="">请选择市</option>' +
    Object.keys(cities).map((c) => `<option value="${c}">${c}</option>`).join("");
}
function fillDistrictOptions(prov, city) {
  const distSel = document.getElementById("areaDistrict");
  const dists = ((AREA["中国"][prov] || {})[city]) || [];
  distSel.innerHTML = '<option value="">' + (dists.length ? "请选择区县" : "无区县") + "</option>" +
    dists.map((d) => `<option value="${d}">${d}</option>`).join("");
}
function buildCityName() {
  const prov = document.getElementById("areaProvince").value;
  const city = document.getElementById("areaCity").value;
  const dist = document.getElementById("areaDistrict").value;
  const parts = [];
  if (prov) parts.push(prov);
  if (city && city !== "市辖区" && city !== "县") parts.push(city);
  if (dist) parts.push(dist);
  return parts.join(" ");
}
function resetAreaSelects() {
  fillProvinceOptions();
  fillCityOptions("");
  fillDistrictOptions("", "");
}
document.getElementById("areaProvince")?.addEventListener("change", (e) => fillCityOptions(e.target.value));
document.getElementById("areaCity")?.addEventListener("change", (e) => fillDistrictOptions(document.getElementById("areaProvince").value, e.target.value));

// 添加对象
document.getElementById("addTargetBtn").addEventListener("click", () => {
  resetAreaSelects(); // v1.1.6：打开弹窗时重置级联，确保省下拉有选项
  document.getElementById("modalMask").hidden = false;
});
document.getElementById("modalCancel").addEventListener("click", () => {
  document.getElementById("modalMask").hidden = true;
});
document.getElementById("modalConfirm").addEventListener("click", async () => {
  const name = document.getElementById("newName").value.trim();
  const relation = document.getElementById("newRelation").value.trim();
  const user_id = document.getElementById("newUserId").value.trim();
  const prov = document.getElementById("areaProvince").value;
  const city = buildCityName();
  if (!name) {
    document.getElementById("modalErr").textContent = "称呼必填";
    return;
  }
  const onlyProvince = prov && !document.getElementById("areaCity").value && !PROVINCE_DIRECT.includes(prov);
  if (!city || onlyProvince) {
    document.getElementById("modalErr").textContent = "请选择城市（至少选到市一级）";
    return;
  }
  try {
    const r = await api(API.addTarget, "POST", { name, relation, user_id, city });
    if (r.code === 0) {
      document.getElementById("modalMask").hidden = true;
      document.getElementById("newName").value = "";
      document.getElementById("newRelation").value = "";
      document.getElementById("newUserId").value = "";
      resetAreaSelects();
      load();
    } else {
      document.getElementById("modalErr").textContent = r.data?.message || r.message || "添加失败";
    }
  } catch (e) {
    document.getElementById("modalErr").textContent = e.message || "添加失败";
  }
});

load();
// v1.1.6：页面加载时初始化省市区级联（省下拉填充 34 个省，否则无法选择）
resetAreaSelects();
