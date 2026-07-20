# Task3 T5 冻结评测协议

本协议适用于 App A `1.4.0`（`DEFINITION_VERSION=1.4.0`）的企业面板一次性正式评测。正式入口是代码所有的 `hypoweaver.official_benchmark_runner`：它负责冻结协议、预占唯一尝试、运行三个系统和五次盲评，并编译最终交付物。

## 正式执行

1. 准备一份 `OfficialBenchmarkConfiguration` JSON。其中的 `protocol_path`、可见输入、隐藏参考、源码清单和配置清单都必须是相对 `artifact_root` 的归一化路径。运行目录必须为空，数据集必须已登记且 SHA256 与可见输入一致。
2. 在任何真实模型调用前生成冻结协议：

   ```bash
   PYTHONPATH=backend/src python3.11 -m hypoweaver.official_benchmark_runner prepare \
     --config official-benchmark-config.json
   ```

   `prepare` 会校验案例身份，并计算 HypoWeaver、Agent Laboratory、benchmark harness、评测配置、可见输入、数据和隐藏参考的 SHA256。
3. 在代码、配置、输入和协议全部冻结后，运行唯一一次正式尝试：

   ```bash
   PYTHONPATH=backend/src python3.11 -m hypoweaver.official_benchmark_runner run \
     --config official-benchmark-config.json
   ```

   `run` 会在首次模型调用前内部执行 `begin_official_attempt`，使用 `artifact_root` 重新校验已冻结的源码和配置，然后生成只读的 run manifest 与 `attempt_id`。不应在调用此命令前手动 `begin`，否则会触发重复尝试拒绝。

## 固定顺序与调用预算

`run` 按以下顺序执行：

1. Qwen 单次端到端基线：由 `QwenSinglePassRunner` 使用代码固定的 prompt 调用一次，不接受手工补写的结果。
2. 冻结的原始 Agent Laboratory：逐 SHA256 校验上游提交 `d9017d9` 的 `ai_lab_repo.py`、`agents.py`、`mlesolver.py`、`papersolver.py`，再由薄适配器调用原始 `LaboratoryWorkflow.perform_research`。它在隔离工作目录中运行，无法读取隐藏参考路径。
3. 完整 HypoWeaver：使用冻结可见输入和外部 Research Engine 走完 H1–H4。
4. A/B 随机匿名的五次 Qwen 模型盲评。每次分别封存“系统到 A/B 的映射”和“A/B 的展示先后”，两种系统映射都必须出现；汇总前由代码解盲到 packet 身份。
5. 代码评估硬指标、九类故障与六项消融，编译交付目录。消融仅重放冻结夹具，不再消耗真实模型预算。

调用上限固定为 `46`：Qwen 单次基线 `1` 次、HypoWeaver 最多 `20` 次、Agent Laboratory 最多 `20` 次、盲评 `5` 次。调用数、输入/输出 token、耗时和技术失败单独记录，不混入科学可靠性指标。

本轮明确禁止新增文献或数据采集，而原始 Agent Laboratory 的 literature review 会请求外部检索。因此该系统可以在首阶段诚实产生 `prohibited_external_data_collection` 中立失败 packet；该失败分支会进入硬指标和盲评，不会被适配器补成研究计划、统计结果或论文。只有来源、隔离、模型连接或 receipt 等技术完整性失败才会终止整个正式尝试。

## Official attempt 凭据

每次实际模型调用尝试都必须产生 `OfficialCallReceipt`。每条凭据绑定本次 `attempt_id` 和 run manifest SHA256，并记录唯一 `call_id`、Qwen provider/模型、调用起止时间与响应或脱敏失败信封的 SHA256。代码在交付时强制校验：

- 三个 packet 的 receipt 数量必须等于各自声明的真实模型调用数；
- Qwen 单次基线必须恰好一次调用；
- 五份盲评必须各有一条独立 receipt；
- 所有调用必须发生在 `begin` 之后，receipt 不得跨尝试或重复使用；fixture、手工补写或无凭据结果不能进入正式交付。

## 隔离与一次性规则

App A、Qwen 单次基线和 Agent Laboratory 只能读取冻结可见输入与数据，不得读取隐藏 reference 或 `reference_summary`。匿名的 paired blind 阶段才会获得冻结 `reference_summary`；结构化 hidden reference 只由确定性代码评测器校验硬指标，不进入三个被评系统的 packet、prompt 或工作目录。

正式 preflight 会把模型、API 基址、评审模型、Python 依赖环境、Agent Laboratory 上游提交、20-call 上限、禁采集策略和 macOS 隔离策略冻结为内存快照。Research Engine 的 health identity 同时绑定完整 `hypoweaver` Python 源码闭包、依赖环境、主副实现 ID 和方法能力；仅返回 HTTP 2xx 不足以通过。

`completed` 和 `failed` 都是不可逆终态。任一正式系统调用、隔离执行、receipt 或交付校验失败，都会将该尝试写为 `failed`；一次性锁使用 `case_id + visible_input_sha256 + data_sha256` 的稳定留出案例身份，不能通过修改源码、配置、reference 或 protocol SHA 后换目录重跑。正式结果未达标时必须如实报告，不得把结果反馈给当前案例后重跑；下一次无偏评测需使用新的留出案例。

## 底层调试入口

`hypoweaver.benchmark_protocol` 保留低层 `freeze` / `begin` / `deliver` 命令用于协议测试和已封存 packet 的离线编译，但不代替上述代码所有的正式 runner。单独调用 `begin` 时必须明确传入冻结产物的根目录：

```bash
PYTHONPATH=backend/src python3.11 -m hypoweaver.benchmark_protocol begin \
  --protocol frozen-protocol.json \
  --output-dir benchmark-results/task3-enterprise-panel-v1 \
  --artifact-root /absolute/path/to/artifact-root
```

该底层命令会对 `artifact_root` 下冻结的显式源码/配置清单再做 SHA256 校验；任何漂移、符号链接、路径重叠或越界都会在第一次模型调用前被拒绝。
