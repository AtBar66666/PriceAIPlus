@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 比牌 启动器

echo ============================================
echo   比牌  全网货源比价  一键启动
echo ============================================
echo.

REM 1) 启动后端（FastAPI，端口 8756）
echo [1/2] 启动后端 ...
start "PriceAI-后端" cmd /k "cd /d "%~dp0crawler" && .venv\Scripts\python.exe -m app.main"

REM 等后端起来
timeout /t 4 /nobreak >nul

REM 2) 启动前端（Vite，端口 5173）
echo [2/2] 启动前端 ...
start "PriceAI-前端" cmd /k "cd /d "%~dp0frontend" && npm run dev"

REM 等前端编译
timeout /t 7 /nobreak >nul

REM 用 127.0.0.1 打开，绕过部分代理对 localhost 的拦截
echo 打开浏览器 http://127.0.0.1:5173/
start "" "http://127.0.0.1:5173/"

echo.
echo 已启动。两个黑色命令行窗口请保持打开（关掉=停止服务）。
echo 打不开就手动在浏览器输入 http://127.0.0.1:5173/
echo.
pause
