@echo off
chcp 65001 >nul
title cproxy - Clash Verge (Rev) Windows 一键安装与调度部署

echo ==============================================================
echo   cproxy for Clash Verge (Rev) - Windows 一键部署脚本
echo ==============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\install-verge-task.ps1" %*
set EXIT_CODE=%ERRORLEVEL%

echo.
if "%1"=="" (
    pause
)
exit /b %EXIT_CODE%
