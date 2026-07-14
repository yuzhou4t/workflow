# Project instructions

- `backend/src/hypoweaver/definition.py` 与严格领域 Schema 是正式运行时事实源；`public/workflows/*.yml` 仅保留为历史设计参考，运行时不得解析。
- App A 不得读取原论文、回归结果或任何 `02_hidden_reference` 内容；隐藏材料只属于 App B。
- 七类方法节点和两类执行器是互斥路由后汇合；假设拆解/数据画像及四类 Critic 才是并行后汇合。
- `execution_status=success` 不代表 `scientific_status=valid`，前端和未来后端必须分开显示。
- H1/H2/H3 在正式后端中必须是可停止、退回和等待的状态机，不能只记录评论后继续。
- 同级 `../Agent Laboratory` 是外部 Benchmark 基线；不要向其中加入 HypoWeaver 的 Critic、冻结或 ClaimLedger 逻辑。
- 修改状态机、Schema 或执行边界后，必须运行后端 `unittest`、`npm test` 和 `npm run build`。
- 不提交 `node_modules/`、`dist/`、测试截图、运行产物、密钥或本机路径数据。
