# 按需获取公开代码与依赖

## 一句话规则

同学的 AI 可以联网安装公开工具、克隆公开仓库，但正式测试只能使用负责人清单中写明的
仓库 URL、相对路径和完整 commit。案例数据、隐藏参考、论文、作者代码和已有结果不能自行
搜索或抓取。

这意味着负责人不必把所有公开代码都重复塞进压缩包；但“按需获取”也不等于使用当时最新
版本，更不等于让 AI 在网上寻找替代数据。

## 三个阶段

### 1. 环境准备

AI 可以通过官方来源按需获取：

- WSL2、Ubuntu、Docker Desktop、Git、Python 和系统包；
- 本仓库以及任务书明确写出的公开仓库；
- 冻结仓库声明的 Python、Node.js 和容器依赖。

这一阶段不接收案例、不配置 API Key、不调用外部模型。

### 2. 组装正式工作区

负责人会给出一份正式获取清单。每个源码目录至少写明：

| 字段 | 含义 |
|---|---|
| `relative_path` | 在 `frozen-workspace/` 中的固定位置 |
| `delivery` | `public_git` 或 `private_archive` |
| `repository_url` | `public_git` 时唯一允许使用的 HTTPS 地址 |
| `commit` | 必须是 40 位完整 Git commit |
| `archive_sha256` | `private_archive` 时压缩包的 SHA256 |

AI 只能按清单工作：

- `public_git`：从指定 URL 克隆，切到指定 commit 的 detached HEAD；
- `private_archive`：使用负责人私发的压缩包并核对 SHA256；
- 某个公开 URL 无法取得指定 commit：报告 blocker，不能改用 `main`、tag、fork 或
  “最接近”的版本；
- 清单未列出的仓库、数据或模型资源：不获取。

对每个 Git 仓库至少保存以下核对结果：

```bash
git remote get-url origin
git rev-parse HEAD
git status --porcelain
```

结果必须分别等于清单 URL、完整 commit 和空输出。不要在聊天或公开 Issue 中粘贴私有
路径、案例名称以外的数据内容或授权回执。

### 3. 冻结并运行

工作区组装完成后，AI 将源码区改为只读并停止更新。此后禁止：

- `git pull`、切换分支、更新依赖或重新解析 lockfile；
- 让系统工作容器任意访问互联网；
- 在看过中间结果后替换仓库或案例文件。

可信控制层只保留访问冻结模型 endpoint 的最小网络能力；执行模型生成代码的 worker
仍然必须完全断网。随后按运行手册执行 `configure-api → preflight → run`。

## 哪些内容仍由负责人私发

无论公开代码采用何种获取方式，以下内容都不能放到 GitHub，也不能由同学自行抓取：

- 本人负责 Case 的两个可见输入视图和补充资产；
- 公网无法取得的修复仓库或运行时；
- 正式 `release-package.json`、release lock 和精确获取清单；
- 内容哈希绑定的 authorization manifest/receipt。

evaluator-only 隐藏参考永远不发给同学。API Key 由电脑主人在本机隐藏输入。

## 当前限制

截至 2026-07-24，本轮修复后的四个核心仓库还没有全部成为“给 URL 和 commit 就能从公网
还原”的状态。因此现在可以按需准备公开环境，但不能自行用上游最新版拼出正式 release。
负责人必须先把修复提交发布到可访问的固定仓库，或把对应目录作为
`private_archive` 私发，才能解锁正式运行。
