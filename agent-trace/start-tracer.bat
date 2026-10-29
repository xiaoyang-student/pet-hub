@echo off
rem PetHub AI Step Tracer (8 AI office apps, step by step)
rem Uses the Python runtime bundled with PetHub - no install needed.
chcp 65001 >nul
cd /d "%~dp0"
title PetHub AI Tracer
echo ============================================================
echo   PetHub AI Tracer
echo   8 AI apps tracked step-by-step (reads their own records)
echo   Press Ctrl+C to stop. Keep this window open.
echo ============================================================
"..\runtime\python\python.exe" trace.py watch --pethub
echo.
echo Tracer stopped.
pause
