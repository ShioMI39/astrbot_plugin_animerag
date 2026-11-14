const B = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (s) => document.querySelector(s);

const KIND = {
  work: "番剧", character: "角色", person: "人物",
  org: "公司/组合", episode: "单集", moegirl: "萌百",
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const n = (v) => (typeof v === "number" ? v.toLocaleString("zh-CN") : v ?? "—");

/* ── 标签页 ── */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("is-on", b === btn));
    document.querySelectorAll(".pane").forEach((p) =>
      p.classList.toggle("is-on", p.id === "pane-" + btn.dataset.tab));
  });
});

function setHead(text, kind) {
  $("#sub").textContent = text;
  $("#dot").className = "dot" + (kind ? " " + kind : "");
}

/* ── 概览 ── */
function renderOverview(s) {
  setHead(s.ready ? n(s.blocks) + " 块索引" : "未就绪：" + (s.error || "未知"),
          s.ready ? "ok" : "bad");

  const metrics = [
    ["番剧", n(s.works)],
    ["角色", n(s.characters)],
    ["单集", n(s.episodes)],
    ["萌百页", n(s.moegirl_pages)],
    ["译文", n(s.translations)],
    ["向量块", n(s.blocks)],
  ];
  $("#metrics").innerHTML = metrics
    .map(([k, v]) => `<div class="metric"><div class="v">${esc(v)}</div><div class="k">${k}</div></div>`)
    .join("");

  const kv = (rows) =>
    rows.map(([k, v, mono]) =>
      `<dt>${k}</dt><dd class="${mono ? "mono" : ""}">${esc(v) || "—"}</dd>`).join("");

  $("#models").innerHTML = kv([
    ["萌娘百科", s.moegirl ? "已启用 · " + n(s.moegirl_pages) + " 页" : "已关闭（只跑 Bangumi 层）"],
    ["Embedding", s.embedding || "未配置 · 语义检索关闭"],
    ["Rerank", s.rerank || "未配置 · 不重排"],
    ["翻译", s.translate || "未配置"],
    ["维度", s.embed_dim ?? "—"],
  ]);
  $("#paths").innerHTML = kv([
    ["数据版本", s.data_version],
    ["数据目录", s.data_dir, true],
  ]);

  $("#updateInfo").innerHTML = kv([
    ["当前版本", s.data_version],
    ["数据目录", s.data_dir, true],
  ]);
}

let lastStatus = null;

async function refresh() {
  try {
    lastStatus = await B.apiGet("status");
    renderOverview(lastStatus);
  } catch (e) {
    setHead("读取失败：" + e.message, "bad");
  }
}

/* ── 检索 ── */
async function search() {
  const q = $("#q").value.trim();
  if (!q) return;
  const btn = $("#go");
  btn.disabled = true;
  $("#results").innerHTML = '<p class="empty">检索中…</p>';
  const t0 = performance.now();
  try {
    const r = await B.apiGet("search", { q });
    const ms = Math.round(performance.now() - t0);

    if (r.mode === "attr") {
      const labels = (r.attr || [])
        .map((a) => `<span class="tag">${esc(a.key)} · ${esc(a.label)}</span>`)
        .join("");
      $("#searchHint").innerHTML = labels + `共 ${n(r.total)} 个角色`;
      $("#results").innerHTML = r.hits.length
        ? `<ul class="res">` + r.hits.map((h, i) =>
            `<li><span class="idx">${i + 1}</span><span>${esc(h.title)}</span>` +
            `<span class="note">${esc(h.note)}</span></li>`).join("") + "</ul>"
        : '<p class="empty">没有符合条件的角色</p>';
      return;
    }

    // 排序是 rerank 分
    const order = r.reranked ? "重排" : "向量";
    $("#searchHint").innerHTML =
      `语义 + 词法 · ${n(r.total)} 条命中 · ${ms} ms · 按${order}分排序`;
    $("#results").innerHTML = r.hits.length
      ? `<ul class="res">` + r.hits.map((h, i) => {
          const main = h.rerank_score != null ? h.rerank_score : h.score;
          return `<li><span class="idx">${i + 1}</span>` +
            `<span class="tag">${KIND[h.kind] || esc(h.kind)}</span>` +
            `<span>${esc(h.title)}</span>` +
            (main != null ? `<span class="score">${main.toFixed(3)}</span>` : "") +
            "</li>";
        }).join("") + "</ul>"
      : '<p class="empty">没有命中</p>';
  } catch (e) {
    $("#results").innerHTML = `<p class="empty">检索失败：${esc(e.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
}

/* ── 更新 ── */
let pollTimer = null;

function renderJob(j) {
  const running = j.state === "running";
  $("#update").disabled = running;
  $("#progressWrap").hidden = j.state === "idle";
  $("#bar").style.width = (j.percent || 0) + "%";
  $("#pct").textContent = (j.percent || 0) + "%";
  const msg = $("#updateMsg");
  msg.textContent = j.message || "";
  msg.className = "msg" +
    (j.state === "done" ? " ok" : j.state === "error" ? " err" : "");
  const log = $("#updateLog");
  log.hidden = !(j.log && j.log.length);
  log.textContent = (j.log || []).join("\n");
  if (running && !pollTimer) {
    pollTimer = setInterval(pollJob, 1000);
  } else if (!running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
    refresh();
  }
}

async function pollJob() {
  try {
    renderJob(await B.apiGet("update/status"));
  } catch {
    /* 忽略单次失败 */
  }
}

async function checkUpdate() {
  const btn = $("#check");
  btn.disabled = true;
  $("#updateMsg").textContent = "查询中…";
  $("#updateMsg").className = "msg";
  try {
    const r = await B.apiGet("check-update");
    if (r.error) {
      $("#updateMsg").textContent = "查询失败：" + r.error;
      $("#updateMsg").className = "msg err";
    } else if (r.up_to_date) {
      $("#updateMsg").textContent = "已是最新（" + r.current + "）";
      $("#updateMsg").className = "msg ok";
    } else {
      $("#updateMsg").textContent =
        "有新版：" + r.current + " → " + r.latest + "（" + r.size_mb + " MB）";
      $("#updateMsg").className = "msg";
    }
  } finally {
    btn.disabled = false;
  }
}

async function startUpdate() {
  $("#update").disabled = true;
  $("#updateLog").hidden = false;
  try {
    renderJob(await B.apiPost("update", {}));
  } catch (e) {
    $("#updateMsg").textContent = "启动失败：" + e.message;
    $("#updateMsg").className = "msg err";
    $("#update").disabled = false;
  }
}

/* ── 配置 ── */
let configData = null;
const edits = {};

const GROUP_TITLE = { model_group: "模型", param_group: "参数", other_group: "其他" };
const GROUP_HINT = {
  "model_group": "都填 AstrBot「服务提供商」里的 provider ID",
  "param_group": "调检索效果，一般不用动",
  "other_group": "数据目录、下载地址、代理"
};

function fieldHtml(group, item, value) {
  const id = `${group}__${item.key}`;
  const title = item.description || item.key;
  const key = title.includes(item.key) ? "" : `<span class="k">${esc(item.key)}</span>`;
  const label = `<label for="${id}">${esc(title)}${key}</label>`;
  const hint = item.hint ? `<div class="h">${esc(item.hint)}</div>` : "";
  let input;
  if (item.type === "bool") {
    input = `<div class="switch"><input id="${id}" type="checkbox" data-g="${group}" data-k="${item.key}" ${value ? "checked" : ""}><span>${value ? "已启用" : "未启用"}</span></div>`;
  } else if (item.type === "int" || item.type === "float") {
    input = `<input id="${id}" type="number" step="${item.type === "int" ? "1" : "0.01"}" value="${esc(value)}" data-g="${group}" data-k="${item.key}">`;
  } else {
    input = `<input id="${id}" type="text" value="${esc(value)}" data-g="${group}" data-k="${item.key}" autocomplete="off">`;
  }
  return `<div class="field" data-group="${group}" data-key="${item.key}">${label}${input}${hint}</div>`;
}

async function loadConfig() {
  configData = await B.apiGet("config");
  const flat = configData.flat || {};
  const html = Object.entries(configData.schema || {}).map(([group, items]) => {
    const body = items.map((it) => fieldHtml(group, it, flat[it.key])).join("");
    const title = GROUP_TITLE[group] || group.replace(/_group$/, "");
    const hint = GROUP_HINT[group] ? `<p>${esc(GROUP_HINT[group])}</p>` : "";
    return `<div class="cfg-group"><h3>${esc(title)}</h3>${hint}${body}</div>`;
  }).join("");
  $("#configForm").innerHTML = html;

  $("#configForm").querySelectorAll("input[data-k]").forEach((el) => {
    el.addEventListener("input", () => {
      const g = el.dataset.g, k = el.dataset.k;
      const field = el.closest(".field");
      const orig = configData.flat?.[k];
      let v;
      if (el.type === "checkbox") v = el.checked;
      else if (el.type === "number") v = el.value === "" ? null : Number(el.value);
      else v = el.value;
      if (String(v) === String(orig)) delete edits[k], field.classList.remove("dirty");
      else edits[k] = { group: g, value: v }, field.classList.add("dirty");
      if (el.type === "checkbox") {
        el.nextElementSibling.textContent = el.checked ? "已启用" : "未启用";
      }
    });
  });
}

async function saveConfig() {
  const keys = Object.keys(edits);
  if (!keys.length) {
    $("#configMsg").textContent = "没有改动";
    $("#configMsg").className = "msg";
    return;
  }
  const payload = {};
  for (const k of keys) {
    const { group, value } = edits[k];
    (payload[group] ||= {})[k] = value;
  }
  const btn = $("#save");
  btn.disabled = true;
  try {
    const r = await B.apiPost("config/save", payload);
    const n = r.pending?.length || 0;
    if (r.saved) {
      $("#configMsg").textContent =
        "本次保存 " + r.changed.length + " 项 · 累计 " + n + " 项待重启";
      $("#configMsg").className = "msg warn";
    } else {
      $("#configMsg").textContent = n
        ? "没有新的改动 · 仍有 " + n + " 项待重启"
        : (r.message || "没有变化");
      $("#configMsg").className = "msg " + (n ? "warn" : "");
    }
    for (const k of keys) delete edits[k];
    $("#configForm").querySelectorAll(".field.dirty").forEach((f) => f.classList.remove("dirty"));
    await loadConfig();
  } catch (e) {
    $("#configMsg").textContent = "保存失败：" + e.message;
    $("#configMsg").className = "msg err";
  } finally {
    btn.disabled = false;
  }
}

/* ── 资源包 ── */
const DATA_PACKAGE_URL =
  "https://github.com/ShioMI39/astrbot_plugin_animerag/releases/download/data/data.zip";

async function copyDataPackageUrl() {
  const msg = $("#downloadMsg");
  const urlEl = $("#dataUrlText");
  msg.className = "msg";
  try {
    const range = document.createRange();
    range.selectNodeContents(urlEl);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  } catch (e) {
    /* 选中失败时仍然尝试写剪贴板 */
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(DATA_PACKAGE_URL);
      msg.className = "msg ok";
      msg.textContent = "直链已复制，粘贴到浏览器地址栏下载。";
      return;
    }
  } catch (e) {
    /* iframe 下 clipboard 常被拒绝，退回选中文本 */
  }
  try {
    if (document.execCommand("copy")) {
      msg.className = "msg ok";
      msg.textContent = "直链已复制，粘贴到浏览器地址栏下载。";
      return;
    }
  } catch (e) {
    /* ignore */
  }
  msg.className = "msg warn";
  msg.textContent = "已选中链接，请按 Ctrl+C 复制。";
}

/* ── 数据维护 ── */
const maintTimers = { vec: null, tr: null };
let lastTrCheck = null;
const MAINT_PATH = { vec: "vector", tr: "translation" };

function renderMaint(prefix, j) {
  const msg = $("#" + prefix + "Msg");
  msg.textContent = j.message || "";
  msg.className = "msg" +
    (j.state === "done" ? " ok" : j.state === "error" ? " err" : "");
  const log = $("#" + prefix + "Log");
  log.hidden = !(j.log && j.log.length);
  log.textContent = (j.log || []).join("\n");
  $("#" + prefix + "Update").disabled = j.state === "running";
  if (j.state === "running" && !maintTimers[prefix]) {
    maintTimers[prefix] = setInterval(() => pollMaint(prefix), 1000);
  } else if (j.state !== "running" && maintTimers[prefix]) {
    clearInterval(maintTimers[prefix]);
    maintTimers[prefix] = null;
  }
}

async function pollMaint(prefix) {
  try {
    renderMaint(prefix, await B.apiGet(MAINT_PATH[prefix] + "/status"));
  } catch {
    /* 忽略单次失败 */
  }
}

async function checkVectors() {
  const btn = $("#vecCheck");
  btn.disabled = true;
  $("#vecMsg").className = "msg";
  $("#vecMsg").textContent = "检查中…";
  try {
    const r = await B.apiGet("vector/check");
    const rows = [
      ["状态", r.needs_update ? "需要更新" : "已是最新"],
      ["块数", n(r.total)],
      ["向量行", n(r.embeddings)],
      ["manifest", n(r.manifest)],
      ["维度", r.dim || "—"],
      ["向量模型", r.embed_model || "—"],
      ["tag 词数", n(r.tags)],
      ["tag 向量", n(r.tag_embeddings)],
      ["tag 维度", r.tag_dim || "—"],
      ["tag 模型", r.tag_model || "—"],
      ["数据版本", r.data_version || "—"],
      ["构建时间", r.built_at || "—"],
      ["大小", (r.size_mb || 0) + " MB"],
    ];
    if (r.reasons && r.reasons.length) rows.push(["原因", r.reasons.join("；")]);
    $("#vecInfo").innerHTML = rows.map(
      (x) => "<dt>" + esc(x[0]) + "</dt><dd>" + esc(x[1]) + "</dd>").join("");
    $("#vecMsg").className = "msg " + (r.needs_update ? "warn" : "ok");
    $("#vecMsg").textContent = r.needs_update
      ? "向量需要更新：" + (r.reasons || []).join("；")
      : "向量已是最新。";
  } catch (e) {
    $("#vecMsg").className = "msg err";
    $("#vecMsg").textContent = "检查失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function startVectorUpdate() {
  $("#vecUpdate").disabled = true;
  $("#vecMsg").className = "msg";
  $("#vecMsg").textContent = "正在启动向量更新…";
  try {
    renderMaint("vec", await B.apiPost("vector/update", {}));
  } catch (e) {
    $("#vecMsg").className = "msg err";
    $("#vecMsg").textContent = "启动失败：" + e.message;
    $("#vecUpdate").disabled = false;
  }
}

async function checkTranslations() {
  const btn = $("#trCheck");
  btn.disabled = true;
  $("#trMsg").className = "msg";
  $("#trMsg").textContent = "检查中…";
  try {
    const r = await B.apiGet("translation/check");
    lastTrCheck = r;
    const counts = r.pending || {};
    $("#trCountCharacter").textContent = counts.character ? n(counts.character.pending) + " 条" : "";
    $("#trCountWork").textContent = counts.work ? n(counts.work.pending) + " 条" : "";
    $("#trCountEpisode").textContent = counts.episode ? n(counts.episode.pending) + " 条" : "";
    const parts = Object.entries(r.pending || {}).map(
      (x) => x[0] + " " + n(x[1].pending));
    const rows = [
      ["待译", n(r.total) + " 条 / " + n(r.chars) + " 字符"],
      ["分层", parts.join(" · ") || "—"],
      ["模型", r.has_provider ? "已配置" : "未配置 translate_provider_id"],
    ];
    $("#trInfo").innerHTML = rows.map(
      (x) => "<dt>" + esc(x[0]) + "</dt><dd>" + esc(x[1]) + "</dd>").join("");
    $("#trMsg").className = "msg " + (r.total ? "warn" : "ok");
    $("#trMsg").textContent = r.total
      ? "有 " + n(r.total) + " 条待译；同步只补新增/变更，不重译已有译文。"
      : "没有待译条目。";
  } catch (e) {
    $("#trMsg").className = "msg err";
    $("#trMsg").textContent = "检查失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function openTranslationDialog() {
  if ($("#trDialog").open) return;
  await checkTranslations().catch(() => {});
  $("#trDialog").showModal();
}

async function startTranslationUpdate() {
  const kinds = [...document.querySelectorAll(
    "#trDialog input[type=checkbox]:checked")].map((el) => el.value);
  if (!kinds.length) {
    alert("至少选择一层：角色简介 / 番剧简介 / 单集简介");
    return;
  }
  $("#trDialog").close();
  $("#trUpdate").disabled = true;
  try {
    renderMaint("tr", await B.apiPost("translation/update", { kinds }));
  } catch (e) {
    $("#trMsg").className = "msg err";
    $("#trMsg").textContent = "启动失败：" + e.message;
    $("#trUpdate").disabled = false;
  }
}

/* ── 绑定 ── */
$("#copyDataUrl").addEventListener("click", copyDataPackageUrl);
$("#go").addEventListener("click", search);
$("#q").addEventListener("keydown", (e) => e.key === "Enter" && search());
$("#check").addEventListener("click", checkUpdate);
$("#update").addEventListener("click", startUpdate);
$("#vecCheck").addEventListener("click", checkVectors);
$("#vecUpdate").addEventListener("click", startVectorUpdate);
$("#trCheck").addEventListener("click", checkTranslations);
$("#trUpdate").addEventListener("click", openTranslationDialog);
$("#trDialogCancel").addEventListener("click", () => $("#trDialog").close());
$("#trDialogStart").addEventListener("click", startTranslationUpdate);
$("#save").addEventListener("click", saveConfig);

/* ── 重启插件 ── */
async function restartPlugin() {
  const btn = $("#restart");
  btn.disabled = true;
  $("#configMsg").textContent = "正在重启…";
  $("#configMsg").className = "msg";
  try {
    await B.apiPost("restart", {});
    // 插件重建后旧连接会断，等一会儿再拉状态；失败也算正常
    await new Promise((r) => setTimeout(r, 2500));
    let ok = false;
    for (let i = 0; i < 6 && !ok; i++) {
      try {
        await refresh();
        await pollJob();
        ok = true;
      } catch {
        await new Promise((r) => setTimeout(r, 1200));
      }
    }
    $("#configMsg").textContent = ok ? "已重启，配置生效" : "重启指令已发送，稍后手动刷新";
    $("#configMsg").className = "msg " + (ok ? "ok" : "");
    await loadConfig().catch(() => {});
  } catch (e) {
    $("#configMsg").textContent = "重启失败：" + e.message;
    $("#configMsg").className = "msg err";
  } finally {
    btn.disabled = false;
  }
}

$("#restart").addEventListener("click", restartPlugin);

await B.ready();
await refresh();
await pollJob();
await pollMaint("vec");
await pollMaint("tr");
await checkVectors().catch(() => {});
await checkTranslations().catch(() => {});
await loadConfig().catch((e) => {
  $("#configForm").innerHTML = `<p class="empty">读取配置失败：${esc(e.message)}</p>`;
});
