@echo off
setlocal EnableDelayedExpansion

REM Launches worker_setup.ps1 with relaxed execution policy.
set "SCRIPT_DIR=%~dp0"
set "PS1_FILE=%SCRIPT_DIR%worker_setup.ps1"

if not exist "%PS1_FILE%" (
    echo worker_setup.ps1 not found alongside this launcher.
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1_FILE%" %*
