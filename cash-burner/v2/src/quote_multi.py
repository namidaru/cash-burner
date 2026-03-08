# src/quote_multi.py
from __future__ import annotations

import os
import math
from typing import List, Dict, Any
from kis_http import request

TRID = "FHKST11300006"
PATH = "/uapi/domestic-stock/v1/quotations/intstock-multprice"

# market code default (J=KOSPI, Q=KOSDAQ etc). You can change via env.
DEFAULT_MRKT = os.getenv("FID_COND_MRKT_DIV_CODE_1", "J")

# 실전용 과열 차단(+5% 이상 진입 금지)과도 정합
ENTRY_BLOCK_DAYRISE_PCT = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "5.0"))

# 거래대금(원) 너무 작은 종목은 제외(유동성 쓰레기 필터)
MIN_TRADE_VALUE = float(os.getenv("WATCH_MIN_TR_VALUE", "600000000"))
MIN_VOLUME = float(os.getenv("WATCH_MIN_VOLUME", "30000"))
WATCH_SOFT_HEAT_PCT = float(os.getenv("WATCH_SOFT_HEAT_PCT", "10.5"))
WATCH_HARD_HEAT_PCT = float(os.getenv("WATCH_HARD_HEAT_PCT", "14.0"))

MARKET_CANDIDATES = [m.strip() for m in os.getenv("FID_COND_MRKT_DIV_CODE_MULTI", f"{DEFAULT_MRKT},NX").split(",") if m.strip()]
_SYMBOL_MARKET: Dict[str, str] = {}

def _item_symbol(it: Dict[str, Any]) -> str:
    return str(
        it.get("mksc_shrn_iscd")
        or it.get("MKSC_SHRN_ISCD")
        or it.get("stnd_iscd")
        or it.get("STND_ISCD")
        or it.get("pdno")
        or it.get("PDNO")
        or ""
    ).strip()



def _norm_symbol(sym: str) -> str:
    s = str(sym or "").strip()
    if len(s) == 6 and s.isdigit():
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return s

def chunk(lst: List[str], n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def _get_first(it: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    for k in keys:
        if k in it and it[k] not in (None, "", " "):
            try:
                return float(str(it[k]).replace(",", ""))
            except Exception:
                pass
    return default

def multi_quote(symbols: List[str]) -> List[Dict[str, Any]]:
    out = []
    for batch in chunk(symbols, 30):
        merged: Dict[str, Dict[str, Any]] = {}
        pending = [_norm_symbol(sym) for sym in batch if sym]

        known_by_market: Dict[str, List[str]] = {}
        for sym in pending:
            m = _SYMBOL_MARKET.get(sym)
            if m:
                known_by_market.setdefault(m, []).append(sym)

        for market, syms in known_by_market.items():
            params = {"FID_COND_MRKT_DIV_CODE_1": market}
            for i, sym in enumerate(syms, start=1):
                params[f"FID_INPUT_ISCD_{i}"] = sym
            j = request("GET", PATH, TRID, params=params)
            items = j.get("output", []) or j.get("output1", []) or j.get("output2", [])
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                sym = _norm_symbol(_item_symbol(it))
                if not sym or sym in merged:
                    continue
                merged[sym] = it
                _SYMBOL_MARKET[sym] = market

        pending = [sym for sym in pending if sym not in merged]
        for market in MARKET_CANDIDATES:
            if not pending:
                break
            params = {"FID_COND_MRKT_DIV_CODE_1": market}
            for i, sym in enumerate(pending, start=1):
                params[f"FID_INPUT_ISCD_{i}"] = sym
            j = request("GET", PATH, TRID, params=params)
            items = j.get("output", []) or j.get("output1", []) or j.get("output2", [])
            if not isinstance(items, list):
                continue
            found = set()
            for it in items:
                if not isinstance(it, dict):
                    continue
                sym = _norm_symbol(_item_symbol(it))
                if not sym or sym in merged:
                    continue
                merged[sym] = it
                _SYMBOL_MARKET[sym] = market
                found.add(sym)
            if found:
                pending = [sym for sym in pending if sym not in found]
        for sym in batch:
            ns = _norm_symbol(sym)
            if ns in merged:
                out.append(merged[ns])
    return out



def volume_acceleration(it: Dict[str, Any]) -> float:
    """거래대금 가속도 proxy (당일 누적대금 / 전일 거래량)."""
    tr_value = _get_first(it, [
        "acml_tr_pbmn", "ACML_TR_PBMN", "stck_acml_tr_pbmn", "STCK_ACML_TR_PBMN"
    ], 0.0)
    prev_vol = _get_first(it, [
        "prdy_vol", "PRDY_VOL", "prev_vol", "PRDY_ACML_VOL"
    ], 0.0)
    if prev_vol <= 0:
        return 0.0
    return tr_value / prev_vol

def score_item(it: Dict[str, Any]) -> float:
    """
    실전 수익형 워치리스트 점수:
    - 급등 초입은 감점 위주, 끝물 과열만 하드 제외
    - 거래대금(유동성) + 등락률(모멘텀) + 거래량을 섞어서 점수화
    - 거래대금이 너무 작으면 제외

    NOTE: 응답 키는 계정/버전마다 약간 다를 수 있어서 best-effort로 여러 키 후보를 탐색함.
    """

    # 1) 등락률(전일대비율) - 가장 중요 모멘텀
    r = _get_first(it, [
        "prdy_ctrt", "PRDY_CTRT", "prdy_ctrt_rate", "prdy_ctrt_pct",
        "stck_prdy_ctrt", "STCK_PRDY_CTRT"
    ], 0.0)

    # 워치리스트는 초반 급등 포착이 목적이므로 soft/hard 이원화
    # - hard: 정말 과열 구간은 제외
    # - soft: 초반 급등은 감점만 적용
    if r >= max(ENTRY_BLOCK_DAYRISE_PCT + 3.0, WATCH_HARD_HEAT_PCT):
        return float("-inf")
    heat_penalty = 0.0
    if r > WATCH_SOFT_HEAT_PCT:
        heat_penalty = min(4.0, (r - WATCH_SOFT_HEAT_PCT) * 0.9)

    # 2) 거래대금/거래량 (유동성)
    tr_value = _get_first(it, [
        "acml_tr_pbmn", "ACML_TR_PBMN", "stck_acml_tr_pbmn", "STCK_ACML_TR_PBMN",
        "tr_pbmn", "TR_PBMN"
    ], 0.0)

    vol = _get_first(it, [
        "acml_vol", "ACML_VOL", "stck_acml_vol", "STCK_ACML_VOL",
        "vol", "VOL"
    ], 0.0)

    # 체결량 하한 필터(너무 얇은 종목 제거)
    if vol > 0 and vol < MIN_VOLUME:
        return float("-inf")

    # 유동성 하한 필터(거래대금 너무 적은 후보는 워치리스트에서 제외)
    if tr_value > 0 and tr_value < MIN_TRADE_VALUE:
        return float("-inf")

    strength = _get_first(it, [
        "tday_rltv", "exec_str", "trade_strength", "cntrg", "cttr", "power"
    ], 0.0)

    # 3) 스프레드/호가 품질(있으면만 사용)
    ask1 = _get_first(it, ["askp1", "ASKP1", "ask1", "ASK1"], 0.0)
    bid1 = _get_first(it, ["bidp1", "BIDP1", "bid1", "BID1"], 0.0)
    spread_penalty = 0.0
    if ask1 > 0 and bid1 > 0 and ask1 >= bid1:
        mid = (ask1 + bid1) / 2.0
        spr_pct = ((ask1 - bid1) / mid) * 100.0 if mid > 0 else 999.0
        # PROMPT 5: 임계치 0.30→0.20%, 감점 강화 최대 4점
        if spr_pct > 0.20:
            spread_penalty = min(4.0, (spr_pct - 0.20) * 3.0)

    # 4) 점수 조합
    # PROMPT 5-1: 거래량 가속도 log 스케일 (폭발적 가속 포착)
    accel_raw = volume_acceleration(it)
    accel_score = math.log1p(max(0.0, accel_raw)) * 6.0

    # PROMPT 5-2: 체결강도 — 등락률 높으면 신뢰도 낮으므로 가중치 축소
    strength_score = max(0.0, (strength - 100.0) / 20.0)
    if r > 2.0:
        strength_score *= 0.7

    # PROMPT 5-3: 유동성 상대화 (MIN_TRADE_VALUE 대비 비율)
    rel_liquidity = tr_value / max(1.0, MIN_TRADE_VALUE)
    liquidity_score = math.log1p(rel_liquidity) * 1.5

    score = (
        (max(0.0, r) * 0.7)
        + accel_score
        + (strength_score * 1.2)
        + liquidity_score
        - spread_penalty
        - heat_penalty
    )

    return score
