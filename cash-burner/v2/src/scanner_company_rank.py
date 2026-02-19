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


def fetch_rank(market: str) -> List[Dict[str, Any]]:
    """Fetch ranking rows for market code. Tries multiple TR IDs for env differences."""
    d = _today_yyyymmdd()
    tr_ids = [x.strip() for x in os.getenv("RANK_TR_IDS", DEFAULT_TR_IDS).split(",") if x.strip()]

    params = {
        "fid_trgt_exls_cls_code": os.getenv("FID_TRGT_EXLS_CLS_CODE", "0"),
        "fid_cond_mrkt_div_code": market,
        "fid_cond_scr_div_code": os.getenv("FID_COND_SCR_DIV_CODE", "20186"),
        "fid_div_cls_code": os.getenv("FID_DIV_CLS_CODE", "0"),
        "fid_rank_sort_cls_code": os.getenv("FID_RANK_SORT_CLS_CODE", "1"),
        "fid_input_date_1": os.getenv("FID_INPUT_DATE_1", d),
        "fid_input_date_2": os.getenv("FID_INPUT_DATE_2", d),
        "fid_input_iscd": os.getenv("FID_INPUT_ISCD", "0000"),
        "fid_trgt_cls_code": os.getenv("FID_TRGT_CLS_CODE", "0"),
        "fid_aply_rang_vol": os.getenv("FID_APLY_RANG_VOL", "0"),
        "fid_aply_rang_prc_2": os.getenv("FID_APLY_RANG_PRC_2", "0"),
        "fid_aply_rang_prc_1": os.getenv("FID_APLY_RANG_PRC_1", "0"),
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


def build_watchlist() -> List[str]:
    """Build watchlist from ranking API results, with env fallback for ops continuity."""
    want_n = int(os.getenv("WATCH_TOP_N", "30"))
    min_tv = float(os.getenv("WATCH_MIN_TR_VALUE", "300000000"))
    block_rise = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "12.0"))

    markets = [m.strip() for m in os.getenv("RANK_MARKETS", "J").split(",") if m.strip()]
    items: List[Tuple[float, str]] = []

    for m in markets:
        for it in fetch_rank(m):
            sym = (
                it.get("mksc_shrn_iscd")
                or it.get("MKSC_SHRN_ISCD")
                or it.get("stnd_iscd")
                or it.get("pdno")
                or it.get("code")
            )
            if not sym:
                continue
            sym = str(sym).zfill(6)

            try:
                r = float(str(it.get("prdy_ctrt", "0")).replace(",", ""))
            except Exception:
                r = 0.0
            if r >= block_rise:
                continue

            try:
                tv = float(str(it.get("acml_tr_pbmn", "0")).replace(",", ""))
            except Exception:
                tv = 0.0
            if tv > 0 and tv < min_tv:
                continue

            score = tv + (r * 1e7)
            items.append((score, sym))

    items.sort(reverse=True)
    out: List[str] = []
    seen = set()
    for _, sym in items:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= want_n:
            break

    if out:
        return out

    fb = _fallback_symbols()
    return fb[:want_n]
