@echo off
chcp 65001 >/dev/null
set PYTHONUTF8=1
cd /d %~dp0

echo.
echo === Swing backtest: 4 strategies ===
echo.

python -X utf8 run_swing_bt.py --strategy all --start 20200101 --end 20260401 --topn 20 --rebalance M --report-out data/swing_results

echo.
echo === Done. See data/swing_results/ ===
pause
