# 负责人四人分发指南

## 1. 新分工

| 人 | 任务 | 负责人最终验收 |
|---|---|---|
| A | Case 005 完整 24 单元 | 无缺失/重复；失败均有终态证据 |
| B | Case 007 完整 24 单元 | 同上；空间资产与 receipt 哈希一致 |
| C | Case 009 在新 release 上完整重跑 24 单元 | 无旧结果继承；修复边界和合同一致 |
| D | HypoWeaver 前端 UI | 独立 PR；测试/构建通过；科学语义未改变 |

每个 Case 只在一位同学的一台机器上完成六系统全矩阵，避免同一 Case 内部出现跨机器环境
差异。

## 2. 现在先发公开任务，不先做四台电脑

先按 `docs/DISTRIBUTION_MESSAGES_ZH.md` 发 GitHub 链接和任务书。三位 Case 同学让自己的
AI 按统一合同配置 Windows/WSL2，并先回传 `environment-report.json`。负责人不需要为
他们逐台安装 Docker、Python 或上游系统。公开工具和源码可以按
`docs/ON_DEMAND_FETCH_ZH.md` 由同学的 AI 获取。

负责人先检查报告：

- 版本和资源够用；
- 工作目录私有且不被同步；
- 冻结区只读；
- worker 没有 Docker socket、API Key、主目录或隐藏参考；
- 生成代码无网络且有限权/限资源；
- blocker 真实列出，没有通过放宽隔离来伪造 `ready=true`。

UI 同学不需要案例包和 API Key。只需要公共代码仓库、明确的干净 base commit 和设计参考。

## 3. 负责人仍然必须统一完成的事情

环境可以下放给同学的 AI，但以下内容不能下放：

1. 使用本轮已经完成并通过回归的 Harness、适配器和 runtime 修复提交；
2. 把相关仓库整理到精确、干净、可公开或可打包的 commit；
3. 生成新的不可变 protocol、suite、release inventory 和冻结内容；
4. 确认 Case 005、007、009 的数据处理和分发范围；
5. 对最终可见内容哈希、供应商、endpoint、模型和 protocol 作一次明确授权；
6. 保管 evaluator-only 隐藏参考和最终评分。

Case 009 必须使用修复后的新 release。因为本轮修复也触及公共合同和适配器，三个 Case 都
应使用同一版不可变 release；旧 RC10 不能继续分发。

## 4. 哪些东西统一，哪些东西每台机器生成

负责人统一提供：

- 同一份正式获取清单、固定相对路径和精确提交；
- 公网无法取得的同一份冻结内容；
- 同一版 `release-package.json` 模板；
- 同一份按内容哈希绑定的 outbound authorization receipt；
- 每个同学只能看到的可见 Case 包；
- 同一份隔离和回传合同。

同学的 AI 在本机生成或验证：

- machine capability reports；
- 包内精确路径核对；
- 包内 release lock；
- machine-local readiness evidence；
- runtime config；
- preflight report；
- 环境与结果结构化摘要。

授权 receipt 不是“批准某个同学操作机器”，而是确认特定可见内容可以发送到特定模型。
它不应因 Windows 用户名不同而重新批准。release lock 由负责人冻结并随正式包发放；每台
机器自己的 AI 负责重新核对其中的仓库、文件、协议、模型和 endpoint，并补充本机路径、
依赖、能力与隔离状态的 readiness evidence。

## 5. 私发材料的最小内容

只在环境报告通过后，通过批准的私有渠道向 Case 同学发送：

- 正式获取清单：每个公开仓库的 HTTPS URL、固定相对路径和 40 位完整 commit；
- 公网无法取得的冻结仓库/运行时及压缩包 SHA256；
- 该同学负责 Case 的两个可见视图；
- 新 protocol、suite、release inventory；
- 正式 release package；
- 内容哈希授权 manifest/receipt。

不要发送：

- 其他 Case 的可见数据；
- evaluator-only 隐藏参考；
- 已有系统输出或旧 partial run；
- API Key；
- 中央评分脚本的私有配置。

同学输入自己的 API Key。若使用项目统一 Key，也只能通过受控密钥渠道注入，不能放进压缩
包。

公开仓库可以不重复放进压缩包，但前提是清单中的指定 commit 确实能由一台全新电脑取得。
只要无法从公网取得，就必须发布该提交或作为私有压缩包补发，不能让同学改用上游最新版。

## 6. 当前源码公开可还原性

截至 2026-07-24，本机核对结果如下：

| 目录 | 正式候选 commit | 当前公网状态 |
|---|---|---|
| Harness | `8879f72e65e18258d2672f266e59a8fc6eab5c56` | 没有配置公网 remote |
| Data-to-Paper | `99ae1bdadbad4a55e92632b6b21ce20793adfdd8` | 比上游 `main` 多 2 个本地提交 |
| DeepScientist | `aaf75f0131f28342378e3a9760ebcb640afbe90b` | 比上游 `main` 多 2 个本地提交 |
| HypoWeaver runtime | `6a6c4b3854d694fe2fa8568b2f68428fd7b69dff` | remote 指向本机目录，且多 1 个本地提交 |

所以现在不能告诉同学“所有源码都从 GitHub 拉最新版”。负责人必须为这四个 commit 分别
选择一种交付方式：

1. 推送到负责人控制、同学可访问的固定仓库，再写入正式获取清单；或
2. 保持源码不公开，以哈希固定的私有压缩包发放。

完成其中一种后，再从一台没有本地缓存的干净环境验证可以精确还原。

## 7. Case 009 解锁条件

Case 009 的代码修复和本地回归已经完成。只有以下剩余事项也全部满足，才能把它从“环境
准备”改成“允许运行”：

- 修复后的四个仓库提交全部进入同一 release inventory；
- 新 protocol 和 suite 已冻结；
- Case 009 旧输出未进入新结果目录；
- 新授权 receipt 包含 `case_009_gfri_air_quality`；
- 操作器在本机拒绝 RC10，并接受正式新 release；
- 负责人明确发出一次“可以运行”的文字通知。

## 8. 结果合并

同学负责生成完整、密封、可诊断的运行包，不负责最终排名。负责人集中完成：

- 基础设施失败裁决；
- benchmark-owned 独立复算；
- V 硬门；
- 身份隔离的 Q 盲评；
- scorecard 完整笛卡尔积检查；
- discovery/aligned 与 native/common 分榜聚合；
- 成本、token、时间和失败率报告。

任何 Case 只有部分系统完成时，不得发布六系统排名。
