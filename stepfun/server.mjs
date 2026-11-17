import http from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { SeekdbClient, Schema, FulltextIndexConfig, VectorIndexConfig } from "seekdb";

const COLLECTION = "edge_kb_chunks";
const clients = new Map();

function clientFor(config) {
  const key = JSON.stringify(config);
  if (!clients.has(key)) {
    clients.set(key, new SeekdbClient({
      host: config.host || "127.0.0.1",
      port: Number(config.port || 2881),
      user: config.user || "root",
      password: config.password || "",
      database: config.database || "edge_kb",
    }));
  }
  return clients.get(key);
}

async function getCollection(config, dimension) {
  const client = clientFor(config);
  const schema = new Schema({
    fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
    vectorIndex: new VectorIndexConfig({
      hnsw: { dimension: dimension || 384, distance: "cosine" },
      embeddingFunction: null,
    }),
  });
  try {
    return await client.getCollection({ name: COLLECTION });
  } catch {
    return await client.createCollection({ name: COLLECTION, schema });
  }
}

function json(res, code, body) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*" });
  res.end(JSON.stringify(body));
}

async function handleApi(req, res) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  let body = {};
  try { body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"); } catch {}
  const config = body.config || {};

  if (req.url === "/api/collections/ensure") {
    const col = await getCollection(config, body.dimension);
    return json(res, 200, { collection: COLLECTION, name: col?.name ?? COLLECTION });
  }
  if (req.url === "/api/docs/add") {
    const col = await getCollection(config, body.embeddings?.[0]?.length);
    await col.add({
      ids: body.ids,
      documents: body.documents,
      embeddings: body.embeddings,
      metadatas: body.documents.map((_, i) => ({ source: "browser-local-embedding", seq: i })),
    });
    return json(res, 200, { added: body.ids.length });
  }
  if (req.url === "/api/search") {
    const col = await getCollection(config, body.embedding?.length);
    let hits;
    try {
      const r = await col.hybridSearch({
        query: { whereDocument: { $contains: body.query } },
        knn: { queryEmbeddings: [body.embedding] },
        nResults: body.nResults || 5,
      });
      hits = (r.ids?.[0] ?? []).map((id, i) => ({
        id,
        document: r.documents?.[0]?.[i] ?? "",
        metadata: r.metadatas?.[0]?.[i] ?? null,
      }));
    } catch {
      const r = await col.query({ queryEmbeddings: [body.embedding], nResults: body.nResults || 5 });
      hits = (r.ids?.[0] ?? []).map((id, i) => ({ id, document: r.documents?.[0]?.[i] ?? "" }));
    }
    return json(res, 200, { hits });
  }
  return json(res, 404, { error: "unknown api" });
}

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".txt": "text/plain" };

http.createServer(async (req, res) => {
  try {
    if (req.url?.startsWith("/api/")) return await handleApi(req, res);
    const rel = req.url === "/" ? "index.html" : decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "");
    const file = join(process.cwd(), normalize(rel));
    if (!file.startsWith(process.cwd())) { res.writeHead(403); return res.end(); }
    const data = await readFile(file);
    res.writeHead(200, { "Content-Type": `${MIME[extname(file)] || "application/octet-stream"}; charset=utf-8` });
    res.end(data);
  } catch (e) {
    json(res, 500, { error: e.message });
  }
}).listen(4173, () => console.log("Edge KB running at http://127.0.0.1:4173"));
