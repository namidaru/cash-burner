from __future__ import annotations

import csv
import os
from collections import defaultdict
from typing import Dict, List


DEFAULT_LEDGER = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
DEFAULT_EQUITY = os.getenv("EQUITY_FILE", os.path.join("data", "equity.csv"))


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
    buys = sells = 0

    if not os.path.exists(path):
        return {"issues": [f"ledger file not found: {path}"], "buys": 0, "sells": 0, "open_symbols": {}}

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
            else:
                sells += 1
                by_symbol[sym] -= qty
                if by_symbol[sym] < 0:
                    issues.append(f"line {i}: sold before buy ({sym})")

    open_symbols = {s: q for s, q in by_symbol.items() if q > 0}
    return {"issues": issues, "buys": buys, "sells": sells, "open_symbols": open_symbols}


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


def main():
    ledger = check_ledger(DEFAULT_LEDGER)
    equity = check_equity(DEFAULT_EQUITY)

    print("[INTEGRITY] ledger:", DEFAULT_LEDGER)
    print(f"- buys={ledger['buys']} sells={ledger['sells']} open_symbols={ledger['open_symbols']}")
    if ledger["issues"]:
        print("- issues:")
        for msg in ledger["issues"]:
            print("  *", msg)
    else:
        print("- issues: none")

    print("[PERFORMANCE] equity:", DEFAULT_EQUITY)
    print(f"- start={equity['start']} end={equity['end']} ret_pct={equity['ret_pct']}")
    if equity["issues"]:
        print("- issues:")
        for msg in equity["issues"]:
            print("  *", msg)


if __name__ == "__main__":
    main()
