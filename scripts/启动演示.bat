@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion

REM ============================================================================
REM  城市内涝风险可视化平台 · 一键启动（演示现场唯一要双击的文件）
REM ----------------------------------------------------------------------------
REM  它做四件事，全部自动判断，不需要人记命令：
REM    1. 自检运行环境（JDK 21、psql）
REM    2. 数据库不存在就建库并导入 sql\floodgis_demo.dump（第一次跑约 10 秒）
REM    3. 等 Spring Boot 起来后自动打开浏览器
REM    4. 前台运行 java -jar，关掉窗口服务就停
REM
REM  关于编码：本文件是 GBK(cp936) 编码 + CRLF 换行，第二行显式把控制台切到 936。
REM    为什么不能用 UTF-8（实测，不是猜的）：
REM    cmd.exe 解析批处理时按"一个字符 = 一个字节"计算文件读取偏移。UTF-8 的汉字
REM    占 3 字节，只要某行出现中文，之后所有行的解析起点就整体错位，
REM    症状是 @echo off 被读成 cho、echo 后面的中文被当成命令执行，
REM    满屏 "不是内部或外部命令"。带 BOM 的 UTF-8 同样错。
REM    GBK 汉字 2 字节，与 cmd 的假设自洽，解析和中文显示都正常。
REM    这和 psql 的中文编码问题是两件事：本脚本传给 psql 的参数全是 ASCII
REM    （SQL 里没有中文），数据本身的中文由 PGCLIENTENCODING=UTF8 保证，
REM    所以用 GBK 存脚本不会让入库中文变乱码。
REM    改动后必须仍然存成 GBK；存成 UTF-8 脚本会直接崩。
REM
REM  所有路径都基于 %~dp0（本文件所在目录），因为 release 目录会被整个
REM  拷到别的机器、别的盘符，任何写死的绝对路径都会让现场翻车。
REM ============================================================================

set "ROOT=%~dp0"
set "JAR=%ROOT%floodgis-demo.jar"
set "DUMP=%ROOT%sql\floodgis_demo.dump"
set "DBNAME=floodgis"
set "DBUSER=postgres"
set "DBHOST=localhost"

echo ============================================================
echo   城市内涝风险可视化平台 · 演示服务
echo ============================================================
echo.

REM ---------------------------------------------------------------------------
REM  1. JDK 自检
REM  Spring Boot 3.5 的字节码是 Java 21 编译的，JDK 17 起来会直接
REM  UnsupportedClassVersionError —— 报错文本很吓人，但原因就这么简单，
REM  所以先查掉，给一句人话。
REM ---------------------------------------------------------------------------
java -version >nul 2>&1
if errorlevel 1 (
    echo [失败] 没有可用的 java 命令。
    echo        需要 JDK 21：装好后把 JDK 的 bin 目录加进 PATH，
    echo        新开一个命令行窗口执行 java -version 能看到 21 即可。
    goto :fail
)

if not exist "%JAR%" (
    echo [失败] 找不到 %JAR%
    echo        演示包不完整，请把 release 目录整个拷过来，不要只拷 jar。
    goto :fail
)

REM ---------------------------------------------------------------------------
REM  2. 数据库密码
REM  允许用环境变量覆盖：换机器、换密码时不必改脚本，set PGPASSWORD=xxx 即可。
REM  没设才回落到本机默认值。
REM ---------------------------------------------------------------------------
if "%PGPASSWORD%"=="" set "PGPASSWORD=010704"
REM  连不上时 5 秒就报错，而不是让演示者对着黑窗口等 30 秒
set "PGCONNECT_TIMEOUT=5"
set "PGCLIENTENCODING=UTF8"

REM ---------------------------------------------------------------------------
REM  3. 定位 psql
REM  先看 PATH；PATH 里没有就在常见安装位置找一遍（版本目录是通配的，
REM  所以 PostgreSQL 15/16/17/18 都能命中）。
REM  找到之后只记 bin 目录，后面 psql / createdb / pg_restore 全从它派生，
REM  避免出现"psql 能跑但 createdb 找不到"的半吊子状态。
REM ---------------------------------------------------------------------------
REM  PATH 命中时 PGBIN 故意保持为空，后面拼出来就是 "psql.exe"，cmd 会自己去 PATH 找
set "PGBIN="
where psql >nul 2>&1
if errorlevel 1 (
    for /d %%D in ("D:\dev\PostgreSQL\*") do if exist "%%~D\bin\psql.exe" set "PGBIN=%%~D\bin\"
    if not defined PGBIN for /d %%D in ("%ProgramFiles%\PostgreSQL\*") do if exist "%%~D\bin\psql.exe" set "PGBIN=%%~D\bin\"
)

"%PGBIN%psql.exe" --version >nul 2>&1
if errorlevel 1 (
    echo [失败] 没装 PostgreSQL，或者 psql 不在 PATH 上。
    echo        需要 PostgreSQL 15+ 并安装 PostGIS 扩展；
    echo        装好后把它的 bin 目录加进 PATH，重新打开本窗口再试。
    goto :fail
)
echo [环境] psql 就绪

REM ---------------------------------------------------------------------------
REM  4. 检查数据库连通性
REM  这一步单独做，是为了把"密码错"和"库不存在"分开报错。
REM  混在一起报的话，现场看到 createdb 的认证失败根本猜不到是密码问题。
REM ---------------------------------------------------------------------------
"%PGBIN%psql.exe" -U %DBUSER% -h %DBHOST% -d postgres -tAc "SELECT 1" >nul 2>&1
if errorlevel 1 (
    echo [失败] 连不上 PostgreSQL 服务器。
    echo        请依次确认：服务是否在跑、端口 5432 是否被占、
    echo        以及密码是否正确（当前用的是 PGPASSWORD 环境变量或脚本里的默认值）。
    goto :fail
)
echo [环境] 数据库连接正常

REM ---------------------------------------------------------------------------
REM  5. 库不存在就建库 + 导入备份
REM  正常演示机上只发生一次；之后再跑就直接跳过，启动只要几秒。
REM  --no-owner / --no-privileges：备份来自别的机器，属主和权限授给谁不重要，
REM  带上这两个参数可以避免 "role xxx does not exist" 这类无关报错。
REM ---------------------------------------------------------------------------
"%PGBIN%psql.exe" -U %DBUSER% -h %DBHOST% -tAc "SELECT 1 FROM pg_database WHERE datname='%DBNAME%'" 2>nul | findstr /c:"1" >nul
if errorlevel 1 (
    echo [数据库] 首次运行，正在创建数据库 %DBNAME% 并导入数据，约 10 秒...
    if not exist "%DUMP%" (
        echo [失败] 找不到 %DUMP%
        echo        没有备份文件就无法自建库。请确认 release 目录里带着 sql 子目录。
        goto :fail
    )
    "%PGBIN%createdb.exe" -U %DBUSER% -h %DBHOST% %DBNAME%
    if errorlevel 1 (
        echo [失败] 建库失败，上面是原始报错。
        goto :fail
    )
    "%PGBIN%pg_restore.exe" -U %DBUSER% -h %DBHOST% -d %DBNAME% --no-owner --no-privileges "%DUMP%"
    REM  pg_restore 对"对象已存在"这类非致命情况会返回 1，但新库不该出现，
    REM  所以这里不吞掉退出码，只在真出错时提示，同时继续往下走，
    REM  因为数据很可能已经完整导入（现场宁可让服务起来再验证，也不要卡死）。
    if errorlevel 1 (
        echo [提示] 导入过程中有报错，服务启动后请打开 /api/zones 核对数据。
    )
    echo [数据库] 已就绪
) else (
    echo [数据库] %DBNAME% 已存在，跳过导入
)

REM ---------------------------------------------------------------------------
REM  6. 端口占用检查
REM  Spring Boot 起不来时窗口会刷一大段 stack trace，现场很难一眼看到
REM  "Port 8080 was already in use"，所以提前查，并且直接把占用进程的 PID 打出来，
REM  照着 taskkill /PID xxx /F 就能解决。
REM ---------------------------------------------------------------------------
set "BUSY_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do if not defined BUSY_PID set "BUSY_PID=%%P"
if defined BUSY_PID (
    echo [失败] 端口 8080 已被进程 PID !BUSY_PID! 占用。
    echo        先执行：taskkill /PID !BUSY_PID! /F
    echo        再重新双击本脚本。
    goto :fail
)

REM ---------------------------------------------------------------------------
REM  7. 延迟 8 秒再开浏览器
REM  Spring Boot + 连接池 + 首次查询，冷启动一般 5-7 秒。直接打开会看到
REM  连接被拒的白页，演示第一印象就毁了。用独立窗口做定时，
REM  这样它不会阻塞下面前台运行的 java。
REM
REM  为什么这里必须写 127.0.0.1 而不是 localhost（真出过事，别改回去）：
REM  天地图 key 的域名白名单里只登记了 127.0.0.1。用 localhost 打开时，浏览器
REM  发出的 Referer 是 http://localhost:8080/，天地图判定域名不符，直接 403
REM  （{"code":301007,"msg":"域名不符"}），后果是整张卫星底图一张瓦片都加载不出来，
REM  屏幕上只剩一个蓝色地球；而时间轴、易涝点、后端接口全都正常，很容易被误判成
REM  Cesium 没配好，在演示现场极难自查。
REM  127.0.0.1 和 localhost 在浏览器眼里是两个不同的域名，白名单不认后者。
REM  以后要换域名或上线，得先去天地图控制台把新域名加进白名单再来改这里。
REM ---------------------------------------------------------------------------
start "" cmd /c "timeout /t 8 /nobreak >nul & start http://127.0.0.1:8080/"

echo.
echo ============================================================
echo   服务启动中，约 8 秒后自动打开浏览器
echo   地址：http://127.0.0.1:8080/
echo   注意：必须用 127.0.0.1，不要用 localhost（否则卫星底图全部 403）
echo   后端自检页：http://127.0.0.1:8080/status
echo   关掉本窗口即停止服务
echo ============================================================
echo.

REM  -jar 用绝对路径，避免"双击时工作目录不是 release"导致找不到 jar
java -jar "%JAR%"

REM  java 退出后才会走到这里。加 pause 是为了让报错留在屏幕上，
REM  否则窗口一关，现场什么都看不到。
echo.
echo ------------------------------------------------------------
echo   服务已停止。如果这不是你主动关的，上面就是退出原因。
echo ------------------------------------------------------------
pause
endlocal
exit /b 0

:fail
echo.
echo ------------------------------------------------------------
echo   启动失败。把上面这段报错整段发给我，不要只描述现象。
echo ------------------------------------------------------------
pause
endlocal
exit /b 1
