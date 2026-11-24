@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion

REM ============================================================================
REM  城市内涝风险可视化平台 · 一键演示包构建（只在开发机上跑一次）
REM ----------------------------------------------------------------------------
REM  为什么要这个脚本？
REM    演示现场只有"双击一个 bat"这一种操作空间。构建过程（npm run build、
REM    mvn package、组装目录）全放在开发机一次做完，现场不做任何编译。
REM
REM  产物：<项目根>\release\
REM    floodgis-demo.jar           后端 + 前端 + Cesium 全在一个 jar 里
REM    sql\floodgis_demo.dump      PostgreSQL 自定义格式备份，现场自建库用
REM    启动演示.bat                 现场唯一要双击的文件
REM    演示包说明.txt               给演示当天的自己看的三步操作卡
REM
REM  关于编码：本文件是 GBK(cp936) 编码 + CRLF 换行，第二行显式把控制台切到 936。
REM    为什么不能用 UTF-8（实测，不是猜的）：
REM    cmd.exe 解析批处理时按"一个字符 = 一个字节"计算文件读取偏移。UTF-8 的汉字
REM    占 3 字节，一旦某行出现中文，之后所有行的解析起点都会错位，症状极其荒谬：
REM    @echo off 被读成 cho，echo 后面的中文被当成命令去执行，
REM    满屏 "不是内部或外部命令，也不是可运行的程序"。
REM    带 BOM 的 UTF-8 也一样错（BOM 反而多出一行 '@echo' 报错）。
REM    GBK 的汉字是 2 字节，与 cmd 的假设自洽，所以解析正常、中文显示也正常。
REM    代价是：用 VS Code 打开若显示乱码，右下角编码切到 GB2312/GBK 即可；
REM    改动后必须仍然存成 GBK，存成 UTF-8 会让脚本直接崩。
REM ============================================================================

set "SCRIPTDIR=%~dp0"
set "ROOT=%~dp0.."
set "REL=%ROOT%\release"
set "BACKEND_JAR=floodgis-backend-0.0.1-SNAPSHOT.jar"

echo ============================================================
echo   城市内涝平台 · 演示包构建
echo ============================================================
echo.

REM ---------------------------------------------------------------------------
REM  第 1 步：构建前端
REM  必须先有 frontend/dist，pom.xml 里的 with-frontend profile 才会被激活，
REM  否则打出来的 jar 只有后端，首页打开是 404 —— 这个坑很难从报错里看出来。
REM ---------------------------------------------------------------------------
echo [1/5] 构建前端（npm run build）...
cd /d "%ROOT%\frontend"
if errorlevel 1 (
    echo       [失败] 进不去 frontend 目录：%ROOT%\frontend
    goto :fail
)
if not exist "package.json" (
    echo       [失败] frontend 目录里没有 package.json，路径可能不对
    goto :fail
)
if not exist "node_modules" (
    echo       [提示] 没有 node_modules，先执行 npm install
    cd /d "%ROOT%\frontend"
    call npm install
    if errorlevel 1 (
        echo       [失败] npm install 失败
        goto :fail
    )
)
call npm run build
if errorlevel 1 (
    echo       [失败] npm run build 失败，看上面的报错
    goto :fail
)
if not exist "%ROOT%\frontend\dist\index.html" (
    echo       [失败] 构建结束但没有 frontend\dist\index.html
    goto :fail
)
echo       完成，dist 已生成

REM ---------------------------------------------------------------------------
REM  第 2 步：找 Maven
REM  本机 Maven 不在 PATH 上，只有 IDEA 自带的那一份。
REM  所以先按常规找 PATH，找不到再退到 IDEA 的安装路径。
REM  两个都没有就只能让人在 IDEA 里点 Maven 面板了，硬编码到这个程度是因为
REM  演示机器只有这一台，为它多写一套 wrapper 不划算。
REM ---------------------------------------------------------------------------
echo [2/5] 定位 Maven...
set "MVN="
where mvn >nul 2>&1
if not errorlevel 1 set "MVN=mvn"
if not defined MVN if exist "D:\dev\JetBrains\plugins\maven-plugin\lib\maven3\bin\mvn.cmd" set "MVN=D:\dev\JetBrains\plugins\maven-plugin\lib\maven3\bin\mvn.cmd"
if not defined MVN (
    echo       [失败] 没找到 Maven。请先在 IDEA 里执行 Maven -^> package，
    echo              或者把 Maven 的 bin 目录加进 PATH 后重跑本脚本。
    goto :fail
)
echo       使用：%MVN%

REM ---------------------------------------------------------------------------
REM  第 3 步：打包后端
REM  注意 call：mvn 是 .cmd 批处理，直接调用会夺走控制权，
REM  本脚本后面的组装步骤就全被跳过了 —— 必须 call。
REM  -DskipTests：演示包不需要跑单测，省 30 秒。
REM
REM  为什么必须是 clean package，不能只写 package（踩过一次，别改回去）：
REM  pom 里把前端 dist 复制进 target/classes/static 用的是 copy-resources，
REM  它只负责覆盖同名文件，**不会删除源目录里已经消失的文件**。
REM  于是上一版打进 target 的旧资源会一直留在那里，再打 jar 时又被一起带上。
REM  真实事故：时间轴的帧从 7 个 png 换成 153 个 webp、旧的 7 个 png 已从
REM  frontend/public/risk/ 删掉，但 jar 里仍然躺着那 7 个 png（4.7 MB 死资源），
REM  数 jar 内文件会对不上 dist，而且谁也不知道哪些是当前有效的。
REM  clean 先把 target 整个删掉，保证 jar 里的静态资源和 frontend/dist 严格一致。
REM ---------------------------------------------------------------------------
echo [3/5] 打包后端（mvn clean package -DskipTests）...
cd /d "%ROOT%\backend"
if errorlevel 1 (
    echo       [失败] 进不去 backend 目录：%ROOT%\backend
    goto :fail
)
call "%MVN%" clean package -DskipTests
if errorlevel 1 (
    echo       [失败] mvn package 失败，看上面的报错
    goto :fail
)
if not exist "target\%BACKEND_JAR%" (
    echo       [失败] 没找到 target\%BACKEND_JAR%
    goto :fail
)
echo       完成，jar 已生成

REM ---------------------------------------------------------------------------
REM  第 4 步：组装 release 目录
REM  每次都整个重建，避免上一次的残留文件混进来（比如改了 jar 名还留着旧 jar）。
REM ---------------------------------------------------------------------------
echo [4/5] 组装 release 目录...
if exist "%REL%" rmdir /s /q "%REL%"
mkdir "%REL%"
if errorlevel 1 (
    echo       [失败] 建不了 %REL%，可能有文件被占用
    goto :fail
)
mkdir "%REL%\sql"

copy /y "%ROOT%\backend\target\%BACKEND_JAR%" "%REL%\floodgis-demo.jar" >nul
if errorlevel 1 (
    echo       [失败] 复制 jar 失败
    goto :fail
)

REM  数据库备份缺失不算致命错误，但现场一定跑不起来，所以明确警告
if not exist "%ROOT%\sql\floodgis_demo.dump" (
    echo       [警告] 缺少 sql\floodgis_demo.dump，现场无法自建库
    echo              先执行：pg_dump -h localhost -U postgres -Fc --no-owner --no-privileges -f sql/floodgis_demo.dump floodgis
) else (
    copy /y "%ROOT%\sql\floodgis_demo.dump" "%REL%\sql\floodgis_demo.dump" >nul
    if errorlevel 1 (
        echo       [失败] 复制数据库备份失败
        goto :fail
    )
)

copy /y "%SCRIPTDIR%启动演示.bat" "%REL%\启动演示.bat" >nul
if errorlevel 1 (
    echo       [失败] 复制 启动演示.bat 失败
    goto :fail
)
copy /y "%SCRIPTDIR%演示包说明.txt" "%REL%\演示包说明.txt" >nul
if errorlevel 1 (
    echo       [失败] 复制 演示包说明.txt 失败
    goto :fail
)
echo       完成

REM ---------------------------------------------------------------------------
REM  第 5 步：报告产物，顺手做一次体积自检
REM  jar 正常应该在 30-60 MB（前端 + Cesium 的模型/贴图都进去了）。
REM  如果只有几 MB，多半是 with-frontend profile 没激活，前端根本没打进去。
REM ---------------------------------------------------------------------------
echo [5/5] 产物清单...
echo.
for %%F in ("%REL%\floodgis-demo.jar") do set "JARBYTES=%%~zF"
if exist "%REL%\sql\floodgis_demo.dump" (
    for %%F in ("%REL%\sql\floodgis_demo.dump") do set "DUMPBYTES=%%~zF"
) else (
    set "DUMPBYTES=0"
)
set /a JARMB=!JARBYTES!/1048576
set /a DUMPMB=!DUMPBYTES!/1048576

echo       floodgis-demo.jar       !JARMB! MB
echo       sql\floodgis_demo.dump  !DUMPMB! MB
echo       启动演示.bat
echo       演示包说明.txt
echo.
if !JARBYTES! LSS 10485760 (
    echo       [警告] jar 不到 10 MB，前端很可能没打进去。
    echo              检查 backend\target\classes\static\index.html 是否存在。
    echo.
)

echo ============================================================
echo   打包完成：%REL%
echo   把这个目录整个拷到演示机，双击 启动演示.bat 即可。
echo ============================================================
endlocal
exit /b 0

:fail
echo.
echo ------------------------------------------------------------
echo   打包未完成，上面的报错可以整段发给我。
echo ------------------------------------------------------------
endlocal
exit /b 1
