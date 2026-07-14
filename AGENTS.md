# Project instructions

- `public/workflows/*.yml` 是当前 Dify 实现的事实源；不要把概念图中尚未落地的节点画成已实现。
- App A 不得读取原论文、回归结果或任何 `02_hidden_reference` 内容；隐藏材料只属于 App B。
- 六类方法节点和两类执行器是互斥路由后汇合，不是并行执行。
- `execution_status=success` 不代表 `scientific_status=valid`，前端和未来后端必须分开显示。
- H1/H2/H3 在正式后端中必须是可停止、退回和等待的状态机，不能只记录评论后继续。
- 同级 `../Agent Laboratory` 是外部 Benchmark 基线；不要向其中加入 HypoWeaver 的 Critic、冻结或 ClaimLedger 逻辑。
- 修改解析器、阶段映射或 YAML 后，必须运行 `npm test` 和 `npm run build`。
- 不提交 `node_modules/`、`dist/`、测试截图、运行产物、密钥或本机路径数据。
