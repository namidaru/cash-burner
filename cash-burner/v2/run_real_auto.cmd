@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d %~dp0
set PYTHONPATH=%CD%\src;%PYTHONPATH%
REM === AUTO (RANKING API v1_국내주식-104) + WS TRACK + REAL ORDER ===
REM required env:
REM   KOREA_INVEST_APP_KEY
REM   KOREA_INVEST_APP_SECRET
REM   KOREA_INVEST_ACC_NO   (10 digits or 8-02)

REM === ENTRY_MODE: theme(네이버 테마 크롤링, 기본) or fastpath(장전 PREOPEN만) ===
REM 택1. 비활성 쪽은 shadow 파일(data/watchlist_shadow.txt)에 결과 저장.
set ENTRY_MODE=theme

set SCAN_INTERVAL_SEC=4
set WATCH_TOP_N=45
REM 테마: 후보 풀 크기 (H0STANC0 구독 대상). PREOPEN이 이 중 상위 5개 선별.
set THEME_TOP_N=45
set WATCH_STABILIZE_ENABLED=0

set FID_COND_SCR_DIV_CODE=20186
set RANK_SCR_CODES=20170
set RANK_DATE1_CANDIDATES=TODAY
set FID_RANK_SORT_CLS_CODE=1
set RANK_SORT_CODES=1
set FID_INPUT_ISCD=0000

REM 워치리스트 블랙리스트: 여기 등록된 종목은 스캐너 풀에서 제외되어 구독 슬롯을 차지하지 않음
set WATCH_BLACKLIST=005930

set WATCH_MIN_VOLUME=30000
set WATCH_MIN_VOLUME_ACCEL=0.8
set WATCH_MIN_PRICE=5000
set WATCH_MAX_PRICE=150000
REM WATCH_MIN_CHANGE_PCT: 코드에서 장전(~09:00) 자동 바이패스, 장중 0.5 적용
set WATCH_MIN_CHANGE_PCT=0.5
set WATCH_MAX_CHANGE_PCT=20.0
set WATCH_HARD_HEAT_PCT=20.0
set WATCH_MAX_SPREAD_PCT=0.35
set WATCH_MIN_STRENGTH=120
set WATCH_MAX_STRENGTH=300
set WATCH_FILL_FROM_VOLUME_RANK=1
set VOLUME_RANK_MARKETS=J
set VOLUME_RANK_SCR=20171
set VOLUME_RANK_TOPK=120
set VOLUME_RANK_TR_IDS=FHPST01710000
set STRENGTH_MARKETS=J
set STRENGTH_SCR_CODES=20170,20186
set STRENGTH_TOPK=120
set WATCH_FILL_FROM_CONDITION=0
set WATCH_COND_SEQS=
set WATCH_INTEGRITY_WITH_QUOTE=0
set PREVCLOSE_WARMUP=1

REM === ENGINE SIMPLE PARAMS ===
set PAPER_TRADE=0

REM 포지션 수 제한 완화 — 현금 잔고가 실제 제한 역할 (score별 배분: 7~25%)
set MAX_POSITIONS=5
REM 진입 마감: v4 즉시매수 — 9시 직후 교집합 종목 매수 (9:10까지)
set TRADING_END_HHMM=910
REM on_timer SELECT 비활성화: surge fast_path만 진입 경로로 사용
set TIMER_ENTRY_ENABLED=0
REM 동시호가 퀄리티 whitelist = 20종목 후보 (surge 감지 대상)
set PREOPEN_WHITELIST_N=20

REM 워치리스트 급등 차단 (엔진 기본값 12.0보다 완화)
set ENTRY_BLOCK_DAYRISE_PCT=15.0

REM 후보 신선도 유효시간 (on_timer 배치 선택용)
set CANDIDATE_FRESH_SEC=2.0

REM === 청산 파라미터 (50/50 분할청산) ===
REM A물량(50%%): trail — arm 도달 후 되돌림 시 매도
set TRAIL_ARM_PCT=5.0
set TRAIL_DROP_PCT=3.0
set TRAIL_DROP_RATIO=0.25
set TRAIL_DROP_MAX_PCT=5.0
REM B물량(50%%): SL/TP/장마감 — trail 무시, TP 도달 시 매도
set SPLIT_EXIT_ENABLED=1
set SPLIT_B_TP_PCT=20.0
REM 보유시간: 사실상 무제한. SL/trail이 청산 담당.
set MAX_HOLD_SEC=99999
REM SL: 3.5% (기존 2%→53% 손절률, 변동성 큰 급등주 노이즈 흡수)
set STOP_LOSS_PCT=3.5
set STOP_LOSS_EMERGENCY_PCT=5.0
REM grace: 30초 (시장가 체결 후 bid-ask 스프레드 노이즈 흡수 — 기존 10초 너무 짧음)
set EXIT_GRACE_SEC=30.0
set STOP_LOSS_EARLY_GRACE_SEC=15.0
REM 장마감 강제청산: 15:19에 남은 포지션 전부 정리
set FORCE_EXIT_HHMM=1519

REM === 가격 위치 게이트 (고점매수 차단) ===
REM 하락 종목 차단: dayrise <= -3% 이면 ofi 관계없이 진입 불가
set FALLING_STOCK_BLOCK_PCT=-3.0
REM 고점 근접 차단: 20초 고점 대비 0.2% 이내 + 반등 없음 + 10틱 이상
set NEAR_HIGH_BLOCK_ENABLED=1
set NEAR_HIGH_BLOCK_PCT=0.2
set NEAR_HIGH_MIN_TICKS=10
REM 거래대금 최소 (20M→10M, ret10 제거로 진입 빨라짐에 맞춤)
set BUY_TRV10_MIN=10000000

set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1469014857276850290/qvex_AKK19cczgRE7QDu-YoxrMaQu1dQkIy8_vRHJ16o61W4PkipAZ4NfV24Nim2Rk43
set DISCORD_BOT_NAME=파쇄기
set DISCORD_USE_ENV_BOT_NAME=0
set DISCORD_TIMEOUT_SEC=4
set WS_STALE_SEC=20
set WATCH_REMOVE_DELAY_SEC=0
REM fast_path v4: 테마∩랭크 교집합 즉시매수 (9시 직후)
set FAST_PATH_END_HHMM=910
set SURGE_MAX_ENTRIES_PER_SYM=1
set SURGE_ALLOC_PCT=0.20
set FAST_PATH_MAX_ENTRIES=5
REM 일일 손실 한도: 3.5% 초과 시 신규 매수 차단
set DAILY_LOSS_LIMIT_PCT=3.5
REM MORNING_REFRESH: 30s→15s (NXT 모멘텀 픽업 지연 단축)
set MORNING_REFRESH_SEC=15
REM H0STANC0 동시호가 구독 시작 시각 (8:50부터 예상체결가+잔량 수신 → PRE_SUB ba_ratio 정렬)
REM 8:45에 프로그램 시작해야 의미있음. 0으로 설정 시 비활성화.
set H0STANC0_START_HHMM=850

REM === 날짜별 로그 폴더 (data\logs\{date}\) ===
set OUT_FILE=data\logs\{date}\ws_dump.log
set CONTROL_FILE=data\logs\{date}\ws_control.log
set SIGNAL_DIAG_FILE=data\logs\{date}\signal_diag.log
set WATCHLIST_DEBUG=data\logs\{date}\watchlist_debug.log
set WATCHLIST_FILE=data\watchlist.txt
set LEDGER_FILE=data\ledger_real.csv
set PREVCLOSE_CACHE=data\prev_close.json

python src\main_real.py
pause
