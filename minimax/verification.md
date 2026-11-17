# 验证样例 · SDD 上下文管理系统

## 任务

为 `sample_project/eligibility.py` 增加「过期订单不得退款」边界，并保持「已付款未过期订单可退款」的行为不变。

## 初始数据与必要来源

- 规格：`sample_project/spec.md` 的 SPEC-001（必须出现在上下文中）
- 代码：`sample_project/eligibility.py`（必须出现）
- 测试：`sample_project/test_eligibility.py`（必须出现）
- 初始状态：`state/engineering.db` 为空（执行 `init` 后导入资料，progress 为空）

## 操作步骤

1. `python context_module.py init`
2. `python context_module.py context --task "为过期订单增加退款边界"` → 保存输出为 `state/context-latest.md`
3. `python -m unittest discover -s sample_project -v` → 记录修改前结果（预期 1 failed）
4. 设置 `MINIMAX_API_KEY`，执行 `python agent/minimax_edit.py` → 生成 `devlog/round-01.md`
5. 审核并将 MiniMax 输出中的补丁应用到 `sample_project/eligibility.py`
6. `python -m unittest discover -s sample_project -v` → 记录修改后结果（预期 3 passed）
7. `python context_module.py save --step S1 --status done --detail "..."`
8. 删除 `state/engineering.db` 中的 progress 行或换新目录，执行 `python context_module.py next --task "..."`，
   确认输出包含 PowerContext 读回的状态。

## 预期结果

- 上下文输出同时包含 SPEC-001、`eligibility.py`、`test_eligibility.py`。
- 修改 SPEC-001 内容并重新 `init && context` 后，输出使用新规格，不是固定答案。
- 修改前后测试集完全一致；目标测试由失败变通过，回归测试始终通过。
- `devlog/round-01.md` 中的请求上下文与 `context-latest.md` 一致，响应与最终 diff 对应。

## 重置方法

删除 `state/` 目录后重新执行 `init`；`devlog/` 可保留作为历史记录。
