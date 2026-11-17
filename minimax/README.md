# 600077 · 规有所依：SDD 上下文管理系统（MiniMax × seekdb/OceanBase × PowerContext）

- **questionId**：600077，满分 20，六项必做全部通过才得分
- **分支 / 目录**：`2026-competition-ecosystem` / `minimax/`
- **提交产物**：本目录全部文件（模块源码、Agent 接入、样例工程、验证样例、开发记录模板）

## 快速开始

```bash
cd minimax
python context_module.py init
python context_module.py context --task "为过期订单增加退款边界"
python -m unittest discover -s sample_project -v     # 修改前：2 passed / 1 failed
```

接入 PowerContext（可选但题面必做，建议配置）：

```bash
export POWERCONTEXT_URL=http://127.0.0.1:8000
export POWERCONTEXT_SCOPE_ID=<从 /v1/scopes 创建>
python context_module.py save --step S1 --status done --detail "已定位过期订单边界"
python context_module.py next --task "继续修复退款边界"
```

用真实 MiniMax 账号完成代码修改并记录：

```bash
export MINIMAX_API_KEY=<你的密钥>       # 不要提交
python agent/minimax_edit.py            # 写出 devlog/round-01.md
# 审核 MiniMax 给出的 diff，应用到 sample_project/eligibility.py
python -m unittest discover -s sample_project -v   # 修改后：3 passed
```

## 六项必做对照

| 验收项 | 对应材料 |
| --- | --- |
| 能够运行 | `context_module.py init/context/next/save` |
| MiniMax 实际参与 | `agent/minimax_edit.py` + `devlog/round-01.md`（真实 API 响应） |
| 上下文正确 | `state/context-latest.md`；更新 `sample_project/spec.md` 后重跑 init/context |
| 数据库实际参与 | `context_module.py` 的 SQLite 工程资料索引；可替换为 OceanBase/seekdb 连接 |
| 状态能够复用 | `save` 写入 PowerContext，`next` 从 `/v1/context/prepare` 读回 |
| 代码修改有效 | `sample_project/test_eligibility.py` 修改前 1 失败、修改后全过 |

> 注意：`devlog/round-01.md` 必须由**你本地真实调用 MiniMax 后生成**，
> 不能在仓库里伪造模型响应；这是本题的硬性核对点。
