@echo off
chcp 65001 >nul
echo ============================================================
echo  MinerU TUN Bypass - 添加持久直连路由
echo  需要以管理员身份运行！
echo ============================================================
echo.

REM 先清理旧路由
for %%i in (47.117.165.160 47.242.87.119 47.243.171.228 13.223.25.84 54.243.117.197) do (
    route delete %%i >nul 2>&1
)

REM 检测真实网关
for /f "tokens=3" %%a in ('route print 0.0.0.0 ^| findstr "0.0.0.0.*0.0.0.0"') do (
    set "GW=%%a"
    goto :found_gw
)
:found_gw

if "%GW%"=="" (
    echo [ERROR] 未检测到网关，请确认网络连接正常
    pause
    exit /b 1
)

if "%GW:~0,6%"=="198.18" (
    echo [WARNING] 检测到 TUN 虚拟网关 %GW%，尝试寻找真实网关...
    REM 用 netsh 获取
    for /f "tokens=2 delims=:" %%a in ('netsh interface ip show config ^| findstr /i "Default Gateway 默认网关"') do (
        set "GW=%%a"
    )
    set "GW=%GW: =%"
)

echo 使用网关: %GW%
echo.

REM 添加持久路由（-p = persistent）
echo 添加 MinerU 直连路由...
route add 47.117.165.160 mask 255.255.255.255 %GW% -p
route add 47.242.87.119 mask 255.255.255.255 %GW% -p
route add 47.243.171.228 mask 255.255.255.255 %GW% -p
route add 13.223.25.84 mask 255.255.255.255 %GW% -p
route add 54.243.117.197 mask 255.255.255.255 %GW% -p

echo.
echo 验证路由:
route print 47.117.165.160 | findstr "47.117"
route print 47.242.87.119 | findstr "47.242"
route print 47.243.171.228 | findstr "47.243"
route print 13.223.25.84 | findstr "13.223"
route print 54.243.117.197 | findstr "54.243"
echo.
echo ============================================================
echo  完成！MinerU 的 5 个 IP 已添加持久直连路由。
echo  即使 TUN 模式开着，这些 IP 也会走真实网卡直连。
echo.
echo  如果以后 MinerU IP 变了（CDN 更新），重新运行此脚本。
echo  运行方式: 右键此文件 - 以管理员身份运行
echo ============================================================
pause
