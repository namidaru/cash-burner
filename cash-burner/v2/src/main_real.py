# src/main_real.py
from __future__ import annotations

import os
import time
import threading
from typing import List

from scanner_company_rank import build_watchlist, get_last_build_meta, check_watchlist_integrity, get_last_source_map
from quote_basic import ensure_prev_close
from ws_sub_manager import write_watchlist
from ws_capture_live import WSCapture
from runner_live import run_live

IN_FILE = os.getenv("IN_FILE", os.path.join("data", "ws_dump.log"))
LIVE_POLL_SEC = float(os.getenv("LIVE_POLL_SEC", "0.2"))
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "10"))
WATCHLIST_DEBUG = os.getenv("WATCHLIST_DEBUG", os.path.join("data", "watchlist_debug.log"))

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


def scanner_loop():
    _log("START scanner_company_rank mode")

    last_watch: List[str] = _load_watchlist_file()
    if last_watch:
        _log(f"BOOT keep existing watchlist n={len(last_watch)} head={last_watch[:10]}")

    while True:
        try:
            watch = build_watchlist()
            _log(f"rank watchlist n={len(watch)} head={watch[:10]}")
            integ = check_watchlist_integrity(watch)
            _log(
                "rank integrity "
                f"total={integ['total']} unique={integ['unique']} "
                f"bad_format={integ['bad_format']} dup={integ['dup']} "
                f"low_price={integ['low_price']} quote_miss={integ['quote_miss']}"
            )
            detail = get_last_build_meta()
            if detail:
                _log(f"rank detail {detail}")

            src_map = get_last_source_map()
            if src_map:
                c_rank_pref = sum(1 for s in watch if src_map.get(s) == "rank_pref")
                c_rank_backup = sum(1 for s in watch if src_map.get(s) == "rank_backup")
                c_strength = sum(1 for s in watch if src_map.get(s) == "strength")
                c_condition = sum(1 for s in watch if src_map.get(s) == "condition")
                c_fallback = sum(1 for s in watch if src_map.get(s) == "fallback")
                _log(
                    "rank source "
                    f"rank_pref={c_rank_pref} rank_backup={c_rank_backup} "
                    f"strength={c_strength} condition={c_condition} fallback={c_fallback}"
                )

            if watch:
                # prev_close cache needed for +12% block and limitup-gap take
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
        time.sleep(SCAN_INTERVAL_SEC)

def main():
    threading.Thread(target=scanner_loop, daemon=True).start()
    cap = WSCapture()
    threading.Thread(target=cap.start, daemon=True).start()
    run_live(IN_FILE, poll=LIVE_POLL_SEC)

if __name__ == "__main__":
    main()
