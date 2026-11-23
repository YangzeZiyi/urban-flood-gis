@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ============================================================
REM  城市内涝风险可视化平台 · 数据库初始化
REM  作用：建库 -> 启 PostGIS -> 建 5 张表 -> 建视图 -> 验证
REM  可重复执行（表会被重建，数据会清空，导入真实数据后慎用）
REM ============================================================

set "PGBIN=D:\dev\PostgreSQL\18\bin"
set "PGHOST=localhost"
set "PGPORT=5432"
set "PGUSER=postgres"
set "PGCLIENTENCODING=UTF8"
set "PGDATABASE=postgres"
set "DBNAME=floodgis"
set "ROOT=D:\wb\urban-flood-gis"
set "LOGDIR=%ROOT%\logs"
set "LOG=%LOGDIR%\init-db.log"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo ==========================================================
echo   城市内涝平台 · 数据库初始化
echo ==========================================================
echo   日志文件：%LOG%
echo.

if not exist "%PGBIN%\psql.exe" (
    echo [错误] 找不到 psql.exe：%PGBIN%\psql.exe
    echo        请确认 PostgreSQL 18 装在 D:\dev\PostgreSQL\18
    goto :fail
)

set "PGPASSWORD="
set /p "PGPASSWORD=请输入 postgres 用户的密码: "
if "!PGPASSWORD!"=="" (
    echo [错误] 密码不能为空
    goto :fail
)

echo.
echo [1/5] 测试数据库连接...
"%PGBIN%\psql.exe" -d postgres -c "SELECT version();" > "%LOG%" 2>&1
if errorlevel 1 (
    echo       [失败] 连不上数据库，八成是密码不对
    echo.
    type "%LOG%"
    goto :fail
)
echo       连接成功

echo [2/5] 创建数据库 %DBNAME% ...
"%PGBIN%\psql.exe" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='%DBNAME%'" > "%LOGDIR%\_exists.tmp" 2>&1
set "DBEXISTS="
for /f "usebackq delims=" %%a in ("%LOGDIR%\_exists.tmp") do set "DBEXISTS=%%a"
del "%LOGDIR%\_exists.tmp" >nul 2>&1

if "!DBEXISTS!"=="1" (
    echo       已存在，跳过创建
) else (
    "%PGBIN%\psql.exe" -d postgres -c "CREATE DATABASE %DBNAME% ENCODING 'UTF8' TEMPLATE template0;" >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo       [失败] 建库失败
        type "%LOG%"
        goto :fail
    )
    echo       已创建
)

echo [3/5] 启用 PostGIS 扩展...
"%PGBIN%\psql.exe" -d %DBNAME% -c "CREATE EXTENSION IF NOT EXISTS postgis;" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo       [失败] 启用扩展失败
    type "%LOG%"
    goto :fail
)
echo       完成

echo [4/5] 建表并创建视图...
"%PGBIN%\psql.exe" -d %DBNAME% -v ON_ERROR_STOP=1 -f "%ROOT%\sql\01_schema.sql" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo       [失败] 01_schema.sql 执行出错
    type "%LOG%"
    goto :fail
)
"%PGBIN%\psql.exe" -d %DBNAME% -v ON_ERROR_STOP=1 -f "%ROOT%\sql\02_views.sql" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo       [失败] 02_views.sql 执行出错
    type "%LOG%"
    goto :fail
)
echo       完成

echo [5/5] 验证...
echo. >> "%LOG%"
echo ============ 验证结果 ============ >> "%LOG%"
"%PGBIN%\psql.exe" -d %DBNAME% -c "SELECT postgis_version();" >> "%LOG%" 2>&1
"%PGBIN%\psql.exe" -d %DBNAME% -c "SELECT ST_AsText(ST_Point(114.3, 30.5)) AS 空间函数测试;" >> "%LOG%" 2>&1
"%PGBIN%\psql.exe" -d %DBNAME% -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;" >> "%LOG%" 2>&1
"%PGBIN%\psql.exe" -d %DBNAME% -c "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'idx_%' ORDER BY indexname;" >> "%LOG%" 2>&1

echo.
echo ============ 执行日志 ============
type "%LOG%"
echo.
echo ==========================================================
echo   完成。数据库 %DBNAME% 已就绪。
echo   日志已保存到 %LOG%
echo ==========================================================
pause
exit /b 0

:fail
echo.
echo ----------------------------------------------------------
echo   初始化未完成。上面的报错可以整段发给我。
echo ----------------------------------------------------------
pause
exit /b 1
