# src/runner_live.py
from __future__ import annotations

import os, time
from parser import extract_raw, parse_ws_raw_multi
from engine_real import EngineReal

def parse_prefix_ts(line: str) -> float:
    if "\t" not in line:
        return time.time()
    prefix = line.split("\t",1)[0].strip()
    try:
        st = time.strptime(prefix, "%Y-%m-%d %H:%M:%S")
        return time.mktime(st)
    except Exception:
        return time.time()

def run_live(in_file: str, poll: float = 0.2):
    eng = EngineReal()
    while True:
        try:
            with open(in_file, "r", encoding="utf-8") as f:
                f.seek(0,2)
                print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE] {in_file}")
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
                            except Exception as e:
                                print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE][WARN] {tr_id} {type(e).__name__}: {e}")
                    else:
                        time.sleep(poll)
        except FileNotFoundError:
            time.sleep(1)
        except Exception as e:
            print(time.strftime("%Y-%m-%d %H:%M:%S"), f"[LIVE][ERR] {type(e).__name__}: {e}")
            time.sleep(1)
