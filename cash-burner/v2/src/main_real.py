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

from scanner_company_rank import build_watchlist, get_last_build_meta, check_watchlist_integrity, get_last_source_map
from quote_basic import ensure_prev_close
from ws_sub_manager import write_watchlist
from ws_capture_live import WSCapture
from runner_live import run_live

def _resolve_in_file() -> str:
    explicit = (os.getenv("IN_FILE", "") or "").strip()
    if explicit:
        return explicit

    raw = os.getenv("OUT_FILE", os.path.join("data", "ws_dump.log"))
    ymd = time.strftime("%Y%m%d")
    if "{date}" in raw:
        return raw.replace("{date}", ymd)
    return raw


IN_FILE = _resolve_in_file()
LIVE_POLL_SEC = float(os.getenv("LIVE_POLL_SEC", "0.2"))
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "2"))
RADAR_SCAN_INTERVAL_SEC = float(os.getenv("RADAR_SCAN_INTERVAL_SEC", "1.0"))
WATCH_RADAR_MODE = os.getenv("WATCH_RADAR_MODE", "0") == "1"
WATCH_STABILIZE_ENABLED = os.getenv("WATCH_STABILIZE_ENABLED", "0") == "1"
WATCHLIST_DEBUG = os.getenv("WATCHLIST_DEBUG", os.path.join("data", "watchlist_debug.log"))
PREVCLOSE_WARMUP = os.getenv("PREVCLOSE_WARMUP", "1") == "1"

def _log(msg: str):
    try:
        d = os.path.dirname(WATCHLIST_DEBUG)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(WATCHLIST_DEBUG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{msg}\n")
    except Exception:
        pass

def _load_watchlist_file() -> List[str]:
    path = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []




def _stabilize_watchlist(prev: List[str], new: List[str]) -> List[str]:
    """전체 갈아끼움 대신 하위 일부만 교체해 신호 흔들림 완화."""
    if not prev:
        return new
    if not new:
        return prev

    keep_n = int(os.getenv("WATCH_KEEP_TOP_N", "20"))
    max_replace = int(os.getenv("WATCH_MAX_REPLACE", "10"))
    want_n = int(os.getenv("WATCH_TOP_N", str(len(new))))

    prev_cut = [s for s in prev[:want_n]]
    new_cut = [s for s in new[:want_n]]

    fixed = [s for s in prev_cut[:keep_n] if s in new_cut]
    result = list(fixed)

    for s in prev_cut[keep_n:]:
        if s in result:
            continue
        result.append(s)

    add_pool = [s for s in new_cut if s not in result]
    replace_n = min(max_replace, len(add_pool))

    if replace_n > 0:
        removable_idx = [i for i in range(len(result) - 1, keep_n - 1, -1)
                         if i < len(result)]
        for i in removable_idx[:replace_n]:
            if not add_pool:
                break
            result[i] = add_pool.pop(0)
    # deduplicate while preserving order
    seen = set()
    result = [s for s in result if not (s in seen or seen.add(s))]

    for s in new_cut:
        if len(result) >= want_n:
            break
        if s not in result:
            result.append(s)

    return result[:want_n]

def scanner_loop():
    _log("START scanner_company_rank mode")

    last_watch: List[str] = _load_watchlist_file()
    if last_watch:
        _log(f"BOOT keep existing watchlist n={len(last_watch)} head={last_watch[:10]}")

    while True:
        try:
            raw_watch = build_watchlist()
            watch = _stabilize_watchlist(last_watch, raw_watch) if WATCH_STABILIZE_ENABLED else raw_watch
            _log(f"rank raw_watch n={len(raw_watch)} head={raw_watch[:10]}")
            _log(f"rank stable_watch n={len(watch)} head={watch[:10]}")
            integ = check_watchlist_integrity(watch)
            _log(
                "rank integrity "
                f"total={integ['total']} unique={integ['unique']} "
                f"bad_format={integ['bad_format']} dup={integ['dup']} "
                f"low_price={integ['low_price']} quote_miss={'off' if integ['quote_miss'] < 0 else integ['quote_miss']}"
            )
            detail = get_last_build_meta()
            if detail:
                _log(f"rank detail {detail}")

            src_map = get_last_source_map()
            if src_map:
                c_rank_pref = sum(1 for s in watch if src_map.get(s) == "rank_pref")
                c_rank_backup = sum(1 for s in watch if src_map.get(s) == "rank_backup")
                c_volume_rank = sum(1 for s in watch if src_map.get(s) == "volume_rank")
                c_strength = sum(1 for s in watch if src_map.get(s) == "strength")
                c_condition = sum(1 for s in watch if src_map.get(s) == "condition")
                c_fallback = sum(1 for s in watch if src_map.get(s) == "fallback")
                _log(
                    "rank source "
                    f"rank_pref={c_rank_pref} rank_backup={c_rank_backup} "
                    f"volume_rank={c_volume_rank} strength={c_strength} "
                    f"condition={c_condition} fallback={c_fallback}"
                )

            if watch:
                # prev_close cache needed for +12% block and limitup-gap take
                if PREVCLOSE_WARMUP:
                    ensure_prev_close(watch)
                if watch != last_watch:
                    write_watchlist(watch)
                    last_watch = watch
                    _log("watchlist updated")
                else:
                    _log("watchlist unchanged")
            else:
                if last_watch:
                    _log(f"WARN empty watchlist (rank api returned none) - keep previous n={len(last_watch)}")
                else:
                    _log("WARN empty watchlist (rank api returned none) - no previous list")
        except Exception as e:
            _log(f"ERR {type(e).__name__}: {e}")
        time.sleep(RADAR_SCAN_INTERVAL_SEC if WATCH_RADAR_MODE else SCAN_INTERVAL_SEC)

def main():
    threading.Thread(target=scanner_loop, daemon=True).start()
    cap = WSCapture()
    threading.Thread(target=cap.start, daemon=True).start()
    run_live(IN_FILE, poll=LIVE_POLL_SEC)

if __name__ == "__main__":
    main()
