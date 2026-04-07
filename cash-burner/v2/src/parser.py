# src/parser.py
from __future__ import annotations

from typing import Dict, List, Tuple
from schemas import COLUMNS_BY_TRID, COLUMNS_H0STCNT0, COLUMNS_H0STANC0

CNT_LEN = len(COLUMNS_H0STCNT0)
CNT_LEN_STANC0 = len(COLUMNS_H0STANC0)

def extract_raw(line: str) -> str:
    line = line.strip()
    if line.startswith("#"):
        return ""  # BUG-032: 주석 줄 skip
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

    if tr_id in ("H0STCNT0", "H0NXCNT0", "H0STANC0"):
        _block_len = CNT_LEN_STANC0 if tr_id == "H0STANC0" else CNT_LEN
        if len(values) < _block_len:
            return out
        if len(values) % _block_len != 0:
            # BUG-031: 완전한 블록만 파싱 (불완전 후미 데이터 무시), 블록 0개이면 drop
            n_complete = len(values) // _block_len
            if n_complete == 0:
                return out
            for i in range(0, n_complete * _block_len, _block_len):
                block = values[i:i + _block_len]
                row = dict(zip(cols, block))
                row["_values_len"] = str(len(block))
                row["_cols_len"] = str(len(cols))
                out.append((tr_id, row))
            return out
        for i in range(0, len(values), _block_len):
            block = values[i:i+_block_len]
            row = dict(zip(cols, block))
            row["_values_len"] = str(len(block))
            row["_cols_len"] = str(len(cols))
            out.append((tr_id, row))
        return out

    # orderbook
    if len(values) != len(cols):
        # best-effort: slice to cols length so _values_len == _cols_len downstream
        block = values[:len(cols)]
        row = dict(zip(cols, block))
        row["_values_len"] = str(len(cols))
        row["_cols_len"] = str(len(cols))
        out.append((tr_id, row))
        return out
    row = dict(zip(cols, values))
    row["_values_len"] = str(len(values))
    row["_cols_len"] = str(len(cols))
    out.append((tr_id, row))
    return out
