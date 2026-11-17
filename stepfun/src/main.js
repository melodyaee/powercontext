import { env, pipeline } from "@huggingface/transformers";

env.allowLocalModels = false;

const MODEL = "Xenova/all-MiniLM-L6-v2";
const DIM = 384;
const SAMPLE = `seekdb 支持向量、全文与结构化条件结合的混合检索。
PowerContext 为 AI Agent 保存任务上下文与长期记忆。
Transformers.js 可以在浏览器本地运行 embedding 模型。
INT8 量化可以减小模型体积并降低端侧推理开销。
WebGPU 可用时优先使用 GPU，否则自动降级到 WASM。`;

const el = (id) => document.getElementById(id);
let extractor = null;
let backend = "wasm";
let dtype = "q8";
let dbReady = false;
let lastModelLoadMs = 0;

function setStatus(text) { el("status").textContent = text; }

async function ensureExtractor(force = false) {
  if (extractor && !force) return extractor;
  const webgpu = !!navigator.gpu;
  const candidates = webgpu
    ? [{ device: "webgpu", dtype: "fp16" }, { device: "wasm", dtype: "q8" }]
    : [{ device: "wasm", dtype: "q8" }];
  let lastErr;
  for (const c of candidates) {
    try {
      const started = performance.now();
      extractor = await pipeline("feature-extraction", MODEL, {
        device: c.device,
        dtype: c.dtype,
        progress_callback: (p) => {
          if (p.status === "progress") el("status").textContent = `模型下载 ${p.file ?? ""} ${Math.round(p.progress ?? 0)}%`;
        },
      });
      backend = c.device;
      dtype = c.dtype;
      lastModelLoadMs = performance.now() - started;
      setStatus(`模型就绪：${MODEL} / ${dtype} / ${backend}`);
      return extractor;
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr ?? new Error("无法初始化端侧模型");
}

async function embed(texts, batchSize = 8) {
  const pipe = await ensureExtractor();
  const out = [];
  for (let i = 0; i < texts.length; i += batchSize) {
    const batch = texts.slice(i, i + batchSize);
    const tensors = await pipe(batch, { pooling: "mean", normalize: true });
    const rows = tensors.tolist ? tensors.tolist() : [tensors];
    for (const row of rows) out.push(Array.from(row).slice(0, DIM));
    el("import-progress").textContent = `已向量化 ${out.length}/${texts.length}`;
  }
  return out;
}

function dbConfig() {
  return {
    host: el("db-host").value,
    port: Number(el("db-port").value),
    user: el("db-user").value,
    password: el("db-pass").value,
    database: el("db-name").value,
  };
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${path} HTTP ${res.status}`);
  return data;
}

el("init-db").onclick = async () => {
  try {
    setStatus("连接 seekdb…");
    const r = await api("/api/collections/ensure", { config: dbConfig(), dimension: DIM });
    dbReady = true;
    setStatus(`seekdb 就绪：${r.collection}`);
  } catch (e) { setStatus(`失败：${e.message}`); }
};

el("load-sample").onclick = () => { el("docs").value = SAMPLE; };

el("import-docs").onclick = async () => {
  try {
    if (!dbReady) throw new Error("请先连接 seekdb");
    const docs = el("docs").value.split(/\n+/).map((s) => s.trim()).filter(Boolean);
    if (!docs.length) throw new Error("没有可导入文本");
    const t0 = performance.now();
    const embeddings = await embed(docs);
    const tEmbed = performance.now() - t0;
    const t1 = performance.now();
    const r = await api("/api/docs/add", {
      config: dbConfig(),
      ids: docs.map((_, i) => `doc-${Date.now()}-${i}`),
      documents: docs,
      embeddings,
    });
    const tDb = performance.now() - t1;
    setStatus(`已写入 ${r.added} 段：向量化 ${tEmbed.toFixed(0)}ms / 写入 ${tDb.toFixed(0)}ms`);
  } catch (e) { setStatus(`导入失败：${e.message}`); }
};

el("search").onclick = async () => {
  try {
    if (!dbReady) throw new Error("请先连接 seekdb");
    const q = el("query").value.trim();
    if (!q) throw new Error("请输入查询");
    const t0 = performance.now();
    const [qv] = await embed([q]);
    const tEmbed = performance.now() - t0;
    const t1 = performance.now();
    const r = await api("/api/search", { config: dbConfig(), query: q, embedding: qv, nResults: 5 });
    const tDb = performance.now() - t1;
    const box = el("results");
    box.innerHTML = "";
    for (const hit of r.hits ?? []) {
      const d = document.createElement("div");
      d.className = "result";
      d.innerHTML = `<div>${escapeHtml(hit.document ?? "")}</div><div class="meta">id=${escapeHtml(hit.id ?? "")} · 混合检索（seekdb）</div>`;
      box.appendChild(d);
    }
    setStatus(`检索完成：查询向量化 ${tEmbed.toFixed(0)}ms / seekdb ${tDb.toFixed(0)}ms / ${backend} / ${dtype}`);
  } catch (e) { setStatus(`检索失败：${e.message}`); }
};

el("bench").onclick = async () => {
  try {
    const coldStart = performance.now();
    await ensureExtractor(true);
    const firstLoad = performance.now() - coldStart;
    const docs = SAMPLE.split(/\n+/);
    const t0 = performance.now();
    const vectors = await embed(docs);
    const batchMs = performance.now() - t0;
    const t1 = performance.now();
    const [qv] = await embed(["混合检索如何工作？"]);
    const queryMs = performance.now() - t1;
    const t2 = performance.now();
    await api("/api/search", { config: dbConfig(), query: "混合检索", embedding: qv, nResults: 5 });
    const dbMs = performance.now() - t2;
    const report = {
      model: MODEL,
      dtype,
      backend,
      vectorDimension: vectors[0]?.length ?? DIM,
      firstLoadMs: Math.round(firstLoad),
      modelLoadMs: Math.round(lastModelLoadMs),
      batchEmbedMs: Math.round(batchMs),
      queryEmbedMs: Math.round(queryMs),
      seekdbHybridMs: Math.round(dbMs),
      notes: "单次值仅作演示；正式结果请重复 5 次取中位数，并分别记录 WebGPU / WASM。",
    };
    el("bench-out").textContent = JSON.stringify(report, null, 2);
  } catch (e) { el("bench-out").textContent = e.message; }
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
