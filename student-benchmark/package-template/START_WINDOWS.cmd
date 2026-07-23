@echo off
setlocal
chcp 65001 >nul
title SixBench Windows
set "PACKAGE_DIR=%~dp0."

where wsl.exe >nul 2>nul
if errorlevel 1 (
  echo [SixBench] 未找到 WSL2。
  echo 请先以管理员身份打开 PowerShell，运行：wsl --install
  echo 重启 Windows 后，再双击本文件。
  pause
  exit /b 2
)

echo [SixBench] 正在进入 WSL2 + Docker 隔离环境……
wsl.exe --cd "%PACKAGE_DIR%" bash ./tools/windows/run-in-wsl.sh menu
set "SIXBENCH_EXIT=%ERRORLEVEL%"
if not "%SIXBENCH_EXIT%"=="0" (
  echo.
  echo [SixBench] Windows 启动失败，状态码：%SIXBENCH_EXIT%
  echo 请先双击 CHECK_WINDOWS.cmd，并把生成的诊断 JSON 发给负责人。
  pause
)
exit /b %SIXBENCH_EXIT%
