# src/quote_basic.py
from __future__ import annotations

import os, json, time
from typing import Dict, Any
from kis_http import request

TRID = "CTPF1002R"
PATH = "/uapi/domestic-stock/v1/quotations/search-stock-info"

CACHE_PATH = os.getenv("PREVCLOSE_CACHE", os.path.join("data", "prev_close.json"))

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def get_basic(symbol: str) -> Dict[str, Any]:
    params = {"PDNO": symbol, "PRDT_TYPE_CD": os.getenv("PRDT_TYPE_CD","300")}
    return request("GET", PATH, TRID, params=params)

def extract_prev_close(j: Dict[str,Any]) -> float:
    out = j.get("output", {}) or j.get("output1", {}) or {}
    for k in ("prdy_clpr","stck_prdy_clpr","PRDY_CLPR"):
        if k in out and out[k]:
            try: return float(out[k])
            except: pass
    return 0.0

def load_cache() -> Dict[str,float]:
    try:
        with open(CACHE_PATH,"r",encoding="utf-8") as f:
            raw = json.load(f)
        today = time.strftime("%Y%m%d")
        out: Dict[str, float] = {}
        for sym, v in raw.items():
            if isinstance(v, dict):
                if v.get("date") == today:
                    out[sym] = float(v.get("price", 0))
        return out
    except Exception:
        return {}

def save_cache(cache: Dict[str,float]):
    _ensure_dir(CACHE_PATH)
    today = time.strftime("%Y%m%d")
    payload = {sym: {"price": price, "date": today} for sym, price in cache.items() if price > 0}
    with open(CACHE_PATH,"w",encoding="utf-8") as f:
        json.dump(payload,f,ensure_ascii=False)

def ensure_prev_close(symbols):
    cache = load_cache()
    changed=False
    for sym in symbols:
        if sym in cache and cache[sym] > 0:
            continue
        try:
            j = get_basic(sym)
            pc = extract_prev_close(j)
            if pc > 0:
                cache[sym]=pc
                changed=True
        except Exception:
            pass
    if changed:
        save_cache(cache)
    return cache
