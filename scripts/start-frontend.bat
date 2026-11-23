@echo off
chcp 65001 >nul
echo ========================================
echo   城市内涝平台 · 前端启动
echo ========================================
cd /d "%~dp0..\frontend"

if not exist package.json (
    echo [错误] 未找到 package.json，请先执行：
    echo   npm create vue@latest frontend
    pause
    exit /b 1
)

if not exist node_modules (
    echo 首次运行，正在安装依赖...
    call npm install
)

echo 正在启动 Vite（默认端口 5173）...
call npm run dev
pause
