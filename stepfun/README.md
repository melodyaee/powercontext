# 600073 · 端侧 AI 推理性能优化与 seekdb 混合检索

- **questionId**：600073，满分 20
- **分支 / 目录**：`2026-competition-ecosystem` / `stepfun/`
- **提交产物**：可运行 Web 应用 + 完整源码 + 一页技术说明（`TECH.md`）

## 运行

```bash
cd stepfun
npm install
npm start
# 浏览器打开 http://127.0.0.1:4173
```

1. 启动一个本地 seekdb server（或把配置指向已有实例），默认 `127.0.0.1:2881`。
2. 页面「运行状态」填 host/port/user/password/database，点「连接 / 建集合」。
3. 点「载入示例语料」→「向量化并写入 seekdb」。
4. 输入查询，点「检索」。页面显示当前推理后端与 dtype。

## 约束落实

- `src/main.js` 用 `@huggingface/transformers` 在**浏览器本地**做文档与查询 embedding，
  没有调用任何云端推理 API。
- `navigator.gpu` 可用时先尝试 WebGPU + FP16；失败自动降级 WASM + INT8(q8)，
  界面状态栏明确显示当前 `backend` 和 `dtype`。
- `server.mjs` 只负责用 seekdb-js 存取：集合同时建 `FulltextIndexConfig` 与
  `VectorIndexConfig(embeddingFunction: null)`；检索优先 `hybridSearch()`，
  由 seekdb 完成向量 + 全文混合检索，不在客户端拼两路结果。
- 模型来源与版本、量化方式、体积/质量/耗时对比方法见 `TECH.md`。

## 示例语料

`src/main.js` 的 `SAMPLE` 常量提供 5 段可直接复现的语料；无需额外下载数据。
