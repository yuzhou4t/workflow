# 负责人准备与发放指南

## 1. 先解决数据控制权

Case 005 和 Case 007 当前都要求 `data_controller_attestation`。在收到书面确认前：

- 不向 public GitHub 上传案例数据；
- 不把案例内容发送给 DashScope 或其他模型服务；
- 不生成声称“已授权”的正式 receipt；
- `release-package.json` 必须保持 `execution_enabled=false`。

数据控制方确认至少应写清：Case、数据来源、允许的人员与机器、允许的 provider/endpoint/model、
允许发送的内容范围、研究用途、保留期限、结果回传和禁止公开再分发。公开模板见
[`../templates/data-controller-attestation.example.md`](../templates/data-controller-attestation.example.md)。

## 2. 每个 Case 的双人角色

| Case | 执行负责人 | 审计负责人 |
|---|---|---|
| 005 | 唯一 24 单元运行、日志和结果封存 | 预检与 24 单元完整性审计 |
| 007 | 唯一 24 单元运行、空间资产核验 | 预检、空间哈希和结果完整性审计 |

审计负责人不使用执行负责人的 API Key，也不重新跑同一预注册种子。

## 3. 为执行机器准备冻结工作区

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
检查所有仓库 commit、工作树清洁状态、Python 3.11、依赖和合同测试。

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

## 5. 生成机器专属 release lock

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

Release lock 必须在数据、代码、suite、模型和路径最终确定后生成。不要复制另一台机器的 lock 后
手工改路径。

## 6. 形成哈希绑定 receipt

只有以下两项都具备后才能形成正式 receipt：

1. 数据控制方书面确认覆盖该 Case 和外部处理范围；
2. 项目负责人看到最终 manifest、protocol、Case 哈希、provider、endpoint、model 后明确批准。

Receipt 必须使用 Harness 定义的 `sixbench-outbound-authorization-receipt-v2` Schema，并让
`sixbench.outbound_authorization.validate_outbound_authorization_receipt` 校验通过。普通学生不能
用自己的同意替代数据控制方，也不能复制其他 Case 或旧 release 的 receipt。

如果任何冻结文件发生变化，重新生成 manifest、lock 和 receipt。

## 7. 生成私有 `release-package.json`

复制公开示例到 Git 忽略的位置：

```bash
cp config/release-package.example.json /absolute/private/path/release-package.json
```

核对 Harness 完整 commit、Python、protocol、suite 和 contract 路径。只有全部门禁通过后，才把
该执行机器负责的 Case 设为：

```json
{"execution_enabled": true}
```

另一 Case 保持 `false`。正式文件不要提交到 Git。

## 8. 发放与回收

执行同学收到：

- 冻结工作区访问权；
- 机器专属 `release-package.json`；
- release lock、outbound manifest 和正式 receipt；
- 自己负责 Case 的任务书；
- 私有结果回传路径。

审计同学收到：

- 不含 API Key 的冻结 manifest、preflight 报告和终态结果包；
- 审计任务书；
- 问题登记与裁决入口。

API Key 由执行同学在自己机器上隐藏输入，负责人不收集。结果和 hidden reference 永远不提交到
public GitHub。
