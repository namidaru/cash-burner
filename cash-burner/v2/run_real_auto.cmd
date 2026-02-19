@echo off
REM === AUTO (RANKING API v1_국내주식-104) + WS TRACK + REAL ORDER ===
REM required env:
REM   KOREA_INVEST_APP_KEY
REM   KOREA_INVEST_APP_SECRET
REM   KOREA_INVEST_ACC_NO   (10 digits or 8-02)

REM use ranking markets: J (KRX), NX (NXT). You can do: set RANK_MARKETS=J,NX
set RANK_MARKETS=J

REM how often to refresh ranking/watchlist (seconds)
set SCAN_INTERVAL_SEC=10
set WATCH_TOP_N=30

REM ranking query defaults (change if needed)
set FID_COND_SCR_DIV_CODE=20186
set FID_RANK_SORT_CLS_CODE=1
set FID_INPUT_ISCD=0000

REM watchlist filters
set ENTRY_BLOCK_DAYRISE_PCT=12.0
set WATCH_MIN_TR_VALUE=300000000

REM sizing: 30% per trade
set POSITION_PCT=0.30

REM entry filters
set WINDOW_SEC=10
set MIN_RET_PCT=0.60
set MIN_TICK_COUNT=10
set MIN_TR_VALUE=0
set MIN_IMB=0.60
set MAX_SPREAD_PCT=0.30

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
