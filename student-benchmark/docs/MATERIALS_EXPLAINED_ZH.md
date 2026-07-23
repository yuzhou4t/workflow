# 四类私有运行材料是什么意思

## 1. 冻结工作区

冻结工作区不是普通代码压缩包，而是一次实验的完整、不可随意修改的本机目录。它至少包含：

- SixBench Harness；
- HypoWeaver、Agent Laboratory、data-to-paper、DeepScientist 等冻结版本；
- Case 005 或 Case 007 的两个可见视图；
- Python 3.11 环境和精确依赖；
- 协议、suite、执行器合同和输出目录。

“冻结”表示仓库提交、案例文件哈希、模型、endpoint、预算和依赖确定后不得继续修改。同学若改动
任一冻结文件，release lock 校验应失败。

公开 GitHub 仓库只能提供代码和说明。案例数据是否允许公开或发送给模型，需要由数据控制方另行
确认；当前不能假定公开仓库具有这项权利。

## 2. `release-package.json`

这是学生操作器的入口索引，告诉脚本：

- 冻结工作区中的 Harness 在哪里；
- 必须使用哪个完整 Git commit；
- 使用哪个 Python；
- 使用哪个 protocol、suite 和 common executor contract；
- 本 Case 是否已经获准执行。

它通常使用相对于冻结工作区的路径，但仍要按每台机器的真实目录和环境核对。公开仓库只保存
`release-package.example.json`，其中 `execution_enabled=false`。正式副本不能提交到 Git。

## 3. Release lock

Release lock 是机器生成的不可变清单，绑定：

- protocol 和 suite 的 SHA256；
- 005/007 两个视图及补充资产的 SHA256；
- Harness 和上游仓库的精确 commit；
- Python、依赖环境、供应商、endpoint 和模型；
- outbound manifest 与 receipt 的预期位置。

它回答的是“这台机器准备运行的，是否正是批准过的那一版”。它不是许可证，也不代表数据已经
获准外发。路径、文件或 commit 漂移后应重新冻结，而不是手改 lock。

## 4. 哈希绑定 authorization receipt

Receipt 是对一次明确授权的机器可校验记录。它必须同时绑定：

- Case ID 及两个视图的最终哈希；
- outbound manifest 的哈希；
- protocol 的 ID 和哈希；
- provider、endpoint、model；
- 允许发送的范围；
- 谁在什么时间依据什么数据控制方确认作出批准。

它回答的是“谁批准把哪一版可见内容发给哪个模型服务”。公开模板、GitHub 仓库所有者身份或学生
自己的同意都不能替代数据控制方授权。修改任何冻结内容后，旧 receipt 自动失效。

## 为什么不能把四项全部公开

| 内容 | 可公开 | 原因 |
|---|---:|---|
| 操作代码、空模板、生成命令 | 是 | 不包含数据和真实授权 |
| 上游开源代码及许可证 | 满足许可证时可以 | 仍须固定版本和保留归属 |
| 005/007 案例数据 | 当前否 | 数据控制方外发/再分发状态尚未确认 |
| API Key、runtime config | 否 | 属于个人凭据 |
| 正式 receipt | 不建议 | 只对特定主体、Case 哈希和范围有效，公开不能授权其他人 |
| 隐藏参考、结果包 | 否 | 会破坏盲测并可能泄露受限内容 |

因此正确流程是“公开代码入口 + 私有机器包 + 本机密钥 + 私有结果回传”，而不是把整个冻结工作区
放进 public GitHub。
