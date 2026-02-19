from __future__ import annotations

import os
import datetime as _dt
from typing import Any, Dict, List, Tuple

from kis_http import request

PATH = "/uapi/domestic-stock/v1/ranking/traded-by-company"
DEFAULT_TR_IDS = "FHPST01860000,VHPST01860000"
_LAST_BUILD_META: str = ""


def _today_yyyymmdd() -> str:
    return _dt.datetime.now().strftime("%Y%m%d")


def _yyyymmdd_delta(days: int) -> str:
    return (_dt.datetime.now() + _dt.timedelta(days=days)).strftime("%Y%m%d")


def _normalize_rank_rows(j: Dict[str, Any]) -> List[Dict[str, Any]]:
    for k in ("output", "output1", "output2"):
        v = j.get(k)
        if isinstance(v, list) and v:
            return v

    # 일부 게이트웨이는 output 내부에 다시 리스트를 담아 주는 케이스가 있음
    for k in ("output", "output1", "output2"):
        v = j.get(k)
        if isinstance(v, dict):
            for _, iv in v.items():
                if isinstance(iv, list) and iv:
                    return iv

    for _, v in j.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def _resolve_date1_candidates() -> List[str]:
    raw = os.getenv("RANK_DATE1_CANDIDATES", "AUTO").strip()
    if not raw or raw.upper() == "AUTO":
        return [_today_yyyymmdd(), _yyyymmdd_delta(-1)]
    out = [x.strip() for x in raw.split(",") if x.strip()]
    return out or [_today_yyyymmdd()]


def fetch_rank(market: str, rank_sort: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """당사매매순위(v1_국내주식-104) 조회. 복수 조합을 시도해 빈응답 확률을 낮춤."""
    tr_ids = [x.strip() for x in os.getenv("RANK_TR_IDS", DEFAULT_TR_IDS).split(",") if x.strip()]
    scr_codes = [x.strip() for x in os.getenv("RANK_SCR_CODES", os.getenv("FID_COND_SCR_DIV_CODE", "20186")).split(",") if x.strip()]
    d2 = os.getenv("FID_INPUT_DATE_2", _today_yyyymmdd())
    d1_candidates = _resolve_date1_candidates()

    all_rows: List[Dict[str, Any]] = []
    debug_meta: List[str] = []

    for tr_id in tr_ids:
        for scr in scr_codes:
            for d1 in d1_candidates:
                params = {
                    "fid_trgt_exls_cls_code": os.getenv("FID_TRGT_EXLS_CLS_CODE", "0"),
                    "fid_cond_mrkt_div_code": market,
                    "fid_cond_scr_div_code": scr,
                    "fid_div_cls_code": os.getenv("FID_DIV_CLS_CODE", "0"),
                    "fid_rank_sort_cls_code": str(rank_sort),
                    "fid_input_date_1": d1,
                    "fid_input_date_2": d2,
                    "fid_input_iscd": os.getenv("FID_INPUT_ISCD", "0000"),
                    "fid_trgt_cls_code": os.getenv("FID_TRGT_CLS_CODE", "0"),
                    "fid_aply_rang_vol": os.getenv("FID_APLY_RANG_VOL", "0"),
                    "fid_aply_rang_prc_2": os.getenv("FID_APLY_RANG_PRC_2", ""),
                    "fid_aply_rang_prc_1": os.getenv("FID_APLY_RANG_PRC_1", ""),
                }

                try:
                    j = request("GET", PATH, tr_id, params=params)
                    rows = _normalize_rank_rows(j)
                    if rows:
                        all_rows.extend(rows)
                        debug_meta.append(f"ok m={market} s={rank_sort} tr={tr_id} scr={scr} d1={d1} rows={len(rows)}")
                    else:
                        debug_meta.append(
                            f"empty m={market} s={rank_sort} tr={tr_id} scr={scr} d1={d1} rt_cd={j.get('rt_cd','?')} msg1={j.get('msg1','')[:40]}"
                        )
                except Exception as e:
                    debug_meta.append(f"err m={market} s={rank_sort} tr={tr_id} scr={scr} d1={d1} ex={type(e).__name__}")

    return all_rows, debug_meta


def _fallback_symbols() -> List[str]:
    raw = os.getenv("FALLBACK_SYMBOLS", "")
    if not raw:
        return []
    out = []
    for tok in raw.split(","):
        s = tok.strip()
        if s:
            out.append(s.zfill(6))
    return out


def _parse_sym(item: Dict[str, Any]) -> str:
    sym = (
        item.get("mksc_shrn_iscd")
        or item.get("MKSC_SHRN_ISCD")
        or item.get("stnd_iscd")
        or item.get("pdno")
        or item.get("code")
    )
    if not sym:
        return ""
    return str(sym).zfill(6)


def _parse_float(item: Dict[str, Any], key: str, d: float = 0.0) -> float:
    try:
        return float(str(item.get(key, d)).replace(",", ""))
    except Exception:
        return d


def get_last_build_meta() -> str:
    return _LAST_BUILD_META


def build_watchlist() -> List[str]:
    """API 결과로 최대 N개 채움.
    1) 필터 통과 종목 우선
    2) 부족하면 같은 API 결과에서 필터 탈락분으로 보충
    3) 그래도 비면 FALLBACK_SYMBOLS 사용
    """
    global _LAST_BUILD_META

    want_n = int(os.getenv("WATCH_TOP_N", "30"))
    min_tv = float(os.getenv("WATCH_MIN_TR_VALUE", "300000000"))
    block_rise = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "12.0"))

    markets = [m.strip() for m in os.getenv("RANK_MARKETS", "J,NX").split(",") if m.strip()]
    sort_codes = [s.strip() for s in os.getenv("RANK_SORT_CODES", "1,0").split(",") if s.strip()]

    preferred: List[Tuple[float, str]] = []
    backup: List[Tuple[float, str]] = []
    meta: List[str] = []

    for m in markets:
        for sc in sort_codes:
            rows, dbg = fetch_rank(m, sc)
            meta.extend(dbg)
            for it in rows:
                sym = _parse_sym(it)
                if not sym:
                    continue

                r = _parse_float(it, "prdy_ctrt", 0.0)
                tv = _parse_float(it, "acml_tr_pbmn", 0.0)
                score = tv + (r * 1e7)

                if r < block_rise and (tv <= 0 or tv >= min_tv):
                    preferred.append((score, sym))
                else:
                    backup.append((score, sym))

    out: List[str] = []
    seen = set()

    for src in (sorted(preferred, reverse=True), sorted(backup, reverse=True)):
        for _, sym in src:
            if sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
            if len(out) >= want_n:
                _LAST_BUILD_META = " | ".join(meta[-24:])
                return out

    if out:
        _LAST_BUILD_META = " | ".join(meta[-24:])
        return out

    fb = _fallback_symbols()
    _LAST_BUILD_META = " | ".join(meta[-24:])
    return fb[:want_n]
