# AI 可读测试包与结构化回传

## 同学收到后

同学只需要解压，并把文件夹交给 AI。AI 必须先读根目录的 `AGENTS.md`、
`START_HERE_FOR_AI.md` 和 `ASSIGNMENT.json`，再运行：

```bash
python3 tools/student_handoff.py explain
```

工具依据脚本自身所在位置解析包根目录，所以压缩包可以放在任意目录，也不需要负责人预先知道
同学机器的用户名。输出会明确给出当前绝对路径、Case、分组、系统、应有单元数、结果目录和回传
目录。

首次使用时运行 `SETUP.command`，或：

```bash
python3 tools/student_handoff.py setup --yes
```

虚拟环境不随 ZIP 搬运。初始化工具会在本机重建冻结运行环境，并根据本机绝对路径生成 release
lock；这是一项自动技术校验，不是对同学重新进行人工审批。当前正式执行环境要求 macOS、
Python 3.11、Node.js 18.18+（五基线分组）和网络连接。

## 固定分工

`ASSIGNMENT.json` 由负责人从冻结 protocol 的真实计划生成，不由学生或 AI 手写。它包含：

- Case、release、分组和系统；
- 本人应执行的 4 或 20 个精确 `cell_id`；
- 每个单元的视图、榜单和种子；
- package 内 operator、release、contracts、runs 和 orchestration 的相对路径。

工具拒绝绝对路径、越出包根目录的路径、重复单元和未分配系统。正式执行仍由
`case_operator.py` 再次检查 release package、分组、release lock 和授权回执。

## 结果如何生成

`report` 不让 AI 自己概括聊天或日志，而是逐一读取：

```text
contracts/<cell_id>/cell_manifest.json
runs/<cell_id>/normalized_result.json
runs/<cell_id>/evidence_manifest.json
```

每个单元分为：

- `sealed`：三类必需产物全部存在；
- `partial`：存在部分产物；
- `missing`：三类产物都不存在。

报告同时核对 Case、系统、视图、榜单、种子和 `cell_id`。出现未分配单元或身份不匹配时，
`collection_status=needs_review`；缺失或部分产物时为 `incomplete`；全部精确收齐时才是
`complete`。

`scientific_outcome` 单独记录 `all_completed`、`all_failed`、`mixed`、
`not_evaluated` 或 `needs_review`。因此“工作流失败但失败证据完整”不会被误报成“没有完成测试”。
逐单元表还会列出 claims、executions、已报告完成检查和 false-evidence flags 的数量；正式评分
需要由负责人使用隐藏参考统一完成，不在学生机器上提前计算。

## AI 最终返回什么

运行：

```bash
python3 tools/student_handoff.py bundle
```

会生成可直接复制的 `RETURN/RETURN_POINTER.json`，其中包含：

- package / release / Case / assignment；
- 系统列表；
- 完整性状态和运行结果概况；
- expected、sealed、successful、failed、partial、missing、unexpected 数量；
- 结果表和回传 ZIP 的绝对路径；
- 回传 ZIP 的 SHA-256。

ZIP 使用文件白名单，只收集结构化摘要、预检报告、进度事件和每个已分配单元的必要证据。API
运行配置、API Key、Case 原始数据、隐藏参考、release receipt 和其他分组结果不会被装入回传包。
