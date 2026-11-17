const LS = "qoder-literacy-pc";
let cfg = JSON.parse(localStorage.getItem(LS) || "{}");
const el = (id) => document.getElementById(id);

function load() {
  if (cfg.url) el("pc-url").value = cfg.url;
  if (cfg.scope) el("pc-scope").value = cfg.scope;
  if (cfg.url && cfg.scope) refresh();
}
function saveCfg() {
  cfg = { url: el("pc-url").value.trim().replace(/\/$/, ""), scope: el("pc-scope").value.trim() };
  localStorage.setItem(LS, JSON.stringify(cfg));
  el("status").textContent = cfg.url && cfg.scope ? "配置已保存" : "请填写完整配置";
  if (cfg.url && cfg.scope) refresh();
}
async function post(path, body) {
  if (!cfg.url || !cfg.scope) throw new Error("请先保存服务地址和 Scope ID");
  const r = await fetch(cfg.url + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_id: cfg.scope, ...body }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message || `HTTP ${r.status}`);
  return data;
}
async function addNote() {
  const text = el("note").value.trim();
  if (!text) return;
  try {
    await post("/v1/memory/remember", { kind: "decision", text, reason: "Qoder Sites literature desk" });
    el("note").value = "";
    el("status").textContent = "已写入 PowerContext";
    refresh();
  } catch (e) { el("status").textContent = `写入失败：${e.message}`; }
}
async function refresh() {
  try {
    const data = await post("/v1/context/prepare", { query: "我的阅读关注点", max_bytes: 4000 });
    const box = el("history");
    box.innerHTML = "";
    if (!data.content) { box.textContent = "暂无历史记忆。写入一条记录后再看。"; return; }
    for (const line of String(data.content).split(/\n+/).filter(Boolean).slice(-8)) {
      const d = document.createElement("div");
      d.className = "history-item";
      d.textContent = line;
      box.appendChild(d);
    }
    el("status").textContent = "历史记忆已读取";
  } catch (e) { el("history").textContent = `读取失败：${e.message}`; }
}
el("connect").onclick = saveCfg;
el("save").onclick = addNote;
load();
