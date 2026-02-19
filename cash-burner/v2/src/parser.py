# src/parser.py
from __future__ import annotations

from typing import Dict, List, Tuple
from schemas import COLUMNS_BY_TRID, COLUMNS_H0STCNT0

CNT_LEN = len(COLUMNS_H0STCNT0)

def extract_raw(line: str) -> str:
    line = line.strip()
    if "\t" in line:
        return line.split("\t", 1)[1].strip()
    return line

def parse_ws_raw_multi(raw: str) -> List[Tuple[str, Dict[str, str]]]:
    """Return list of (tr_id, row). Supports packed trade records (46-field blocks)."""
    out: List[Tuple[str, Dict[str, str]]] = []
    if not raw or raw.startswith("{"):
        return out
    if not (raw.startswith("0|") or raw.startswith("1|")):
        return out

    parts = raw.split("|", 3)
    if len(parts) < 4:
        return out

    tr_id = parts[1]
    data = parts[3].strip()
    values = data.split("^")

    cols = COLUMNS_BY_TRID.get(tr_id)
    if not cols:
        return out

    if tr_id in ("H0STCNT0", "H0NXCNT0"):
        if len(values) < CNT_LEN:
            return out
        if len(values) % CNT_LEN != 0:
            # best-effort: first block only
            block = values[:CNT_LEN]
            row = dict(zip(cols, block))
            row["_values_len"] = str(len(block))
            row["_cols_len"] = str(len(cols))
            out.append((tr_id, row))
            return out
        for i in range(0, len(values), CNT_LEN):
            block = values[i:i+CNT_LEN]
            row = dict(zip(cols, block))
            row["_values_len"] = str(len(block))
            row["_cols_len"] = str(len(cols))
            out.append((tr_id, row))
        return out

    # orderbook
    row = dict(zip(cols, values))
    row["_values_len"] = str(len(values))
    row["_cols_len"] = str(len(cols))
    out.append((tr_id, row))
    return out
