# Agent Prompt 学习材料摘要（2026-11-16）

本目录只有两类内容：学生本人原文，以及逐字保留的真实命令输出。
conversation.json 的 agent_worklog / experiments / retrospective 均标注 source=agent，
按评分规则不计入学生证据。完整 stdout：/student_evidence/raw_output_1116.txt。

| 实例 | 指南题号 | 主题 | 引用实验 | 学生原文 | 实际结果 | 解释 |
| --- | --- | --- | --- | --- | --- | --- |
| S1 / E-A | 第 2 题 | float32 量化导致距离坍缩 | /experiments/E2 | /student_evidence/instances/1/student_quote.txt | /student_evidence/instances/1/result_quote.txt | /student_evidence/instances/1/interpretation.txt |
| S2 / E-B | 第 3 题 | 距离并列时的主键升序规则 | /experiments/E2 | /student_evidence/instances/2/student_quote.txt | /student_evidence/instances/2/result_quote.txt | /student_evidence/instances/2/interpretation.txt |
| S3 / E-C | 第 7 题 | 窗口边界必须纳入全部并列行 | /experiments/E8 | /student_evidence/instances/3/student_quote.txt | /student_evidence/instances/3/result_quote.txt | /student_evidence/instances/3/interpretation.txt |
| S4 / E-D | 第 8 题 | 过滤开销来源与窗口增长 | /experiments/E7, /experiments/E9 | /student_evidence/instances/4/student_quote.txt | /student_evidence/instances/4/result_quote.txt | /student_evidence/instances/4/interpretation.txt |

四条均有预测、实际结果与解释修正，且都跑出了真实结果，属「已验证」实例，非 0.5 档计划。
采集脚本由助手在比赛 VM 上代跑；学生对照输出提交上述原文，助手未代写。

## 一句话要点

S1：float32 在 2^24 附近无法区分相邻整数，16777217 被舍入成 16777216，L2=0。
S2：距离并列须按主键升序，否则 vector_ties_and_filter 期望的 id=3 会变成 id=8。
S3：窗口边界上的并列行必须全部纳入，window_check.py 66/66 证明快速路径与全扫描一致。
S4：过滤开销在逐行回读而非条件判断，窗口化后 FILTERED 7.06 ms/query 反略快于 NO_FILTER 7.26。
