# GreenFinance Plot Agent 合并说明

来源：`carolzhu-jr/GreenFinance_Plot_Agent`，基线提交
`07820bd3aef18e84b8a4e2290e03d1b7ef666ade`（2026-07-21）。

当前同仓职责：

- 本目录由 Task4 同学维护图形配方与渲染质量；
- `coefficient_forest` 和 `sample_flow` 已作为进程内渲染器接入；
- `hypoweaver/visualization.py` 由主工作流维护输入授权、Claim/Execution 追溯和封存；
- Scientific Writer 继续负责正文，绘图模块不生成或提高研究结论强度。

没有照搬的上游内容：

- 独立 FastAPI、Docker 和端口配置（同仓调用不需要）；
- `TEST_DATA` 缺省回退（真实工作流禁止用测试值补造图）；
- Qwen 图题/图注生成（写作职责仍在 HypoWeaver）；
- 通过 `exec` 执行字符串模板的实验性模板库；
- `case_006/02_hidden_reference` 及任何隐藏参考材料。
