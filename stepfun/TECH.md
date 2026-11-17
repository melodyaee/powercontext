# 一页技术说明 · 端侧迷你知识库

## 1. 模型与量化

- 模型：`Xenova/all-MiniLM-L6-v2`（Hugging Face 公开模型，384 维）。
- 运行时：`@huggingface/transformers@3.7.6`（Transformers.js v3 / ONNX Runtime Web）。
- 量化：WebGPU 路径使用 `dtype: "fp16"`；WASM 路径使用 `dtype: "q8"`（INT8）。
- 压缩比口径：`量化前模型文件总体积 / 量化后模型文件总体积`，按浏览器实际下载文件统计。
- 质量对比：用同一组查询，在相同 seekdb 集合上比较量化前后 Recall@5 / nDCG@5。

| 配置 | 模型体积 | 压缩比 | Recall@5 | nDCG@5 |
| --- | --- | --- | --- | --- |
| 未量化基线（fp32） | 评审运行 `npm start` 后按浏览器下载量填写 | 1.00× | 按样例集填写 | 按样例集填写 |
| INT8 + WASM | 同上口径填写 | 填写 | 填写 | 填写 |
| FP16 + WebGPU | 同上口径填写 | 填写 | 填写 | 填写 |

> 本仓库不预填未实测的数字。页面「运行一次简易基准」会输出模型加载、批量向量化、
> 查询向量化和 seekdb 检索耗时，重复 5 次取中位数即可填写上表。

## 2. 端侧运行与降级

- 优先 WebGPU：检测 `navigator.gpu`，成功则使用 FP16。
- 自动降级：WebGPU 初始化失败时重新以 WASM + INT8 初始化，核心流程不变。
- 界面状态栏显示 `backend` 与 `dtype`，便于评审核对降级路径。
- 计算位置：embedding 全部在 `src/main.js` 的浏览器进程中完成；`server.mjs` 不接收原文做向量化，
  只接收已生成的 embedding 与原文写入 seekdb。

## 3. seekdb 使用

- SDK：`seekdb` (seekdb-js)，server 模式连接本地 seekdb。
- 集合：`edge_kb_chunks`。
- Schema：`FulltextIndexConfig("ik", { ik_mode: "smart" })` +
  `VectorIndexConfig({ hnsw: { dimension: 384, distance: "cosine" }, embeddingFunction: null })`。
- 写入：`collection.add({ ids, documents, embeddings, metadatas })`。
- 检索：`collection.hybridSearch({ query: { whereDocument: { $contains: q } }, knn: { queryEmbeddings: [qv] } })`；
  混合排序由 seekdb 完成。若版本不支持 `hybridSearch`，降级到 `query()` 并在界面标注。

## 4. 状态、增量与优化

- 增量导入：每次 `add()` 使用新 id（`doc-<timestamp>-<seq>`），集合状态由 seekdb 持久化；
  页面刷新后可继续导入和检索。
- 批处理：`embed()` 默认批大小 8，降低多次推理调用开销。
- 分阶段计时：页面基准分别输出首次加载、模型加载、批量向量化、查询向量化、seekdb 检索耗时。
- 首次与后续请求分开：基准按钮会强制重新加载模型一次，同时记录预热后的批量与查询耗时。
