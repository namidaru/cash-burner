from __future__ import annotations

import os
import datetime as _dt
from typing import Any, Dict, List, Tuple

from kis_http import request

PATH = "/uapi/domestic-stock/v1/ranking/traded-by-company"
DEFAULT_TR_IDS = "FHPST01860000,VHPST01860000"


def _today_yyyymmdd() -> str:
    return _dt.datetime.now().strftime("%Y%m%d")


def _normalize_rank_rows(j: Dict[str, Any]) -> List[Dict[str, Any]]:
    for k in ("output", "output1", "output2"):
        v = j.get(k)
        if isinstance(v, list) and v:
            return v
    return []


def fetch_rank(market: str, rank_sort: str) -> List[Dict[str, Any]]:
    """당사매매순위(v1_국내주식-104) 조회. market/sort 조합별로 호출."""
    d = _today_yyyymmdd()
    tr_ids = [x.strip() for x in os.getenv("RANK_TR_IDS", DEFAULT_TR_IDS).split(",") if x.strip()]

    params = {
        "fid_trgt_exls_cls_code": os.getenv("FID_TRGT_EXLS_CLS_CODE", "0"),
        "fid_cond_mrkt_div_code": market,
        "fid_cond_scr_div_code": os.getenv("FID_COND_SCR_DIV_CODE", "20186"),
        "fid_div_cls_code": os.getenv("FID_DIV_CLS_CODE", "0"),
        "fid_rank_sort_cls_code": str(rank_sort),
        "fid_input_date_1": os.getenv("FID_INPUT_DATE_1", d),
        "fid_input_date_2": os.getenv("FID_INPUT_DATE_2", d),
        "fid_input_iscd": os.getenv("FID_INPUT_ISCD", "0000"),
        "fid_trgt_cls_code": os.getenv("FID_TRGT_CLS_CODE", "0"),
        "fid_aply_rang_vol": os.getenv("FID_APLY_RANG_VOL", "0"),
        "fid_aply_rang_prc_2": os.getenv("FID_APLY_RANG_PRC_2", ""),
        "fid_aply_rang_prc_1": os.getenv("FID_APLY_RANG_PRC_1", ""),
    }

    for tr_id in tr_ids:
        try:
            j = request("GET", PATH, tr_id, params=params)
            rows = _normalize_rank_rows(j)
            if rows:
                return rows
        except Exception:
            continue
    return []


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


def build_watchlist() -> List[str]:
    """API 결과로 최대 30개 채움.
    1) 필터 통과 종목 우선
    2) 부족하면 같은 API 결과에서 필터 탈락분으로 보충
    3) 그래도 비면 FALLBACK_SYMBOLS 사용
    """
    want_n = int(os.getenv("WATCH_TOP_N", "30"))
    min_tv = float(os.getenv("WATCH_MIN_TR_VALUE", "300000000"))
    block_rise = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "12.0"))

    markets = [m.strip() for m in os.getenv("RANK_MARKETS", "J,NX").split(",") if m.strip()]
    sort_codes = [s.strip() for s in os.getenv("RANK_SORT_CODES", "1,0").split(",") if s.strip()]

    preferred: List[Tuple[float, str]] = []
    backup: List[Tuple[float, str]] = []

    for m in markets:
        for sc in sort_codes:
            rows = fetch_rank(m, sc)
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
                return out

    if out:
        return out

    fb = _fallback_symbols()
    return fb[:want_n]
