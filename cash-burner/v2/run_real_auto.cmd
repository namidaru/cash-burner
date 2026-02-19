@echo off
cd /d %~dp0
set PYTHONPATH=%CD%\src;%PYTHONPATH%
REM === AUTO (RANKING API v1_국내주식-104) + WS TRACK + REAL ORDER ===
REM required env:
REM   KOREA_INVEST_APP_KEY
REM   KOREA_INVEST_APP_SECRET
REM   KOREA_INVEST_ACC_NO   (10 digits or 8-02)

REM use ranking markets: J (KRX), NX (NXT). You can do: set RANK_MARKETS=J,NX
set RANK_MARKETS=J,NX

REM how often to refresh ranking/watchlist (seconds)
set SCAN_INTERVAL_SEC=10
set WATCH_TOP_N=30

REM ranking query defaults (change if needed)
set FID_COND_SCR_DIV_CODE=20186
set FID_RANK_SORT_CLS_CODE=1
set RANK_SORT_CODES=1,0
set FID_INPUT_ISCD=0000

REM emergency fallback symbols when ranking API returns empty
set FALLBACK_SYMBOLS=
REM watchlist filters
set ENTRY_BLOCK_DAYRISE_PCT=12.0
set WATCH_MIN_TR_VALUE=300000000

REM sizing: 30% per trade
set POSITION_PCT=0.30

REM entry filters
set WINDOW_SEC=10
set ORDERBOOK_MAX_AGE_SEC=1.0
set MIN_TICKS_FOR_CALC=2

REM session presets
set OPEN_MIN_RET_PCT=0.90
set OPEN_MIN_TR_VALUE=120000000
set OPEN_MIN_TICK_COUNT=16
set OPEN_MIN_IMB=0.64
set OPEN_MAX_SPREAD_PCT=0.22
set OPEN_CONFIRM_SEC=1.2
set OPEN_COOLDOWN_SEC=180

set MID_MIN_RET_PCT=0.70
set MID_MIN_TR_VALUE=50000000
set MID_MIN_TICK_COUNT=12
set MID_MIN_IMB=0.62
set MID_MAX_SPREAD_PCT=0.25
set MID_CONFIRM_SEC=1.0
set MID_COOLDOWN_SEC=120

set CLOSE_MIN_RET_PCT=0.60
set CLOSE_MIN_TR_VALUE=80000000
set CLOSE_MIN_TICK_COUNT=10
set CLOSE_MIN_IMB=0.60
set CLOSE_MAX_SPREAD_PCT=0.20
set CLOSE_CONFIRM_SEC=1.2
set CLOSE_COOLDOWN_SEC=180

REM VI risk-off
set VI_GUARD_PCT=0.40
set VI_COOLDOWN_SEC=120
set VI_LIKE_RET_PCT_OPEN=2.5
set VI_LIKE_RET_PCT_MID=2.0
set VI_LIKE_RET_PCT_CLOSE=1.6

REM fallback/base (used if session preset missing)
set MIN_RET_PCT=0.60
set MIN_TICK_COUNT=10
set MIN_TR_VALUE=0
set MIN_IMB=0.60
set MAX_SPREAD_PCT=0.30
set CONFIRM_SEC=1.0
set COOLDOWN_SEC=120
REM exits
set HARD_STOP_PCT=3.5
set TRAIL_ARM_PCT=4.0
set TRAIL_DROP_PCT=3.5
set LIMITUP_GAP_TAKE_PCT=0.85

REM files
set OUT_FILE=data\ws_dump.log
set CONTROL_FILE=data\ws_control.log
set WATCHLIST_FILE=data\watchlist.txt
set WATCHLIST_DEBUG=data\watchlist_debug.log
set LEDGER_FILE=data\ledger_real.csv
set PREVCLOSE_CACHE=data\prev_close.json

python src\main_real.py
pause
