@echo off
setlocal
chcp 65001 >nul
title SixBench Windows Setup
set "PACKAGE_DIR=%~dp0."

where wsl.exe >nul 2>nul
if errorlevel 1 (
  echo [SixBench] 未找到 WSL2。请先运行：wsl --install
  pause
  exit /b 2
)

wsl.exe --cd "%PACKAGE_DIR%" bash ./tools/windows/run-in-wsl.sh setup
set "SIXBENCH_EXIT=%ERRORLEVEL%"
if not "%SIXBENCH_EXIT%"=="0" pause
exit /b %SIXBENCH_EXIT%
