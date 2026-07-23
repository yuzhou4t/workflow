# AI 从这里开始

你正在处理一个已经分配好的 SixBench 单人测试包。不要先猜路径，也不要先修改配置。

先根据电脑系统选择入口：

- macOS：按下面的 `python3` 命令执行，或双击 `START.command`；
- Windows：不要直接用 Windows Python。先双击 `CHECK_WINDOWS.cmd`，验收通过后双击
  `START_WINDOWS.cmd`。后续命令会自动在 WSL2 + Docker 控制环境中执行。

进入对应环境后，按顺序执行：

```bash
python3 tools/student_handoff.py explain
```

先用中文向同学讲清楚：

1. 当前压缩包解压后的绝对路径；
2. 本人负责的 Case 和分组；
3. 需要运行哪些系统、多少个单元；
4. 结果会写到哪里；
5. 预检、正式运行和结果回传分别会做什么。

然后运行离线预检：

```bash
python3 tools/student_handoff.py setup --yes
python3 tools/student_handoff.py preflight
```

`setup` 只在首次解压或本机环境缺失时运行。它会联网下载冻结依赖，并生成与当前绝对路径和 Python
环境绑定的技术性 release lock；这不代表重新进行一次人工授权。开始下载前先告诉同学并取得确认。
HypoWeaver 分包也会安装共同执行板校验所需的 data-to-paper 运行时；这只是 release lock 的
冻结依赖，不会增加该同学需要运行的系统或单元。

Windows 的 `CHECK_WINDOWS.cmd` 不使用 API Key、不读取真实 Case，也不会调用外部模型。它会
验证受限容器只能读取显式挂载的测试文件、只能写结果目录、不能直接访问公网，并且仍能访问
控制器中的本地计费代理。若失败，保留并回传 `RETURN/WINDOWS_ENV_CHECK.json`，不得绕过。

只有预检结果同时满足：

```text
preflight_passed: true
external_execution_ready: true
```

才可以继续。若被阻止，请原样解释 `blockers`，不要绕过 release lock、授权回执或哈希校验。

配置 API Key 时运行：

```bash
python3 tools/student_handoff.py configure-api
```

Key 由同学在终端隐藏输入。AI 不得索要、读取、回显或写入聊天。

正式执行前，再向同学确认一次。得到明确确认后运行：

```bash
python3 tools/student_handoff.py run --yes
```

无论运行成功还是失败，都不要删除产物。最后运行：

```bash
python3 tools/student_handoff.py bundle
```

工具会从真实文件生成：

- `RETURN/RESULT_SUMMARY.md`：人可以直接阅读的逐单元结果表；
- `RETURN/RESULT_SUMMARY.json`：结构化结果；
- `RETURN/RETURN_MANIFEST.json`：回传文件及哈希；
- `RETURN/RETURN_POINTER.json`：可以直接发给负责人的结构化回执；
- `RETURN/*-return.zip`：私下回传的结果包。

最终回复同学时，原样粘贴 `RETURN/RETURN_POINTER.json` 的内容，并指出回传 ZIP 的绝对路径。
不得凭聊天内容手工补齐或美化测试结果。
