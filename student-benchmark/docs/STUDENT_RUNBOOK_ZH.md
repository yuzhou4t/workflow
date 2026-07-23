# Case 005/007 同学运行手册

## 1. 小组角色

每个 Case 只有一个付费执行批次：

- 执行负责人：保管冻结工作区，在获准机器上配置自己的 API Key，完成预检和 24 单元运行；
- 审计负责人：检查预检报告、manifest、终态证据、日志和回传包，不重复运行相同批次。

若希望两个人各自调用模型，必须在运行前另行预注册种子、预算和聚合方法，并重新冻结 release；
不能看到第一批结果后再补跑。

## 2. API 配置

只使用正式 suite 冻结的供应商、endpoint 和模型。运行：

```bash
python3 scripts/case_operator.py configure-api \
  --case <005或007> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

Key 通过隐藏输入读取，写入 suite 指定的 `runtime_config`，权限设为 `0600`。不要把 Key 写入
命令参数、shell 历史、`.env`、Issue、聊天截图或 Git。

## 3. 离线预检

```bash
python3 scripts/case_operator.py preflight \
  --case <005或007> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json \
  --report /absolute/path/to/private-preflight-report.json
```

预检不会调用外部模型。它核验：

- Harness commit 和工作树清洁状态；
- protocol、suite、案例路径和 24 单元矩阵；
- 上游合同测试；
- API 配置权限、模型和 endpoint，但不输出 Key；
- release lock 和 hash-bound receipt。

只有 `external_execution_ready=true` 才能运行。若数据控制方确认、receipt 或
`execution_enabled=true` 任一缺失，脚本必须拒绝。

## 4. 正式执行

```bash
python3 scripts/case_operator.py run \
  --case <005或007> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

执行顺序是 native discovery 六系统、native aligned 六系统、common discovery 六系统、
common aligned 六系统。

禁止：

- 修改 prompt、预算、系统代码、模型或数据后继续同一批次；
- 删除失败目录再跑；
- 只重跑表现不好的系统；
- 合并 discovery/aligned 或 native/common 分数；
- 把结果提交到 GitHub。

工作流失败可以封存并继续；完整性错误、release 漂移、模型身份漂移、缺失终态产物或未裁决的
基础设施异常必须停止整个批次。

## 5. 审计负责人验收

审计负责人至少核对：

- 24 个唯一 `cell_manifest.json`，12 native + 12 common；
- 每个单元的 Case、视图、系统、榜单、种子和哈希与冻结计划一致；
- 每个终态都有 `normalized_result.json` 和 `evidence_manifest.json`；
- 失败单元保留原始失败证据，没有删除或选择性重跑；
- common 单元的 result/receipt 成对存在；
- Case 007 的空间权重、映射和补充资产哈希进入 manifest/receipt；
- API Key、隐藏参考和真实数据未进入日志或回传说明。

审计负责人提交问题清单和“完整/不完整/需要负责人裁决”的结论，不自行解锁重跑，也不读取隐藏
参考或发布排名。
