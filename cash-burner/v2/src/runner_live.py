# src/runner_live.py
from __future__ import annotations

import os, time
from parser import extract_raw, parse_ws_raw_multi
from engine_simple import EngineSimple

def parse_prefix_ts(line: str) -> float:
    if "\t" not in line:
        return time.time()
    prefix = line.split("\t",1)[0].strip()
    try:
        st = time.strptime(prefix, "%Y-%m-%d %H:%M:%S")
        return time.mktime(st)
    except Exception:
        return time.time()

def _resolve_in_file(path: str) -> str:
    if "{date}" in path:
        return path.replace("{date}", time.strftime("%Y%m%d"))
    return path

def _sym_price_from_trade_row(row: dict) -> tuple[str, float]:
    sym = str(row.get("MKSC_SHRN_ISCD") or row.get("mksc_shrn_iscd") or "").strip()
    price = 0.0
    for k in ("STCK_PRPR", "stck_prpr", "STCK_CLPR", "stck_clpr"):
        try:
            v = float(str(row.get(k, 0) or 0).replace(",", ""))
            if v > 0:
                price = v
                break
        except Exception:
            pass
    return sym, price


def run_live(in_file: str, poll: float = 0.2, cap=None):
    _last_timer = [0.0]
    eng = EngineSimple()
    while True:
        try:
            live_path = _resolve_in_file(in_file)
            with open(live_path, "r", encoding="utf-8") as f:
                f.seek(0,2)
                print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE] {live_path}")
                while True:
                    line = f.readline()
                    if line:
                        ts_epoch = parse_prefix_ts(line.rstrip("\n"))
                        raw = extract_raw(line)
                        for tr_id, row in parse_ws_raw_multi(raw):
                            vlen=int(row.get("_values_len","0"))
                            clen=int(row.get("_cols_len","0"))
                            if vlen!=clen:
                                continue
                            try:
                                if tr_id=="H0STASP0":
                                    eng.on_orderbook(row, ts_epoch)
                                else:
                                    eng.on_trade(row, ts_epoch)
                                    if cap is not None:
                                        sym, px = _sym_price_from_trade_row(row)
                                        if sym and px > 0:
                                            cap.feed_leader_price(sym, px, ts_epoch)
                            except Exception as e:
                                print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE][WARN] {tr_id} {type(e).__name__}: {e}")
                    else:
                        now = time.time()
                        if now - _last_timer[0] >= 1.0:
                            _last_timer[0] = now
                            try:
                                eng.on_timer(now)
                            except Exception as e:
                                print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE][WARN] timer {type(e).__name__}: {e}")
                        time.sleep(poll)
        except FileNotFoundError:
            now = time.time()
            if now - _last_timer[0] >= 1.0:
                _last_timer[0] = now
                try:
                    eng.on_timer(now)
                except Exception as e:
                    print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE][WARN] timer(no-file) {type(e).__name__}: {e}")
            time.sleep(max(1.0, poll))
        except Exception as e:
            print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE][ERR] {type(e).__name__}: {e}")
            time.sleep(1)
