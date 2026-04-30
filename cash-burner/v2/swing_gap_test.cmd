@echo off
chcp 65001 >/dev/null
set PYTHONUTF8=1
cd /d %~dp0

echo.
echo === Gap reversion: 5 variants ===
echo.

python -X utf8 run_gap_bt.py --start 20200101 --end 20260401 --all-variants

echo.
pause
