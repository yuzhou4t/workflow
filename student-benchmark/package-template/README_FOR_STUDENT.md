# 你只需要做三件事

1. 解压后，把整个文件夹交给你使用的 AI，并让它先读
   `START_HERE_FOR_AI.md`。
2. API Key 只在终端的隐藏输入框中填写，不要发给 AI、同学或项目负责人。
3. 测试结束后，把 `RETURN` 目录中的 `*-return.zip` 和
   `RETURN_POINTER.json` 私下发给项目负责人。

你也可以直接双击启动文件，按菜单操作：

- macOS：`START.command`
- Windows 10/11：先双击 `CHECK_WINDOWS.cmd` 验收 WSL2 + Docker，成功后双击
  `START_WINDOWS.cmd`

首次使用时，菜单会帮助你重建本机运行环境。Windows 版要求 WSL2 和 Docker Desktop，并把
Python 3.11、Node.js 20 和六系统执行放进 Linux 容器，不要求在 Windows 本机单独安装 Python
或 Node。正式工作区会自动复制到 WSL 的 Linux 文件系统，避免 Windows 磁盘权限导致 API 配置
校验失败；最终回传文件仍会同步到你解压目录中的 `RETURN`。首次构建镜像和安装依赖需要联网，
耗时取决于下载速度。开始后不要移动或改名解压目录。

请不要改测试代码、Case 文件、分工文件或冻结配置。运行失败也属于结果，不要删除失败目录后重跑。
