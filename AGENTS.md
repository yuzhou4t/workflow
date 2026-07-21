# Project instructions

- `backend/src/hypoweaver/definition.py` 与严格领域 Schema 是正式运行时事实源；`public/workflows/*.yml` 仅保留为历史设计参考，运行时不得解析。
- App A 不得读取原论文、回归结果或任何 `02_hidden_reference` 内容；隐藏材料只属于 App B。
- 七类方法节点和两类执行器是互斥路由后汇合；假设拆解/数据画像及四类 Critic 才是并行后汇合。
- `execution_status=success` 不代表 `scientific_status=valid`，前端和未来后端必须分开显示。
- H1/H2/H3 在正式后端中必须是可停止、退回和等待的状态机，不能只记录评论后继续。
- Test DAG、Evidence Registry、确定性 Claim Gate 与 Manuscript IR 是代码拥有的科学约束；不得退回为由 LLM 自报检查完成、自由升级主张或手抄统计数字。
- common-executor 只能在 H2 结果前停止；恢复时只能注入与原 AnalysisRequest 精确哈希绑定的密封结果，禁止重新规划、偷看结果或绕过 Claim Gate。
- 同级 `../Agent Laboratory` 是外部 Benchmark 基线；不要向其中加入 HypoWeaver 的 Critic、冻结或 ClaimLedger 逻辑。
- 六系统能力板的 `12/12` 只表示接口已接通，不表示科研能力、案例支持或科学有效性；HypoWeaver 原生流程不能完整交付 Case 010 的 CR/AR 双结局仍是明确能力缺口。
- Benchmark 的两个榜单和两个输入视图不得混分；Case 004/010 配对冻结门未全部通过时，formal 必须保持关闭，不能退化为单案例正式榜。
- 修改状态机、Schema 或执行边界后，必须运行后端 `unittest`、`npm test` 和 `npm run build`。
- 不提交 `node_modules/`、`dist/`、测试截图、运行产物、密钥或本机路径数据。
