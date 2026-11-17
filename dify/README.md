# 600076 · Just for Fun：脑洞辩论主持人（Dify × PowerContext）

- **questionId**：600076，满分 20
- **分支 / 目录**：`2026-competition-ecosystem` / `dify/`
- **技术选型**：Dify（核心流程编排） + **PowerContext**（记忆与上下文，满足「至少一种」要求）
- **提交产物**：`dsl.yml`（Dify DSL）、本运行说明、`verification.md`（验证样例）、`.env.example`

## 作品是什么

用户输入两个八竿子打不着的东西（例如「咖啡 vs 闹钟，谁更适合当班长」），
Dify Chatflow 里的 AI 主持人组织三轮越来越离谱但逻辑自洽的辩论，并请用户投票。
每一轮的辩题与结果都写入 PowerContext；下一轮开场时先读取历史记忆，
自然带出「上次你站了冰箱那边」，形成越玩越懂你的连续体验。

## 为什么满足技术结合要求

| 组成 | 承担的作用 | 可核对的证据 |
| --- | --- | --- |
| Dify（必选） | 整个流程编排：开始 → HTTP 读取 → LLM → HTTP 写回 → 回答 | `dsl.yml` 的 graph.nodes / edges |
| PowerContext | 保存每轮辩题与偏好，并在**后续运行**中读取、使用 | `pc-load` 调 `/v1/context/prepare`，响应注入 LLM system prompt；`verification.md` 记录跨会话复现 |

题面允许 Dify + PowerContext 的组合，不额外要求 seekdb。
这里选择 PowerContext 的官方 HTTP API 接入（Dify 的 HTTP Request 节点直接调用）：
- 读取：`POST /v1/context/prepare`
- 写入：`POST /v1/memory/remember`

不依赖任何自定义插件，评审导入 DSL 后只需配置 PowerContext 地址与 scope_id。

## 运行说明

1. 环境：Dify ≥ 0.10（DSL `version: 0.1.5`），一个可用的 LLM Provider。
   若没有 `gpt-4o-mini`，在 `llm-host` 节点中把 `model.name` 换成账号内任意可用模型。
2. 启动 PowerContext（任选其一）：
   - 官方服务：按 PowerContext 文档启动 HTTP API，默认 `http://127.0.0.1:8000`；
   - 或本仓库主线分支自带的 Runtime：`2026-competition` 分支的 `src/powercontext`。
3. 创建稳定 Scope：

   ```bash
   curl -s -H 'Content-Type: application/json' \
     -d '{"title":"Dify debate demo","summary":"Debate app memory","idempotency_key":"dify-debate-demo"}' \
     http://127.0.0.1:8000/v1/scopes
   ```

   把返回的 `scope_id` 填进 Dify 环境变量 `POWERCONTEXT_SCOPE_ID`。
4. 复制 `.env.example` 中的变量名，在 Dify 应用「环境变量」里设置
   `POWERCONTEXT_URL`、`POWERCONTEXT_SCOPE_ID`。若 Dify 与 PowerContext 都在 Docker 中，
   把主机名改成 compose 服务名（如 `http://powercontext:8000`）。
5. Dify → 「导入 DSL」→ 选择 `dsl.yml` → 发布。
6. 在应用界面输入建议问题之一，按 `verification.md` 走一遍完整样例。

> 配置示例只用占位值，不含任何真实 API Key、密码或凭据。
