@echo off
chcp 65001 >nul
echo ========================================
echo   城市内涝平台 · 后端启动
echo ========================================
cd /d "%~dp0..\backend"

if not exist mvnw.cmd (
    echo [错误] 未找到 mvnw.cmd，请先用 IDEA 打开 backend 目录生成项目
    pause
    exit /b 1
)

echo 正在启动 Spring Boot（端口 8080）...
call mvnw.cmd spring-boot:run
pause
