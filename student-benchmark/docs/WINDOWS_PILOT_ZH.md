# Windows 版负责人验收流程

## 当前状态

Windows 版目前是 `owner_validation_only` 试运行版本：可以由项目负责人在自己的 Windows 电脑
验收，但在验收记录回来、运行镜像最终锁定和完整预检通过前，不发给四位执行同学，也不把它写成
“Windows 已正式支持”。

它没有把六个系统改写成 Windows 程序。双击入口负责进入 WSL2；可信控制器和各系统运行在固定的
Linux 镜像中。模型生成代码使用完全断网的容器，data-to-paper 和 DeepScientist 只进入 Docker
内部网络，并且只能通过控制器中的预算账本代理调用指定模型。真实 API Key 不挂载进工作容器。

## 电脑准备

要求：

- Windows 11 x64，或 Docker Desktop 当前支持的 Windows 10 22H2 / Build 19045 x64；
- WSL 2.1.5 或更高版本（建议更新到最新版），建议默认 Ubuntu；
- Docker Desktop，并为当前 Ubuntu 开启 WSL integration；
- 首次构建镜像、复制工作区和安装依赖需要足够磁盘空间及网络。

如果没有 WSL2，以管理员身份打开 PowerShell：

```powershell
wsl --install
```

重启后安装并启动 Docker Desktop，在设置中开启当前 WSL 发行版的 integration。官方参考：

- [Microsoft：安装 WSL](https://learn.microsoft.com/windows/wsl/install)
- [Docker：WSL 2 backend](https://docs.docker.com/desktop/features/wsl/)

## 第一阶段：只验收环境，不碰真实 Case 和 API

1. 把负责人提供的未压缩 Windows 试运行文件夹放到 Windows 本地磁盘。
2. 不移动、不改名、不修改其中的文件。
3. 双击 `CHECK_WINDOWS.cmd`。
4. 第一次会下载基础镜像并构建运行环境。
5. 完成后，把原文件夹中的 `RETURN/WINDOWS_ENV_CHECK.json` 发回负责人。

通过必须同时满足：

- `status = "passed"`；
- `visible_read = true`；
- `forbidden_read_denied = true`；
- `output_write = true`；
- `root_write_denied = true`；
- `external_network_denied = true`；
- `ledger_only_network_reachable = true`；
- `docker_socket_absent = true`；
- `network_declared_internal = true`；
- `worker_exit_zero = true`。

这一步使用程序生成的临时 marker，不读取 Case 005/007，不读取 API Key，也不调用 DashScope。

## 第二阶段：负责人确认后才做

第一阶段通过并由负责人检查诊断 JSON 后：

1. 双击 `SETUP_WINDOWS.cmd`，同意首次依赖下载；
2. 双击 `START_WINDOWS.cmd`；
3. 在菜单中配置 API Key；
4. 运行离线预检；
5. 把 `RETURN/PREFLIGHT.json` 发回负责人；
6. 只有 `preflight_passed=true` 且 `external_execution_ready=true`，才讨论是否进行有成本的完整
   Case 测试。

工作区会自动复制到 WSL 自己的 Linux 文件系统，以保证 `chmod 600`、Python 虚拟环境和 npm
符号链接可靠；复制过程中还会依据冻结 Git 索引恢复 Windows 传输可能丢失的符号链接和执行权限。
最后的结构化结果和回传 ZIP 会同步回 Windows 原解压目录的 `RETURN`。API 配置、release lock、
Case 数据和中间结果不会同步到公开 GitHub。

## 正式分发前仍需完成

- 在真实 Windows 机器记录镜像 ID、Docker/WSL 信息和全部隔离检查；
- 固定并复核最终运行镜像摘要；
- 完成 setup 与 20 单元分组的离线预检；
- 至少完成一个完整 Case 的 4 + 20 单元 Windows 验收，核对结构化回传；
- 再重新生成四个干净工作区并压缩，不直接复制负责人已经跑过的目录。
