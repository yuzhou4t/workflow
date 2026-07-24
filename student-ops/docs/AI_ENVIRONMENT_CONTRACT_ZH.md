# AI 环境与隔离合同（Windows / WSL2）

## 核心原则

隔离环境可以由每位同学自己的 AI 搭建，不需要负责人逐台电脑代劳。但四个 AI 必须执行
同一份合同并交回证据；不能各自发明一套安全标准。

AI 可以安装、配置、检查和生成报告。涉及管理员权限、启用系统组件、重启、输入 API Key、
开始付费模型调用和上传结果时，必须由电脑主人确认。

## Case 同学的标准环境

推荐使用 Windows 11 + WSL2 Ubuntu。Docker Desktop 可作为容器运行时，但必须开启 WSL2
后端和对应 Ubuntu 集成。所有 Git、Python、测试和 benchmark 命令都在 WSL2 内执行，
不要混用 Windows Python 与 WSL Python。

AI 应按以下顺序工作：

1. 记录 Windows、WSL、Ubuntu、Docker、Git、Python 和可用磁盘/内存版本；
2. 克隆公开的 `sixbench-student-ops`；
3. 通过官方来源按需安装公开工具和依赖；
4. 在私有工作目录中准备运行环境，确保该目录不会被云盘同步或 Git 提交；
5. 在不读取案例数据和密钥的情况下完成容器与本地依赖检查；
6. 生成 `environment-report.json` 并停止，等待正式获取清单和私有材料。

正式源码组装只能在可信控制层联网进行。AI 只能获取负责人清单中列出的公开 URL 和完整
commit；取不到就报告 blocker，不能自行换成最新分支或其他 fork。完整规则见
`docs/ON_DEMAND_FETCH_ZH.md`。

## 必须满足的隔离边界

- 冻结工作区只读；AI 的 Dockerfile、Compose、日志和临时文件放在工作区之外。
- API Key 只能存在于可信控制层的私有配置中，不能进入命令行、Git、日志、截图或普通工作
  容器。
- 系统工作容器不能挂载 Docker socket、SSH 密钥、浏览器配置、用户主目录或隐藏参考。
- 案例可见输入以只读方式挂载；每个 cell 只有自己的可写输出目录。
- 执行模型生成代码的容器必须禁用网络，并使用只读根文件系统、非 root 用户、
  `cap_drop=ALL`、`no-new-privileges` 及明确的 CPU、内存和进程数限制。
- 只有冻结的供应商代理可以访问固定 endpoint；不能允许系统任意访问互联网。
- 按需获取只发生在冻结前的可信控制层；工作区冻结后不能再拉取代码或更新依赖。
- evaluator-only 隐藏参考不能出现在同学机器的工作容器或提示上下文中。
- 任何隔离检查失败都必须 fail-closed，不能通过删除检查或手改 `execution_enabled` 继续。

具体容器参数可以由 AI 结合机器实现，但上述边界不能降低。若某个上游系统暂时无法满足，
AI 应报告 blocker 和最小修复建议，而不是偷偷放宽权限。

## 正式 release 材料到达后

AI 负责：

1. 按正式获取清单克隆公开仓库，并在私有目录解压负责人补发的目录；
2. 保持固定相对路径，校验 origin、完整 commit、压缩包 SHA256 和文件哈希；
3. 运行离线 capability checks；
4. 验证包内 release lock，并为本机生成 machine readiness evidence；
5. 使用负责人提供的内容哈希授权回执；
6. 请电脑主人通过隐藏输入填写 API Key；
7. 运行 preflight；只有双 `true` 才向主人请求付费运行确认。

授权回执绑定的是可见案例内容、协议、供应商、endpoint 和模型哈希，不是给某个人“操作
电脑的权限”。release lock 由负责人统一冻结并随正式包发放；同学的 AI 不能重写它，只能
在本机重新核验，并另行记录本机路径、依赖、能力和隔离状态。因此：正式内容、总封条与
授权由负责人统一提供，本机 readiness 和隔离检查由同学的 AI 完成。

## `environment-report.json`

报告不能包含用户名、真实绝对路径、API Key 或案例内容。至少使用以下结构：

```json
{
  "schema_version": "sixbench-environment-report-v1",
  "assignment": "case_005",
  "os": {
    "windows": "<version>",
    "wsl": "<version>",
    "linux": "<distribution and version>"
  },
  "runtime": {
    "docker": "<version>",
    "python": "<version>",
    "git": "<version>"
  },
  "resources": {
    "memory_gb": 0,
    "free_disk_gb": 0
  },
  "isolation_checks": {
    "frozen_workspace_read_only": "passed",
    "worker_has_no_docker_socket": "passed",
    "worker_has_no_api_key": "passed",
    "generated_code_network_none": "passed",
    "hidden_reference_absent": "passed",
    "resource_limits_present": "passed"
  },
  "offline_tests": [],
  "blockers": [],
  "ready_for_private_package": true
}
```

报告写 `passed` 必须有实际命令或日志依据。证据日志私下回传，不提交 GitHub。
