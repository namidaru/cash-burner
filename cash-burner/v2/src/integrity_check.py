from __future__ import annotations

import csv
import os
import time
from collections import defaultdict
from typing import Dict, List


DEFAULT_LEDGER = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
DEFAULT_EQUITY = os.getenv("EQUITY_FILE", os.path.join("data", "equity.csv"))
DEFAULT_SIGNAL_DIAG = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag_simple.log"))


def _to_float(v: str, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _to_int(v: str, d: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return d


def check_ledger(path: str) -> Dict[str, object]:
    issues: List[str] = []
    by_symbol = defaultdict(int)
    open_ts: Dict[str, float] = {}
    buys = sells = 0

    if not os.path.exists(path):
        return {"issues": [f"ledger file not found: {path}"], "buys": 0, "sells": 0, "open_symbols": {}, "open_age_sec": {}, "max_open_age_sec": 0.0}

    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"ts", "action", "symbol", "qty", "price", "reason"}
        if not required.issubset(set(reader.fieldnames or [])):
            issues.append(f"missing required columns: {sorted(required - set(reader.fieldnames or []))}")
        for i, row in enumerate(reader, start=2):
            action = (row.get("action") or "").upper()
            sym = row.get("symbol") or ""
            qty = _to_int(row.get("qty", "0"))
            price = _to_float(row.get("price", "0"))
            ts = _to_float(row.get("ts", "0"))

            if action not in {"BUY", "SELL"}:
                continue
            if not sym:
                issues.append(f"line {i}: empty symbol")
                continue
            if qty <= 0:
                issues.append(f"line {i}: non-positive qty ({qty})")
            if price <= 0:
                issues.append(f"line {i}: non-positive price ({price})")

            if action == "BUY":
                buys += 1
                by_symbol[sym] += qty
                if by_symbol[sym] > 0 and sym not in open_ts:
                    open_ts[sym] = ts
            else:
                sells += 1
                by_symbol[sym] -= qty
                if by_symbol[sym] <= 0:
                    open_ts.pop(sym, None)
                if by_symbol[sym] < 0:
                    issues.append(f"line {i}: sold before buy ({sym})")

    open_symbols = {s: q for s, q in by_symbol.items() if q > 0}
    now = time.time()
    open_age_sec = {}
    for s in open_symbols:
        if s in open_ts:
            open_age_sec[s] = max(0.0, now - open_ts[s])
        else:
            open_age_sec[s] = -1.0  # sentinel: buy timestamp missing
    max_open_age_sec = max(open_age_sec.values(), default=0.0)
    max_expect_open_sec = float(os.getenv("MAX_EXPECT_OPEN_SEC", "1200"))
    if max_open_age_sec > max_expect_open_sec:
        issues.append(f"open position age exceeds threshold: {max_open_age_sec:.0f}s > {max_expect_open_sec:.0f}s")

    return {
        "issues": issues,
        "buys": buys,
        "sells": sells,
        "open_symbols": open_symbols,
        "open_age_sec": {k: round(v, 1) for k, v in open_age_sec.items()},
        "max_open_age_sec": round(max_open_age_sec, 1),
    }


def check_equity(path: str) -> Dict[str, object]:
    if not os.path.exists(path):
        return {"issues": [f"equity file not found: {path}"], "start": None, "end": None, "ret_pct": None}

    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        return {"issues": ["equity file has no rows"], "start": None, "end": None, "ret_pct": None}

    start = _to_float(rows[0].get("equity", "0"))
    end = _to_float(rows[-1].get("equity", "0"))
    ret = ((end / start - 1.0) * 100.0) if start > 0 else None
    return {"issues": [], "start": start, "end": end, "ret_pct": ret}


def check_signal_diag(path: str) -> Dict[str, object]:
    if not os.path.exists(path):
        return {"issues": [f"signal diag file not found: {path}"], "gate_counts": {}, "cand_pass": 0, "cand_drop": 0}

    gate_counts: Dict[str, int] = defaultdict(int)
    cand_pass = 0
    cand_drop = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",", 3)
            if len(parts) < 4:
                continue
            status = parts[2]
            detail = parts[3]
            if status == "CAND":
                if "pass=1" in detail:
                    cand_pass += 1
                elif "pass=0" in detail:
                    cand_drop += 1
                if "why=" in detail:
                    why = detail.split("why=", 1)[1].split(" ", 1)[0]
                    gate_counts[why] += 1
    issues: List[str] = []
    if (cand_pass + cand_drop) > 0 and cand_pass == 0:
        issues.append("candidate pass count is zero; check hard gate thresholds")
    return {"issues": issues, "gate_counts": dict(sorted(gate_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]), "cand_pass": cand_pass, "cand_drop": cand_drop}


def main():
    ledger = check_ledger(DEFAULT_LEDGER)
    equity = check_equity(DEFAULT_EQUITY)
    diag = check_signal_diag(DEFAULT_SIGNAL_DIAG)

    print("[INTEGRITY] ledger:", DEFAULT_LEDGER)
    print(f"- buys={ledger['buys']} sells={ledger['sells']} open_symbols={ledger['open_symbols']}")
    print(f"- open_age_sec={ledger.get('open_age_sec',{})} max_open_age_sec={ledger.get('max_open_age_sec',0)}")
    if ledger["issues"]:
        print("- issues:")
        for msg in ledger["issues"]:
            print("  *", msg)
    else:
        print("- issues: none")

    print("[CANDIDATE] signal_diag:", DEFAULT_SIGNAL_DIAG)
    print(f"- cand_pass={diag['cand_pass']} cand_drop={diag['cand_drop']} top_gates={diag['gate_counts']}")
    if diag['issues']:
        print("- issues:")
        for msg in diag['issues']:
            print("  *", msg)

    print("[PERFORMANCE] equity:", DEFAULT_EQUITY)
    print(f"- start={equity['start']} end={equity['end']} ret_pct={equity['ret_pct']}")
    if equity["issues"]:
        print("- issues:")
        for msg in equity["issues"]:
            print("  *", msg)


if __name__ == "__main__":
    main()
