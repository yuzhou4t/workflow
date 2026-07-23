# 负责人准备与发放指南

## 1. 先解决数据控制权

Case 005 和 Case 007 当前都要求 `data_controller_attestation`。在收到书面确认前：

- 不向 public GitHub 上传案例数据；
- 不把案例内容发送给 DashScope 或其他模型服务；
- 不生成声称“已授权”的正式 receipt；
- `release-package.json` 必须保持 `execution_enabled=false`。

数据控制方确认至少应写清：Case、数据来源、执行主体限制（如有）、允许的
provider/endpoint/model、允许发送的内容范围、研究用途、保留期限、结果回传和禁止公开再分发。公开模板见
[`../templates/data-controller-attestation.example.md`](../templates/data-controller-attestation.example.md)。

这是一项针对数据外发范围的一次性项目确认，不是逐个批准学生工作。若 Case 字节、协议、
provider、endpoint 和 model 都未变化，同一份正式授权回执可以覆盖本轮四个执行任务。

## 2. 每个 Case 的双人执行分工

| Case | HypoWeaver 同学 | 五基线同学 |
|---|---|---|
| 005 | HypoWeaver 4 单元 | 其余五系统 20 单元 |
| 007 | HypoWeaver 4 个空间案例单元 | 其余五系统 20 个空间案例单元 |

每位同学使用独立工作区和自己的 API 配置。双方完成前不交换结果；项目负责人最后合并为每个
Case 的 24 个唯一单元。五基线任务明显更重，应预留更多时间和 API 预算。两个执行环境应尽量
使用相同架构和资源规格；环境不同时，不把运行时间差直接作为系统能力结论。

## 3. 为四个执行任务准备冻结工作区

建议目录保持如下结构；具体路径必须以最终机器为准：

```text
frozen-workspace/
├── Agent Laboratory/
├── benchmark-baselines/
│   ├── DeepScientist/
│   ├── data-to-paper/
│   ├── hypoweaver-rc9-runtime/
│   └── six-system-comparison/
├── benchmark-cases-v3-pilot/
├── benchmark-results-v3-pilot/
└── workflow/
    └── student-benchmark/
```

不要把案例数据和 hidden reference 放进公开 Git checkout。通过批准的私有渠道放入固定位置后，
检查所有仓库 commit、工作树清洁状态、Python 3.11、依赖和合同测试。四个工作区使用相同文件
版本；本机路径和 Python 环境只属于自动兼容性检查，不构成新的人工授权。

## 4. 生成 outbound manifest

在 Harness 根目录运行：

```bash
PYTHONPATH=src .venv/bin/python -m sixbench.outbound_authorization \
  --protocol configs/benchmark-v3-pilot-rc9.json \
  --provider-id dashscope \
  --provider-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model qwen3.7-plus \
  --output /absolute/private/path/benchmark-v3-pilot-rc9-outbound-manifest.json
```

Manifest 只是待授权内容清单，默认状态不是授权。先检查它是否只包含需要的 Case 和最终文件哈希。

## 5. 在每个工作区自动生成 release lock

仍在 Harness 根目录运行：

```bash
PYTHONPATH=src .venv/bin/python -m sixbench.release_freeze \
  --release-id benchmark-v3-pilot-rc9 \
  --protocol configs/benchmark-v3-pilot-rc9.json \
  --inventory configs/release-inventory-v9.json \
  --provider-id dashscope \
  --provider-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --model qwen3.7-plus \
  --output /absolute/private/path/benchmark-v3-pilot-rc9-release-lock.json
```

当前 RC9 的 release lock 会记录本机绝对路径、Python 环境和 capability report，因此必须在各自
工作区自动生成，不能复制另一台机器的 lock 后手工改路径。它证明“本机确实在运行冻结版本”，
不是对该同学进行新的人工审批。

## 6. 一次性形成项目级哈希绑定 receipt

只有以下两项都具备后才能形成正式 receipt：

1. 数据控制方书面确认覆盖该 Case 和外部处理范围；
2. 项目负责人看到最终 manifest、protocol、Case 哈希、provider、endpoint、model 后明确批准。

Receipt 应同时授权 Case 005 和 Case 007，并使用 Harness 定义的
`sixbench-outbound-authorization-receipt-v2` Schema，让
`sixbench.outbound_authorization.validate_outbound_authorization_receipt` 校验通过。普通学生不能
用自己的同意替代数据控制方，也不能复制其他 Case 或旧 release 的 receipt。

四个执行任务可以复用同一份项目级 receipt；本机 release lock 仍分别生成。只有冻结 Case、协议
或 provider/endpoint/model 发生变化时，才重新生成 manifest 和 receipt，不需要每次运行重新批准。

## 7. 生成私有 `release-package.json`

复制公开示例到 Git 忽略的位置：

```bash
cp config/release-package.example.json /absolute/private/path/release-package.json
```

核对 Harness 完整 commit、Python、protocol、suite 和 contract 路径。只有全部门禁通过后，才把
该执行任务负责的 Case 设为：

```json
{
  "execution_enabled": true,
  "enabled_assignments": ["hypoweaver"]
}
```

五基线同学使用 `["baselines"]`。另一 Case 保持 `false`，同一正式包不得同时启用两个分组。
正式文件不要提交到 Git。

## 8. 发放与回收

四位执行同学分别收到：

- 包含 AI 入口和本人精确计划的冻结工作区压缩包；
- 只启用本人 Case 和分组的 `release-package.json`；
- 自动生成本机 release lock 的初始化工具、统一 outbound manifest 和项目级正式 receipt；
- 自己负责的 4 单元或 20 单元任务书；
- 私有结果回传路径。

API Key 由各执行同学在自己机器上隐藏输入，负责人不收集。收到两份封存包后，项目负责人按
Case 合并 4 + 20 个单元并进行完整性检查。结果和 hidden reference 永远不提交到 public GitHub。

在每个已经整理好的冻结工作区中安装 AI 入口：

```bash
python3 student-benchmark/scripts/install_ai_handoff.py \
  --case 005 \
  --assignment hypoweaver \
  --workspace /absolute/path/to/curated-frozen-workspace \
  --release /absolute/path/to/release-package.json \
  --formal
```

另外三个包分别替换为 `005/baselines`、`007/hypoweaver` 和 `007/baselines`。工具会从冻结
protocol 生成精确的 4/20 单元列表，并拒绝“formal 包却没有只启用本人分组”的 release。

不要直接压缩当前开发目录：它可能包含旧结果、`.git`、不可移植的 `.venv`、API 配置和其他
Case。应先在单独的 curated workspace 中只放允许发放的冻结材料，安装 AI 入口，运行秘密扫描和
离线检查，再形成私有 ZIP。不要把负责人机器生成的 release lock 放进 ZIP；同学解压后由
`SETUP.command` 在本机自动重建虚拟环境并生成技术性 lock。公开 GitHub 只保存模板和工具，不
保存四个正式 ZIP。

同学回传的 `RETURN_POINTER.json` 是机器可读收件单；先核对 ZIP SHA-256，再依据
`RESULT_SUMMARY.json` 和 `RETURN_MANIFEST.json` 合并。详细格式见
[`AI_PACKAGE_FORMAT_ZH.md`](AI_PACKAGE_FORMAT_ZH.md)。
