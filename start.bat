@echo off
rem PetHub one-click launcher (browser mode)
rem For the real floating transparent pet, use: electron\npm start
cd /d %~dp0

rem bridge: pythonw = no console window at all
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw bridge\server.py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    start "PetHub Bridge" /min python bridge\server.py
  ) else (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
  )
)

timeout /t 1 /nobreak >nul

rem Chrome 装在哪儿不确定（可能不在 C 盘、可能是用户级安装），逐个候选找，
rem 找不到就退回系统默认浏览器。
set "CHROME="
for %%P in (
  "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
  "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
  "%LocalAppData%\Google\Chrome\Application\chrome.exe"
  "%ProgramW6432%\Google\Chrome\Application\chrome.exe"
) do (
  if not defined CHROME if exist %%~P set "CHROME=%%~P"
)
if not defined CHROME (
  for /f "skip=2 tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul') do (
    if not defined CHROME if exist "%%~B" set "CHROME=%%~B"
  )
)

if defined CHROME (
  start "" "%CHROME%" --app=http://127.0.0.1:8765/
) else (
  start "" http://127.0.0.1:8765/
)
echo PetHub started (bridge runs hidden). Floating version: electron\npm start
