# Case 005/007 四人测试交接

本目录是公开的操作入口，目标是让四位同学从
`https://github.com/yuzhou4t/workflow` 拉取统一说明和工具，然后在获准的本机冻结工作区中
完成 Case 005 或 Case 007 的六系统测试。

公开仓库不包含案例数据、API Key、隐藏参考、机器专属 release lock、正式授权回执或结果。
这些内容不能通过把仓库设为 public 自动获得授权。

## 四人分工

| 小组 | 角色 | 工作 |
|---|---|---|
| Case 005 | [执行负责人](assignments/CASE_005_RUNNER.md) | 在唯一获准机器上运行完整 24 单元并封存产物 |
| Case 005 | [独立审计负责人](assignments/CASE_005_AUDITOR.md) | 复核预检、24 个 manifest、失败证据和结果包完整性，不重复调用模型 |
| Case 007 | [执行负责人](assignments/CASE_007_RUNNER.md) | 在唯一获准机器上运行完整 24 单元并封存产物 |
| Case 007 | [独立审计负责人](assignments/CASE_007_AUDITOR.md) | 额外复核空间权重、映射和 execution receipt 哈希，不重复调用模型 |

每个案例的工作量为：

```text
6 个系统 × 2 个输入视图 × 2 个榜单 × 1 个种子 = 24 个单元
```

两位同学合作研究同一个案例，不等于把同一批次跑两遍。执行负责人负责唯一的付费运行；
审计负责人负责独立检查和问题登记。这样既保留双人复核，也不改变预注册种子或重复消耗 API。

## 从公开仓库开始

```bash
git clone https://github.com/yuzhou4t/workflow.git
cd workflow/student-benchmark
python3 -m unittest discover -s tests -v
python3 scripts/case_operator.py status --case 005
```

到这里都不需要 API Key 或案例数据。

随后，负责人通过私有渠道向执行同学提供：

1. 获准的冻结工作区；
2. 该机器对应的 `release-package.json`；
3. 已在该机器生成并校验的 release lock；
4. 覆盖指定 Case 的哈希绑定 authorization receipt。

四项材料的含义和生成顺序见
[`docs/MATERIALS_EXPLAINED_ZH.md`](docs/MATERIALS_EXPLAINED_ZH.md)，负责人操作见
[`docs/COORDINATOR_GUIDE_ZH.md`](docs/COORDINATOR_GUIDE_ZH.md)。

## 执行负责人命令

以下以 Case 005 为例；Case 007 把编号替换为 `007`。

安全写入 API 配置，Key 通过隐藏输入读取：

```bash
python3 scripts/case_operator.py configure-api \
  --case 005 \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

先运行不会调用外部模型的预检：

```bash
python3 scripts/case_operator.py preflight \
  --case 005 \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json \
  --report /absolute/path/to/private-preflight-report.json
```

只有报告同时出现以下状态才允许继续：

```text
preflight_passed: true
external_execution_ready: true
```

正式运行：

```bash
python3 scripts/case_operator.py run \
  --case 005 \
  --workspace /absolute/path/to/frozen-workspace \
  --release /absolute/path/to/release-package.json
```

详细停止条件和回传内容见
[`docs/STUDENT_RUNBOOK_ZH.md`](docs/STUDENT_RUNBOOK_ZH.md)。

## 当前公开状态

仓库中的 [`config/release-package.example.json`](config/release-package.example.json)
故意将 005/007 的 `execution_enabled` 都设为 `false`。这是安全模板，不是正式运行包。

只有数据控制方确认、机器冻结、哈希绑定授权和本地预检全部通过后，负责人才能在私有正式副本中
把对应 Case 改为 `true`。不得在 GitHub 提交这个正式副本。
