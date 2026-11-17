# 一页技术说明 · SDD 上下文管理系统

## 模块结构

- `context_module.py`：CLI 入口，提供 `init / context / save / next` 四个调用入口。
- `agent/minimax_edit.py`：把当前上下文交给 MiniMax，保存真实请求与响应到 `devlog/`。
- `sample_project/`：一个有单元测试的小型业务工程，用于验证「修改前失败、修改后通过」。
- `state/`：SQLite 工程资料索引与状态文件（运行时生成）。

## 上下文选择方式

1. 只选 `active=1` 的规格条目，避免过时规格污染。
2. 关联代码与测试按路径排序全部纳入，保证「规格—代码—测试」可核对。
3. 已完成事项从本地 SQLite 与 PowerContext `/v1/context/prepare` 合并；
   PowerContext 返回的内容标记为只读历史，当前指令优先。
4. 更新规格并重新 `init` 后，输出随规格变化，不是硬编码答案。

## 技术分工

| 技术 | 实际作用 |
| --- | --- |
| MiniMax | 接收真实上下文，生成代码修改；`devlog/round-01.md` 保存真实调用记录 |
| SQLite（可替换为 seekdb/OceanBase） | 保存规格、代码、测试索引与任务进度；查询结果直接进入上下文 |
| PowerContext | `save` 写任务状态，`next` 从 `/v1/context/prepare` 读回并用于后续上下文 |

## 已知限制

- 仓库中不包含真实 API Key；MiniMax 调用必须由评审者用本人账号复现。
- 示例工程故意保留一个边界缺陷，只有按规格修改后测试才全绿。
