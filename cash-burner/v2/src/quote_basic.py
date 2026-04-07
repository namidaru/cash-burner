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
    for k in ("prdy_clpr", "stck_prdy_clpr", "PRDY_CLPR",
              "bfdv_clpr", "BFDV_CLPR",
              "thdt_clpr", "THDT_CLPR",
              "sbst_pric", "SBST_PRIC"):
        if k in out and out[k]:
            try: return float(out[k])
            except: pass
    return 0.0

def load_cache() -> Dict[str,float]:
    try:
        with open(CACHE_PATH,"r",encoding="utf-8") as f:
            raw = json.load(f)
        out: Dict[str, float] = {}
        for sym, v in raw.items():
            if isinstance(v, dict):
                p = float(v.get("price", 0))
                if p > 0:
                    out[sym] = p
            elif isinstance(v, (int, float)) and v > 0:
                out[sym] = float(v)
        return out
    except Exception:
        return {}

def save_cache(cache: Dict[str,float]):
    _ensure_dir(CACHE_PATH)
    today = time.strftime("%Y%m%d")
    payload = {sym: {"price": price, "date": today} for sym, price in cache.items() if price > 0}
    with open(CACHE_PATH,"w",encoding="utf-8") as f:
        json.dump(payload,f,ensure_ascii=False)

# BUG-046: 반복 0-가격 종목 재시도 차단 (세션 내 메모리 캐시)
_zero_price_skip: set[str] = set()

# 외국인/기관 순매수 캐시 (장 전 1회 조회, {sym: {"foreign": N, "inst": N}})
_investor_cache: Dict[str, Dict[str, float]] = {}

def fetch_investor_trend(sym: str) -> Dict[str, float]:
    """종목별 전일 외국인/기관 순매수 금액 조회 (FHKST01010900)."""
    if sym in _investor_cache:
        return _investor_cache[sym]
    try:
        j = request("GET", "/uapi/domestic-stock/v1/quotations/inquire-investor",
                     "FHKST01010900", params={
                         "FID_COND_MRKT_DIV_CODE": "J",
                         "FID_INPUT_ISCD": sym,
                     })
        items = j.get("output", [])
        if items and isinstance(items, list) and len(items) >= 1:
            row = items[0]  # 가장 최근 거래일
            foreign = float(row.get("frgn_ntby_qty", 0) or 0)
            inst = float(row.get("orgn_ntby_qty", 0) or 0)
            result = {"foreign": foreign, "inst": inst}
        else:
            result = {"foreign": 0.0, "inst": 0.0}
    except Exception:
        result = {"foreign": 0.0, "inst": 0.0}
    _investor_cache[sym] = result
    return result


def fetch_investor_bulk(symbols) -> Dict[str, Dict[str, float]]:
    """여러 종목 외국인/기관 순매수 일괄 조회. API 과호출 방지로 0.1초 간격."""
    for sym in symbols:
        if sym not in _investor_cache:
            fetch_investor_trend(sym)
            time.sleep(0.1)
    return {sym: _investor_cache.get(sym, {"foreign": 0.0, "inst": 0.0}) for sym in symbols}


def ensure_prev_close(symbols):
    cache = load_cache()
    changed = False
    for sym in symbols:
        if sym in _zero_price_skip:
            continue
        if sym in cache and cache[sym] > 0:
            continue
        try:
            j = get_basic(sym)
            pc = extract_prev_close(j)
            if pc > 0:
                cache[sym] = pc
                changed = True
            else:
                rt = j.get("rt_cd", "?")
                msg = j.get("msg1", "").strip()
                print(f"[PREV_CLOSE] {sym} API 응답 0원 — rt_cd={rt} msg={msg}", flush=True)
                if str(rt) != "0":
                    _zero_price_skip.add(sym)  # rt_cd 비정상이면 재시도 차단
        except Exception as e:
            print(f"[PREV_CLOSE] {sym} API 실패: {type(e).__name__}: {e}", flush=True)
            # _zero_price_skip에 넣지 않음 — 다음 호출에서 재시도
    if changed:
        save_cache(cache)
    return cache
