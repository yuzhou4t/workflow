# Case 005 独立审计负责人

目标：不重复调用模型，独立确认 Case 005 的预检、矩阵和终态证据完整。

检查 12 个 native、12 个 common manifest 是否唯一且覆盖六系统、双视图、双榜单；检查失败单元
是否保留终态证据，common result/receipt 是否成对，是否存在删除、选择性重跑或模型身份漂移。

输出一份问题清单和最终状态：

- `complete`：24 单元均有合格终态证据；
- `incomplete`：缺单元或产物；
- `needs_coordinator_adjudication`：疑似基础设施或完整性问题。

不要读取隐藏参考、计算最终排名或自行允许重跑。
