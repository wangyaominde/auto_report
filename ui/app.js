/* WorkLog GUI 前端逻辑（无外部依赖） */
"use strict";

let api = null;
let currentKind = "daily";
let currentReport = null;
let corrections = [];

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ---------------- 基础 UI ---------------- */

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 2600);
}

/* 后台任务：界面不锁死，右下角角标显示进行中的任务，完成后 toast */
const runningTasks = new Map(); // key -> label

function showTasks() {
  const pill = $("#tasksPill");
  if (!runningTasks.size) { pill.hidden = true; return; }
  $("#tasksText").textContent = Array.from(runningTasks.values()).join("、") + " 进行中…";
  pill.hidden = false;
}

async function runTask(key, label, fn, doneText, onDone) {
  if (runningTasks.has(key)) { toast("「" + label + "」已在进行中，请稍候"); return; }
  runningTasks.set(key, label);
  showTasks();
  toast("已开始「" + label + "」，期间可继续使用其他功能");
  try {
    const r = await fn();
    if (r && r.ok === false) toast(label + " 失败：" + (r.output || r.error || "未知错误"));
    else { toast(doneText); if (onDone) onDone(); }
  } catch (e) {
    toast(label + " 出错：" + e);
  }
  runningTasks.delete(key);
  showTasks();
  refreshStatus();
}

function switchPage(page) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $$(".page").forEach((p) => { p.hidden = p.id !== "page-" + page; });
  if (page === "reports") loadReportList();
  if (page === "memory") loadMemory();
  if (page === "blacklist") loadBlacklist();
  if (page === "settings") loadSettings();
}

/* ---------------- Markdown 渲染 ---------------- */

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, (_, c) => "<code>" + c + "</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
}

function renderMarkdown(md) {
  const lines = md.split(/\r?\n/);
  const out = [];
  let list = null; // "ul" | "ol"
  let inCode = false;
  let table = [];

  const closeList = () => { if (list) { out.push("</" + list + ">"); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?[\s:|-]+\|?\s*$/.test(r));
    let html = "<table>";
    rows.forEach((row, i) => {
      const cells = row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|");
      const tag = i === 0 ? "th" : "td";
      html += "<tr>" + cells.map((c) => `<${tag}>` + inlineMd(escapeHtml(c.trim())) + `</${tag}>`).join("") + "</tr>";
    });
    out.push(html + "</table>");
    table = [];
  };

  for (const raw of lines) {
    if (/^```/.test(raw)) {
      closeList(); flushTable();
      out.push(inCode ? "</code></pre>" : "<pre><code>");
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(escapeHtml(raw)); continue; }

    if (/^\s*\|.*\|\s*$/.test(raw)) { closeList(); table.push(raw); continue; }
    flushTable();

    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); out.push(`<h${h[1].length}>` + inlineMd(escapeHtml(h[2])) + `</h${h[1].length}>`); continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(raw)) { closeList(); out.push("<hr>"); continue; }

    const ul = raw.match(/^\s*[-*]\s+(.*)$/);
    const ol = raw.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? "ul" : "ol";
      if (list !== want) { closeList(); out.push("<" + want + ">"); list = want; }
      out.push("<li>" + inlineMd(escapeHtml((ul || ol)[1])) + "</li>");
      continue;
    }

    closeList();
    if (raw.trim() === "") continue;
    out.push("<p>" + inlineMd(escapeHtml(raw)) + "</p>");
  }
  closeList(); flushTable();
  if (inCode) out.push("</code></pre>");
  return out.join("\n");
}

/* ---------------- 仪表盘 ---------------- */

async function refreshStatus() {
  if (!api) return;
  try {
    const s = await api.status();
    $("#dashDate").textContent = s.date + " · 采集窗口 09:00–21:00";
    $("#statRecords").textContent = s.records;
    $("#statShots").textContent = s.screenshots;
    $("#statLast").textContent = s.lastRecord ? "最近记录 " + s.lastRecord.slice(11, 19) : "今日暂无记录";

    const colDot = $("#colDot"), colState = $("#colState");
    const footDot = $("#footDot"), footText = $("#footText");
    if (s.privacy) {
      colDot.className = "dot dot-amber"; colState.textContent = "隐私模式中";
      footDot.className = "dot dot-amber"; footText.textContent = "隐私模式 · 已暂停采集";
    } else if (s.collecting) {
      colDot.className = "dot dot-green"; colState.textContent = "采集中";
      footDot.className = "dot dot-green"; footText.textContent = "采集中 · 今日 " + s.records + " 条";
    } else {
      colDot.className = "dot dot-gray"; colState.textContent = "未在采集";
      footDot.className = "dot dot-gray"; footText.textContent = "未在采集";
    }
    $("#privacyToggle").checked = !!s.privacy;
    $("#privacyText").textContent = s.privacy ? "已开启" : "关闭";
  } catch (e) { /* 窗口初始化早于桥接就绪时忽略 */ }
}


/* ---------------- 报告 ---------------- */

async function loadReportList() {
  const names = await api.list_reports(currentKind);
  const listEl = $("#reportList");
  listEl.innerHTML = "";
  if (!names.length) {
    listEl.innerHTML = '<li class="empty">暂无报告</li>';
    return;
  }
  for (const name of names) {
    const li = document.createElement("li");
    li.textContent = prettyReportName(name);
    li.dataset.name = name;
    li.onclick = () => openReport(name);
    if (name === currentReport) li.classList.add("active");
    listEl.appendChild(li);
  }
}

function prettyReportName(name) {
  let m = name.match(/^daily-report-(.+)\.md$/);
  if (m) return m[1];
  m = name.match(/^weekly-report-(.+)_to_(.+)\.md$/);
  if (m) return m[1] + " ~ " + m[2].slice(5);
  m = name.match(/^monthly-report-(.+)\.md$/);
  if (m) return m[1] + " 月报";
  return name;
}

async function openReport(name) {
  const r = await api.read_report(name);
  if (!r.ok) { toast("读取失败：" + r.error); return; }
  currentReport = name;
  $$("#reportList li").forEach((li) => li.classList.toggle("active", li.dataset.name === name));
  $("#reportTitle").textContent = prettyReportName(name);
  $("#btnRegen").disabled = false;

  // 只渲染 AI 正文部分（采集摘要原始数据太长，折叠掉）
  let content = r.content;
  const cut = content.indexOf("## 采集摘要");
  if (cut !== -1) content = content.slice(0, cut);

  corrections = await api.corrections_list();
  const body = $("#reportBody");
  body.innerHTML = renderMarkdown(content);
  attachStrikeButtons(body);
}

function attachStrikeButtons(root) {
  const struckTexts = new Set(corrections.map((c) => c.text));
  root.querySelectorAll("li").forEach((li) => {
    const text = li.textContent.trim();
    if (!text || text.length < 4) return;
    if (struckTexts.has(text)) li.classList.add("struck");
    const btn = document.createElement("button");
    btn.className = "strike-btn";
    btn.textContent = "✕ 不是我做的";
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const r = await api.corrections_add(text, currentReport, "not_mine");
      if (r.ok) {
        li.classList.add("struck");
        corrections.unshift(r.entry);
        toast("已记入记忆库，重新生成时将剔除同类内容");
      }
    };
    li.appendChild(btn);
  });
}

/* ---------------- 记忆库 ---------------- */

const REASON_LABEL = { not_mine: "非我的", wrong: "有误", private: "隐私" };

async function loadMemory() {
  $("#profileEditor").value = await api.profile_get();
  corrections = await api.corrections_list();
  $("#corrCount").textContent = corrections.length + " 条";
  const listEl = $("#corrList");
  listEl.innerHTML = "";
  if (!corrections.length) {
    listEl.innerHTML = '<li style="color:var(--text-2)">还没有否决记录——在报告页勾掉不属于你的条目即可积累</li>';
    return;
  }
  for (const c of corrections) {
    const li = document.createElement("li");
    const badge = document.createElement("span");
    badge.className = "corr-badge";
    badge.textContent = REASON_LABEL[c.reason] || "否决";
    const div = document.createElement("div");
    div.className = "corr-text";
    div.textContent = c.text;
    const meta = document.createElement("div");
    meta.className = "corr-meta";
    meta.textContent = (c.ts || "").replace("T", " ") + (c.report ? " · " + c.report : "");
    div.appendChild(meta);
    const del = document.createElement("button");
    del.className = "corr-del";
    del.textContent = "✕";
    del.title = "删除该记录";
    del.onclick = async () => {
      await api.corrections_delete(c.id);
      loadMemory();
    };
    li.append(badge, div, del);
    listEl.appendChild(li);
  }
}

/* ---------------- 黑名单 ---------------- */

let blKeywords = [];

async function loadBlacklist() {
  blKeywords = await api.blacklist_get();
  renderChips();
}

function renderChips() {
  const box = $("#blChips");
  box.innerHTML = "";
  for (const kw of blKeywords) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = kw;
    const del = document.createElement("button");
    del.textContent = "✕";
    del.onclick = async () => {
      blKeywords = blKeywords.filter((k) => k !== kw);
      await api.blacklist_save(blKeywords);
      renderChips();
    };
    chip.append(label, del);
    box.appendChild(chip);
  }
}

async function addBlacklistKeyword() {
  const input = $("#blInput");
  const kw = input.value.trim();
  if (!kw) return;
  if (!blKeywords.includes(kw)) {
    blKeywords.push(kw);
    await api.blacklist_save(blKeywords);
    renderChips();
    toast("已添加：" + kw);
  }
  input.value = "";
}

/* ---------------- 大模型设置 ---------------- */

const PRESETS = {
  minimax: {
    label: "MiniMax", url: "https://api.minimaxi.com/anthropic/v1/messages",
    format: "anthropic", model: "MiniMax-M3",
    hint: "MiniMax 编程套餐（sk-cp- 开头的 Key）只能走 Anthropic 接口格式。M3 支持视觉。",
  },
  openai: {
    label: "OpenAI", url: "https://api.openai.com/v1/chat/completions",
    format: "openai", model: "gpt-4o",
    hint: "模型名可按需修改（需支持图片输入，如 gpt-4o / gpt-5 系列）。",
  },
  anthropic: {
    label: "Anthropic", url: "https://api.anthropic.com/v1/messages",
    format: "anthropic", model: "claude-sonnet-5",
    hint: "Claude 全系支持视觉；也可改用 claude-haiku-4-5-20251001 降低成本。",
  },
  kimi: {
    label: "Kimi", url: "https://api.moonshot.cn/v1/chat/completions",
    format: "openai", model: "kimi-latest",
    hint: "月之暗面 OpenAI 兼容接口；kimi-latest 指向最新模型，需确认视觉支持。",
  },
  deepseek: {
    label: "DeepSeek", url: "https://api.deepseek.com/v1/chat/completions",
    format: "openai", model: "deepseek-chat",
    hint: "注意：DeepSeek 当前接口不支持图片输入，截图分析将不可用，只能生成纯文字报告。",
  },
};

function detectPreset(url) {
  for (const [id, p] of Object.entries(PRESETS)) {
    try { if (new URL(p.url).host === new URL(url).host) return id; } catch (e) { /* ignore */ }
  }
  return null;
}

function renderPresetChips(activeId) {
  const box = $("#presetChips");
  box.innerHTML = "";
  for (const [id, p] of Object.entries(PRESETS)) {
    const b = document.createElement("button");
    b.className = "preset-chip" + (id === activeId ? " active" : "");
    b.textContent = p.label;
    b.onclick = () => {
      $("#setUrl").value = p.url;
      $("#setModel").value = p.model;
      $("#setFormat").value = p.format;
      $("#presetHint").textContent = p.hint;
      renderPresetChips(id);
      $("#setKey").focus();
    };
    box.appendChild(b);
  }
  const custom = document.createElement("button");
  custom.className = "preset-chip" + (activeId === null ? " active" : "");
  custom.textContent = "自定义";
  custom.onclick = () => { renderPresetChips(null); $("#presetHint").textContent = "自定义任意 OpenAI / Anthropic 兼容接口。"; };
  box.appendChild(custom);
}

async function loadSettings() {
  const s = await api.settings_get();
  $("#setUrl").value = s.url || "";
  $("#setKey").value = s.key || "";
  $("#setModel").value = s.model || "";
  $("#setFormat").value = s.format === "openai" ? "openai" : "anthropic";
  $("#setMaxTokens").value = s.maxTokens || "8000";
  $("#setTemp").value = s.temperature || "0.2";
  $("#setShotDays").value = s.shotRetentionDays || "7";
  $("#setShotDelete").checked = !!s.shotDeleteAfterAnalysis;
  const active = detectPreset(s.url || "");
  renderPresetChips(active);
  if (active) $("#presetHint").textContent = PRESETS[active].hint;
}

async function saveSettings() {
  const cfg = {
    url: $("#setUrl").value.trim(),
    key: $("#setKey").value.trim(),
    model: $("#setModel").value.trim(),
    format: $("#setFormat").value,
    maxTokens: $("#setMaxTokens").value.trim(),
    temperature: $("#setTemp").value.trim(),
    shotRetentionDays: $("#setShotDays").value.trim(),
    shotDeleteAfterAnalysis: $("#setShotDelete").checked,
  };
  if (!cfg.url || !cfg.key || !cfg.model) { toast("API 地址、Key、模型均不能为空"); return null; }
  const r = await api.settings_save(cfg);
  if (r.ok) toast("已保存，下一次生成/分析即用新配置");
  else toast("保存失败：" + (r.output || ""));
  return r.ok;
}

/* ---------------- 初始化 ---------------- */

function bindEvents() {
  $$(".nav-item").forEach((b) => { b.onclick = () => switchPage(b.dataset.page); });
  $$(".tab").forEach((t) => {
    t.onclick = () => {
      $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
      currentKind = t.dataset.kind;
      loadReportList();
    };
  });

  $("#privacyToggle").onchange = async (e) => {
    await api.privacy_set(e.target.checked);
    toast(e.target.checked ? "隐私模式已开启：暂停记录与截图" : "隐私模式已关闭");
    refreshStatus();
  };

  $("#btnColStart").onclick = () => runTask("col", "启动采集", () => api.collector_start(), "采集已启动");
  $("#btnColStop").onclick = () => runTask("col", "停止采集", () => api.collector_stop(), "已发送停止指令（5 秒内生效）");
  $("#btnGenToday").onclick = () => runTask("report", "生成今日日报", () => api.generate_today(), "今日日报已生成，可在报告页查看");
  $("#btnShot").onclick = () => runTask("shot", "截图", () => api.screenshot_now(), "截图完成");
  $("#btnAnalyze").onclick = () => runTask("analyze", "分析截图", () => api.analyze_now(), "截图分析完成（视觉模型约 1–2 分钟）");
  $("#btnOpenReports").onclick = () => api.open_folder("reports");
  $("#btnOpenShots").onclick = () => api.open_folder("screenshots");

  $("#btnRegen").onclick = () => {
    if (!currentReport) return;
    const name = currentReport;
    runTask("regen", "重新生成 " + prettyReportName(name), () => api.regenerate(name),
      "已重新生成：" + prettyReportName(name),
      () => { if (currentReport === name) openReport(name); });
  };

  $("#btnProfileSave").onclick = async () => {
    await api.profile_save($("#profileEditor").value);
    toast("档案已保存，下次生成报告即生效");
  };

  $("#btnBlAdd").onclick = addBlacklistKeyword;
  $("#blInput").onkeydown = (e) => { if (e.key === "Enter") addBlacklistKeyword(); };

  $("#btnKeyShow").onclick = () => {
    const input = $("#setKey");
    input.type = input.type === "password" ? "text" : "password";
    $("#btnKeyShow").textContent = input.type === "password" ? "显示" : "隐藏";
  };
  $("#btnSetSave").onclick = saveSettings;
  $("#btnSetTest").onclick = async () => {
    const saved = await saveSettings();
    if (!saved) return;
    runTask("llmtest", "测试 LLM 连接", () => api.settings_test(), "连接成功，模型响应正常");
  };
}

window.addEventListener("pywebviewready", () => {
  api = window.pywebview.api;
  bindEvents();
  refreshStatus();
  setInterval(refreshStatus, 5000);
});
