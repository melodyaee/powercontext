# 600078 · 文献速读台（Qoder × PowerContext）

- **questionId**：600078，满分 20
- **分支 / 目录**：`2026-competition-ecosystem` / `qoder/`
- **提交产物**：项目网站网址（`workUrl` 必填）、代码仓库、运行说明、验证样例、Qoder 开发记录

## 项目简介

一个静态单页应用，帮助同学把论文摘要、实验记录和待办集中管理：
用户输入阅读记录后写入 PowerContext；下次打开时通过 `/v1/context/prepare`
读回关注点并展示，实现「越用越懂你」的阅读助手。

## 本地运行

```bash
cd qoder
python -m http.server 4173
# 打开 http://127.0.0.1:4173
```

1. 启动 PowerContext，创建 Scope：

   ```bash
   curl -s -H 'Content-Type: application/json' \
     -d '{"title":"Literacy desk","summary":"Qoder demo","idempotency_key":"qoder-literacy"}' \
     http://127.0.0.1:8000/v1/scopes
   ```

2. 页面填入服务地址与 `scope_id`，点「保存配置」。
3. 写一条阅读记录 → 点「写入记忆」→ 刷新页面 → 历史区域应出现刚才的内容。

## 技术结合

| 组成 | 作用 |
| --- | --- |
| PowerContext | 保存阅读记录（`/v1/memory/remember`），读取关注点（`/v1/context/prepare`）并在页面展示 |
| Qoder | 由参赛者用 Qoder 完成本项目开发并在 Qoder Sites 发布；记录模板见 `QODER_RECORD_TEMPLATE.md` |

## 必须由参赛者完成（无法代劳）

1. 用**你自己的 Qoder 账号**开发或至少重构本项目，保留关键对话截图/录屏与账号 UID。
2. 在 Qoder 中通过 **Sites** 发布 `qoder/` 目录，可见范围设为「任何人都可访问」。
3. 把发布后的完整网址填进提测弹窗的 `workUrl` 字段。
4. 按 `VERIFICATION.md` 验证并把真实记录写入 `QODER_RECORD_TEMPLATE.md`。

> 我不能伪造 Qoder 开发记录或 Sites 网址；这三项是本题硬性核对点。
