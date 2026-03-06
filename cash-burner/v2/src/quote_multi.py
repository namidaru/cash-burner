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
WATCH_SOFT_HEAT_PCT = float(os.getenv("WATCH_SOFT_HEAT_PCT", "2.5"))
WATCH_HARD_HEAT_PCT = float(os.getenv("WATCH_HARD_HEAT_PCT", "9.5"))

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
        params = {"FID_COND_MRKT_DIV_CODE_1": DEFAULT_MRKT}
        for i, sym in enumerate(batch, start=1):
            params[f"FID_INPUT_ISCD_{i}"] = sym
        j = request("GET", PATH, TRID, params=params)
        items = j.get("output", []) or j.get("output1", []) or j.get("output2", [])
        if isinstance(items, list):
            out.extend(items)
    return out



def volume_acceleration(it: Dict[str, Any]) -> float:
    """거래대금 가속도 proxy (당일 누적대금 / 전일 거래량)."""
    tr_value = _get_first(it, [
        "acml_tr_pbmn", "ACML_TR_PBMN", "stck_acml_tr_pbmn", "STCK_ACML_TR_PBMN"
    ], 0.0)
    prev_vol = _get_first(it, [
        "prdy_vol", "PRDY_VOL", "prev_vol", "PRDY_ACML_VOL"
    ], 1.0)
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

    # 3) 스프레드/호가 품질(있으면 가산/감점)
    # 멀티시세에 ask/bid가 안 올 수도 있음 → 있으면만 사용
    ask1 = _get_first(it, ["askp1", "ASKP1", "ask1", "ASK1"], 0.0)
    bid1 = _get_first(it, ["bidp1", "BIDP1", "bid1", "BID1"], 0.0)
    spread_penalty = 0.0
    if ask1 > 0 and bid1 > 0 and ask1 >= bid1:
        mid = (ask1 + bid1) / 2.0
        spr_pct = ((ask1 - bid1) / mid) * 100.0 if mid > 0 else 999.0
        # 스프레드 큰 애는 감점(미끄러짐 방지)
        # 0.30% 넘으면 꽤 불리하다고 보고 감점
        if spr_pct > 0.30:
            spread_penalty = min(2.0, (spr_pct - 0.30) * 2.0)  # 최대 2점 감점

    # 4) 점수 조합 (급등 초입 탐지형)
    # - volume_accel: 거래대금 가속도 중심
    # - strength_score: 체결강도 100 기준 정규화
    # - liquidity_score: 절대 유동성 보정
    accel = min(volume_acceleration(it), 3.0)
    strength_score = max(0.0, (strength - 100.0) / 20.0)
    liquidity_score = math.log1p(max(tr_value, 0.0))

    score = (
        (max(0.0, r) * 0.8)
        + (accel * 3.0)
        + (strength_score * 1.8)
        + (liquidity_score * 1.0)
        - spread_penalty
        - heat_penalty
    )

    return score
