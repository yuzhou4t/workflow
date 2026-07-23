@echo off
setlocal
chcp 65001 >nul
title SixBench Windows Check
set "PACKAGE_DIR=%~dp0."

where wsl.exe >nul 2>nul
if errorlevel 1 (
  echo [SixBench] 未找到 WSL2。
  echo 请先以管理员身份打开 PowerShell，运行：wsl --install
  echo 重启 Windows 后，再双击本文件。
  pause
  exit /b 2
)

echo [SixBench] 正在运行离线 Windows 环境与隔离验收……
wsl.exe --cd "%PACKAGE_DIR%" bash ./tools/windows/run-in-wsl.sh diagnose
set "SIXBENCH_EXIT=%ERRORLEVEL%"
echo.
if "%SIXBENCH_EXIT%"=="0" (
  echo [SixBench] 环境验收通过。
) else (
  echo [SixBench] 环境验收未通过，状态码：%SIXBENCH_EXIT%
)
if exist "%PACKAGE_DIR%\RETURN\WINDOWS_ENV_CHECK.json" (
  echo 请把 RETURN\WINDOWS_ENV_CHECK.json 发给负责人。
) else (
  echo 没有生成诊断 JSON；请把本窗口完整截图发给负责人。
)
pause
exit /b %SIXBENCH_EXIT%
