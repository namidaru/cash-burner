# src/scanner_conditions.py
from __future__ import annotations

import os, time
from typing import List, Dict, Any
from kis_http import request, USER_ID

TRID_LIST = "HHKST03900300"
TRID_RUN  = "HHKST03900400"

PATH_LIST = "/uapi/domestic-stock/v1/quotations/psearch-title"
PATH_RUN  = "/uapi/domestic-stock/v1/quotations/psearch-result"

def list_conditions() -> List[Dict[str, Any]]:
    # docs show GET; user_id may be required in some setups
    params = {}
    if USER_ID:
        params["user_id"] = USER_ID
    j = request("GET", PATH_LIST, TRID_LIST, params=params)
    return j.get("output2", []) or []

def run_condition(seq: str) -> List[str]:
    params = {"seq": str(seq)}
    if USER_ID:
        params["user_id"] = USER_ID
    j = request("GET", PATH_RUN, TRID_RUN, params=params)
    # BUG-041: output2/output1 통합 순회, 중복 루프 제거
    out = []
    sources = []
    if j.get("output2"):
        sources.append(j["output2"])
    if j.get("output1"):
        sources.append(j["output1"])
    for src in sources:
        for item in (src if isinstance(src, list) else [src]):
            if not isinstance(item, dict):
                continue
            matched = False
            for k in ("code", "stnd_iscd", "pdno", "mkSC_SHRN_ISCD", "MKSC_SHRN_ISCD"):
                if k in item and item[k]:
                    out.append(str(item[k]).zfill(6))
                    matched = True
                    break
            if not matched:
                for k, v in item.items():
                    if "code" in k.lower() and v:
                        out.append(str(v).zfill(6))
    return sorted(set(out))

def scan(seqs: List[str]) -> List[str]:
    symbols=[]
    for s in seqs:
        try:
            symbols.extend(run_condition(s))
        except Exception:
            pass
    return sorted(set(symbols))
