@echo off
chcp 65001 >/dev/null
set PYTHONUTF8=1
cd /d %~dp0

echo.
echo === Swing data update ===
echo Daily candles 2018-01-01 ~ today
echo Estimated: 30~60 min
echo.

if "%KOREA_INVEST_APP_KEY%"=="" (
    echo [ERROR] KOREA_INVEST_APP_KEY not set.
    pause
    exit /b 1
)

python -X utf8 run_swing_bt.py --update --syms-from universe --start 20180101
pause
