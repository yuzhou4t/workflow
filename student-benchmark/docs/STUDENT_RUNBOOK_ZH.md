# Case 005/007 同学运行手册

## 1. 每个 Case 的 1 + 5 系统分工

每个 Case 的六个系统拆成两个互斥分组：

- `hypoweaver`：只运行 HypoWeaver，4 个单元；
- `baselines`：运行 Agent Laboratory、data-to-paper、Direct Qwen、
  Qwen Code Agent + Writer 和 DeepScientist，20 个单元。

两组使用完全相同的 Case 版本、双视图、双榜单、种子、模型和预算。双方都完成并封存前，不得互看
中间结果，也不得根据对方表现修改系统或选择性重跑。两份结果合并后必须恰好覆盖六系统的 24 个
唯一单元。

## 2. API 配置

只使用正式 suite 冻结的供应商、endpoint 和模型。运行：

```bash
python3 scripts/case_operator.py configure-api \
  --case <005或007> \
  --assignment <hypoweaver或baselines> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

Key 通过隐藏输入读取，写入 suite 指定的 `runtime_config`，权限设为 `0600`。不要把 Key 写入
命令参数、shell 历史、`.env`、Issue、聊天截图或 Git。

## 3. 离线预检

```bash
python3 scripts/case_operator.py preflight \
  --case <005或007> \
  --assignment <hypoweaver或baselines> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json \
  --report /absolute/path/to/private-preflight-report.json
```

预检不会调用外部模型。它核验：

- Harness commit 和工作树清洁状态；
- protocol、suite、案例路径，以及本人分组的 4 或 20 个单元；
- 上游合同测试；
- API 配置权限、模型和 endpoint，但不输出 Key；
- release lock 和 hash-bound receipt。

只有 `external_execution_ready=true` 才能运行。若数据控制方确认、receipt 或
`execution_enabled=true` 任一缺失，脚本必须拒绝。

## 4. 正式执行

```bash
python3 scripts/case_operator.py run \
  --case <005或007> \
  --assignment <hypoweaver或baselines> \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

操作器会先生成完整 24 单元计划，再只保留 release package 启用的分组。HypoWeaver 同学不会
运行五个基线系统，五基线同学也不会运行 HypoWeaver。

禁止：

- 修改 prompt、预算、系统代码、模型或数据后继续同一批次；
- 删除失败目录再跑；
- 只重跑表现不好的系统；
- 合并 discovery/aligned 或 native/common 分数；
- 把结果提交到 GitHub。

工作流失败可以封存并继续；完整性错误、release 漂移、模型身份漂移、缺失终态产物或未裁决的
基础设施异常必须停止整个批次。

## 5. 合并与验收

两位同学分别封存结果后，由项目负责人统一合并并核对：

- HypoWeaver 包含 4 个唯一 `cell_manifest.json`；
- 五基线包包含 20 个唯一 `cell_manifest.json`；
- 合并后为 24 个唯一单元，12 native + 12 common，六个系统各 4 个；
- 每个单元的 Case、视图、系统、榜单、种子和哈希与冻结计划一致；
- 每个终态都有 `normalized_result.json` 和 `evidence_manifest.json`；
- 失败单元保留原始失败证据，没有删除或选择性重跑；
- common 单元的 result/receipt 成对存在；
- Case 007 的空间权重、映射和补充资产哈希进入 manifest/receipt；
- API Key、隐藏参考和真实数据未进入日志或回传说明。

只有两份包都封存并通过完整性检查后，才能生成对照结果。缺少任一分组时，只能标记为
`incomplete`，不能把已有分组当作完整六系统比较。
