# src/main_real.py
from __future__ import annotations

import os
import time
import threading
from typing import List

os.environ.setdefault("OUT_FILE", os.path.join("data", "ws_dump.log"))
os.environ.setdefault("CONTROL_FILE", os.path.join("data", "ws_control.log"))
os.environ.setdefault("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
os.environ.setdefault("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
os.environ.setdefault("POSITION_STATE_FILE", os.path.join("data", "positions_real.json"))
os.environ.setdefault("AUTO_POSITION_LOG_FILE", os.path.join("data", "auto_positions_real.csv"))
os.environ.setdefault("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag.log"))
os.environ.setdefault("WATCHLIST_DEBUG", os.path.join("data", "watchlist_debug.log"))

# 로그 경로의 {date} 패턴을 오늘 날짜로 치환 (모든 모듈이 import 전에 실행)
_today = time.strftime("%Y%m%d")
for _key in ("OUT_FILE", "CONTROL_FILE", "SIGNAL_DIAG_FILE", "WATCHLIST_DEBUG"):
    _v = os.environ.get(_key, "")
    if "{date}" in _v:
        os.environ[_key] = _v.replace("{date}", _today)

from scanner_company_rank import build_watchlist as build_watchlist_rank, get_last_build_meta as get_rank_meta
from scanner_theme import build_watchlist_theme, get_last_build_meta as get_theme_meta, get_last_theme_detail, refresh_prev_day_cache
from quote_basic import ensure_prev_close
from ws_sub_manager import write_watchlist
from ws_capture_live import WSCapture
from runner_live import run_live
from engine_simple import EngineSimple

# ENTRY_MODE: "theme" (네이버 테마 크롤링) or "fastpath" (KIS 등락률/거래대금 랭크)
# 양쪽 다 45개 후보 풀 → H0STANC0 → PREOPEN 퀄리티 상위 5개 확정.
# 비활성 쪽은 shadow 파일에 결과 저장 (비교용).
ENTRY_MODE = os.getenv("ENTRY_MODE", "theme").lower()

SHADOW_WATCHLIST = os.path.join(
    os.path.dirname(os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))),
    "watchlist_shadow.txt",
)

def _raw_in_file() -> str:
    """{date} 미치환 원본 그대로 반환. runner_live가 매 루프마다 그 시점 날짜로 치환."""
    explicit = (os.getenv("IN_FILE", "") or "").strip()
    if explicit:
        return explicit
    return os.getenv("OUT_FILE", os.path.join("data", "ws_dump.log"))


IN_FILE                = _raw_in_file()
LIVE_POLL_SEC          = float(os.getenv("LIVE_POLL_SEC", "0.2"))
SCAN_INTERVAL_SEC      = float(os.getenv("SCAN_INTERVAL_SEC", "2"))
WATCHLIST_DEBUG        = os.getenv("WATCHLIST_DEBUG", os.path.join("data", "watchlist_debug.log"))
PREVCLOSE_WARMUP       = os.getenv("PREVCLOSE_WARMUP", "1") == "1"

# ── 장중 워치리스트 확장 (9:15~10:00) ──
MIDDAY_EXPAND_ENABLED  = os.getenv("MIDDAY_EXPAND_ENABLED", "1") == "1"
MIDDAY_EXPAND_START    = int(os.getenv("MIDDAY_EXPAND_START_HHMM", "915"))
MIDDAY_EXPAND_END      = int(os.getenv("MIDDAY_EXPAND_END_HHMM", "1000"))
MIDDAY_EXPAND_INTERVAL = float(os.getenv("MIDDAY_EXPAND_INTERVAL_SEC", "90"))
MIDDAY_EXPAND_MAX      = int(os.getenv("MIDDAY_EXPAND_MAX_WATCH", "20"))

def _log(msg: str):
    try:
        d = os.path.dirname(WATCHLIST_DEBUG)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(WATCHLIST_DEBUG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{msg}\n")
    except Exception:
        pass


def _write_shadow(syms: List[str], label: str):
    """shadow 워치리스트 파일에 기록 (비활성 모드의 종목 저장용)."""
    try:
        tmp = SHADOW_WATCHLIST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"# shadow [{label}] {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            for s in syms:
                f.write(s.strip() + "\n")
        os.replace(tmp, SHADOW_WATCHLIST)
        _log(f"shadow[{label}] wrote {len(syms)} syms")
    except Exception as e:
        _log(f"shadow[{label}] write ERR {e}")


def _run_active_scan() -> List[str]:
    """양쪽 스캐너 교집합: 테마(모멘텀) ∩ 랭크(거래량) → 즉시매수 대상."""
    # 1) 테마 스캐너
    theme_watch: List[str] = []
    try:
        theme_watch = build_watchlist_theme()
        meta = get_theme_meta()
        _log(f"theme result n={len(theme_watch)} head={theme_watch[:10]}")
        if meta:
            _log(f"theme meta {meta}")
        for dl in get_last_theme_detail()[:5]:
            _log(f"theme detail {dl}")
    except Exception as e:
        _log(f"theme scan ERR {type(e).__name__}: {e}")

    # 2) 랭크 스캐너 (거래량/거래대금)
    rank_watch: List[str] = []
    try:
        rank_watch = build_watchlist_rank()
        rank_meta = get_rank_meta()
        _log(f"rank result n={len(rank_watch)} head={rank_watch[:10]}")
        if rank_meta:
            _log(f"rank meta {rank_meta}")
    except Exception as e:
        _log(f"rank scan ERR {type(e).__name__}: {e}")

    # 3) 교집합 (테마 순서 유지)
    rank_set = set(rank_watch)
    intersect = [s for s in theme_watch if s in rank_set]
    _log(f"INTERSECT theme={len(theme_watch)} rank={len(rank_watch)} "
         f"overlap={len(intersect)} syms={intersect[:15]}")

    # shadow: 양쪽 전체를 기록 (비교용)
    _write_shadow(theme_watch, "theme_full")
    _write_shadow(rank_watch, "rank_full")

    if not intersect:
        _log("INTERSECT empty — no candidates today")
    return intersect


def _read_current_watchlist() -> List[str]:
    """현재 watchlist.txt 내용을 읽어서 리스트로 반환."""
    wl_path = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
    try:
        with open(wl_path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def _expand_midday_watchlist(eng: "EngineSimple | None") -> int:
    """장중 워치리스트 확장: whitelist+보유 보호 + 거래량/체결강도 후보 추가.

    Returns: 최종 워치리스트 종목 수.
    """
    # 1. 보호 대상: 현재 watchlist(whitelist 포함) + 보유 포지션
    current = _read_current_watchlist()
    held = set(eng.pos.keys()) if eng else set()
    protected = list(dict.fromkeys(current + sorted(held)))  # 순서 보존, 중복 제거

    # 2. 장중 스캔 (거래량+체결강도 기반)
    try:
        candidates = build_watchlist_rank()
        meta = get_rank_meta()
        _log(f"MIDDAY_SCAN rank n={len(candidates)} meta={meta}")
    except Exception as e:
        _log(f"MIDDAY_SCAN ERR {type(e).__name__}: {e}")
        return len(protected)

    # 3. 보호 종목 유지 + 새 후보 추가 (상한까지)
    merged = list(protected)
    new_added: List[str] = []
    for sym in candidates:
        if len(merged) >= MIDDAY_EXPAND_MAX:
            break
        if sym not in set(merged):
            merged.append(sym)
            new_added.append(sym)

    # 4. 새 종목 prev_close 보충
    if new_added and PREVCLOSE_WARMUP:
        try:
            ensure_prev_close(new_added)
        except Exception as e:
            _log(f"MIDDAY_PREVCLOSE ERR {type(e).__name__}: {e}")

    # 5. watchlist.txt에 쓰기
    write_watchlist(merged)
    if new_added:
        _log(f"MIDDAY_EXPAND protected={len(protected)} +new={len(new_added)} "
             f"total={len(merged)} new={new_added[:10]}")

    return len(merged)


def scanner_loop(cap: "WSCapture | None" = None, eng: "EngineSimple | None" = None):
    """8:45~8:54 반복 스캔 → 45개 후보 풀 확보 → PREOPEN이 8:59에 5개 확정.
    9:15~10:00 장중 확장 → 빈 WS 슬롯에 거래량/체결강도 상위 종목 추가.

    활성 스캐너 → watchlist.txt (H0STANC0 구독 대상)
    비활성 스캐너 → watchlist_shadow.txt (비교용)
    종목선정 실패 → watchlist 비움 → H0STANC0 구독 없음 → 매수 없음
    """
    _log(f"START entry_mode={ENTRY_MODE}")
    _commit_hhmm = int(os.getenv("SCAN_COMMIT_HHMM", "854"))
    _boot_hhmm = int(time.strftime("%H%M"))

    _pending_watch: List[str] = []
    _last_midday_scan: float = 0.0  # 장중 확장 마지막 실행 시각
    if _boot_hhmm < 855:
        _pool_done_today: str = ""
        try:
            write_watchlist([])
        except Exception:
            pass
        _log("BOOT pre-market — watchlist cleared")
    else:
        # 장중 재시작: 워치리스트 파일 유지, 스캐너 재실행 방지
        _pool_done_today = time.strftime("%Y%m%d")
        _log("BOOT mid-session — keeping watchlist, scanner skip")

    while True:
        try:
            now_hhmm = int(time.strftime("%H%M"))
            today = time.strftime("%Y%m%d")

            if now_hhmm >= 1530 or now_hhmm < 845:
                # 장 마감 직후 1회: 캐시 갱신 + compact + 로그 정리
                if now_hhmm >= 1530 and now_hhmm < 1600 and _pool_done_today == today:
                    try:
                        refresh_prev_day_cache()
                        _log("EOD theme prev-day cache refreshed")
                    except Exception as e:
                        _log(f"EOD cache refresh err: {e}")
                    try:
                        from daily_compact import process_date, cleanup_old_logs
                        _log(f"EOD compact start date={today}")
                        process_date(today)
                        _log("EOD compact done")
                    except Exception as e:
                        _log(f"EOD compact err: {e}")
                    _pool_done_today = today + "_eod"  # 1회만 실행
                time.sleep(30)
                continue

            # ── 855 이후 ──
            if now_hhmm >= 855:
                if _pool_done_today != today:
                    if _pending_watch:
                        if PREVCLOSE_WARMUP:
                            ensure_prev_close(_pending_watch)
                        write_watchlist(_pending_watch)
                        _log(f"POOL late-commit n={len(_pending_watch)}")
                    else:
                        write_watchlist([])
                        _log("POOL no candidates — watchlist cleared")
                    _pool_done_today = today

                # ── 장중 워치리스트 확장 (9:15~10:00, 90초 간격) ──
                if (MIDDAY_EXPAND_ENABLED
                        and MIDDAY_EXPAND_START <= now_hhmm < MIDDAY_EXPAND_END
                        and (time.time() - _last_midday_scan) >= MIDDAY_EXPAND_INTERVAL):
                    try:
                        n = _expand_midday_watchlist(eng)
                        _last_midday_scan = time.time()
                    except Exception as e:
                        _log(f"MIDDAY_EXPAND ERR {type(e).__name__}: {e}")
                        _last_midday_scan = time.time()

                time.sleep(30)
                continue

            if _pool_done_today == today:
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── 8:45~8:54: 활성 스캐너 ──
            watch = _run_active_scan()

            if watch:
                _pending_watch = watch
                # 즉시 watchlist 반영 → H0STANC0이 8:50부터 구독 가능
                write_watchlist(watch)
                _log(f"SCAN wrote n={len(watch)} to watchlist (H0STANC0 pool)")

            # ── 8:54 최종 커밋 ──
            if now_hhmm >= _commit_hhmm:
                if _pending_watch:
                    if PREVCLOSE_WARMUP:
                        ensure_prev_close(_pending_watch)
                    write_watchlist(_pending_watch)
                    _log(f"POOL committed n={len(_pending_watch)} at hhmm={now_hhmm}")
                else:
                    write_watchlist([])
                    _log(f"POOL scanner FAILED — watchlist cleared at hhmm={now_hhmm}")
                _pool_done_today = today

        except Exception as e:
            _log(f"ERR {type(e).__name__}: {e}")
        time.sleep(SCAN_INTERVAL_SEC)


def main():
    eng = EngineSimple()
    cap = WSCapture(engine=eng)
    threading.Thread(target=cap.start, daemon=True).start()
    threading.Thread(target=scanner_loop, args=(cap, eng), daemon=True).start()
    run_live(IN_FILE, poll=LIVE_POLL_SEC, cap=cap, eng=eng)


if __name__ == "__main__":
    main()
