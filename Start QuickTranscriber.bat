@echo off
rem QuickTranscriber for Windows - double-click to start.
rem The first start sets everything up inside this folder (needs internet once).
rem Prefer QuickTranscriber.exe, which does the same without a console window.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\setup.ps1" %*
if errorlevel 1 pause
endlocal
