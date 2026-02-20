from __future__ import annotations

import os
import re
import datetime as _dt
from typing import Any, Dict, List, Tuple

from kis_http import request
from scanner_conditions import scan as scan_conditions
from quote_multi import multi_quote, score_item

PATH = "/uapi/domestic-stock/v1/ranking/traded-by-company"
STRENGTH_PATH = "/uapi/domestic-stock/v1/ranking/volume-power"
VOLUME_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
DEFAULT_TR_IDS = "FHPST01860000,VHPST01860000"
DEFAULT_STRENGTH_TR_IDS = "FHPST01710000,VHPST01710000"
DEFAULT_VOLUME_TR_IDS = "FHPST01710000,VHPST01710000"
_LAST_BUILD_META: str = ""
_LAST_SOURCE_MAP: Dict[str, str] = {}


def _time_hhmm() -> int:
    return int(_dt.datetime.now().strftime("%H%M"))


def _selection_profile() -> Tuple[float, float]:
    """장구간별 추천치: (min_vol_percentile, max_spread_pct)."""
    hhmm = _time_hhmm()
    if 900 <= hhmm < 920:   # 장초
        return 0.95, 0.50
    if 1030 <= hhmm < 1330:  # 장중
        return 0.90, 0.60
    if 1400 <= hhmm <= 1520:  # 장후반
        return 0.85, 0.80
    return 0.90, 0.60


def _today_yyyymmdd() -> str:
    return _dt.datetime.now().strftime("%Y%m%d")


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
    raw = os.getenv("RANK_DATE1_CANDIDATES", "TODAY").strip()
    if not raw or raw.upper() in ("AUTO", "TODAY"):
        return [_today_yyyymmdd()]
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


def fetch_strength_rank(market: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """체결강도 상위 조회. 실패 시 빈 목록을 반환하고 디버그 메타를 남긴다."""
    path = os.getenv("STRENGTH_PATH", STRENGTH_PATH).strip() or STRENGTH_PATH
    tr_ids = [x.strip() for x in os.getenv("STRENGTH_TR_IDS", DEFAULT_STRENGTH_TR_IDS).split(",") if x.strip()]
    scr_codes = [x.strip() for x in os.getenv("STRENGTH_SCR_CODES", os.getenv("RANK_SCR_CODES", "20170")).split(",") if x.strip()]
    limit_n = int(os.getenv("STRENGTH_TOPK", "120"))

    all_rows: List[Dict[str, Any]] = []
    debug_meta: List[str] = []

    for tr_id in tr_ids:
        for scr in scr_codes:
            params = {
                "fid_cond_mrkt_div_code": market,
                "fid_cond_scr_div_code": scr,
                "fid_input_iscd": os.getenv("STRENGTH_INPUT_ISCD", os.getenv("FID_INPUT_ISCD", "0000")),
                "fid_trgt_cls_code": os.getenv("STRENGTH_TRGT_CLS_CODE", os.getenv("FID_TRGT_CLS_CODE", "0")),
                "fid_trgt_exls_cls_code": os.getenv("STRENGTH_TRGT_EXLS_CLS_CODE", os.getenv("FID_TRGT_EXLS_CLS_CODE", "0")),
            }

            try:
                j = request("GET", path, tr_id, params=params)
                rows = _normalize_rank_rows(j)
                if rows:
                    all_rows.extend(rows)
                    debug_meta.append(f"s-ok m={market} tr={tr_id} scr={scr} rows={len(rows)}")
                else:
                    debug_meta.append(
                        f"s-empty m={market} tr={tr_id} scr={scr} rt_cd={j.get('rt_cd','?')} msg1={j.get('msg1','')[:40]}"
                    )
            except Exception as e:
                debug_meta.append(f"s-err m={market} tr={tr_id} scr={scr} ex={type(e).__name__}")

            if len(all_rows) >= limit_n:
                return all_rows[:limit_n], debug_meta

    return all_rows[:limit_n], debug_meta


def fetch_volume_rank(market: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    tr_ids = [x.strip() for x in os.getenv("VOLUME_RANK_TR_IDS", DEFAULT_VOLUME_TR_IDS).split(",") if x.strip()]
    limit_n = int(os.getenv("VOLUME_RANK_TOPK", "120"))
    all_rows: List[Dict[str, Any]] = []
    debug_meta: List[str] = []

    for tr_id in tr_ids:
        params = {
            "fid_cond_mrkt_div_code": market,
            "fid_cond_scr_div_code": os.getenv("VOLUME_RANK_SCR", "20171"),
            "fid_input_iscd": os.getenv("VOLUME_RANK_INPUT_ISCD", "0000"),
            "fid_div_cls_code": os.getenv("VOLUME_RANK_DIV", "0"),
            "fid_blng_cls_code": os.getenv("VOLUME_RANK_BLNG", "0"),
            "fid_trgt_cls_code": os.getenv("VOLUME_RANK_TRGT", "111111111"),
            "fid_trgt_exls_cls_code": os.getenv("VOLUME_RANK_EXLS", "000000"),
            "fid_input_price_1": os.getenv("VOLUME_RANK_P1", ""),
            "fid_input_price_2": os.getenv("VOLUME_RANK_P2", ""),
            "fid_vol_cnt": os.getenv("VOLUME_RANK_VOL_CNT", ""),
            "fid_input_date_1": os.getenv("VOLUME_RANK_DATE1", ""),
        }
        try:
            j = request("GET", VOLUME_RANK_PATH, tr_id, params=params)
            rows = _normalize_rank_rows(j)
            if rows:
                all_rows.extend(rows)
                debug_meta.append(f"v-ok m={market} tr={tr_id} rows={len(rows)}")
            else:
                debug_meta.append(f"v-empty m={market} tr={tr_id} rt_cd={j.get('rt_cd','?')} msg1={j.get('msg1','')[:40]}")
        except Exception as e:
            debug_meta.append(f"v-err m={market} tr={tr_id} ex={type(e).__name__}")

        if len(all_rows) >= limit_n:
            return all_rows[:limit_n], debug_meta

    return all_rows[:limit_n], debug_meta


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
    raw = (
        item.get("mksc_shrn_iscd")
        or item.get("MKSC_SHRN_ISCD")
        or item.get("stnd_iscd")
        or item.get("pdno")
        or item.get("code")
    )
    if not raw:
        return ""

    s = str(raw).strip()

    # 우선 순수 숫자 6자리 심볼 우선
    if s.isdigit() and len(s) == 6:
        return s

    # 예: Q530107 -> 530107 처럼 알파벳 접두어가 붙는 케이스 보정
    m = re.search(r"(\d{6})", s)
    if m:
        return m.group(1)

    return ""


def _parse_float(item: Dict[str, Any], key: str, d: float = 0.0) -> float:
    try:
        return float(str(item.get(key, d)).replace(",", ""))
    except Exception:
        return d


def _parse_price(item: Dict[str, Any]) -> float:
    # API 변형 대응: 현재가 키가 다를 수 있어 다중 후보를 순회
    for k in ("stck_prpr", "prpr", "stck_clpr", "close", "price", "cur_prc"):
        v = _parse_float(item, k, -1.0)
        if v > 0:
            return v
    return 0.0


def _parse_strength(item: Dict[str, Any]) -> float:
    for k in ("tday_rltv", "exec_str", "trade_strength", "cntrg", "cttr", "power"):
        v = _parse_float(item, k, -1.0)
        if v >= 0:
            return v
    return 0.0


def _parse_spread_pct(item: Dict[str, Any], default_spread: float) -> float:
    ask1 = _parse_float(item, "askp1", 0.0)
    if ask1 <= 0:
        ask1 = _parse_float(item, "ASKP1", 0.0)
    bid1 = _parse_float(item, "bidp1", 0.0)
    if bid1 <= 0:
        bid1 = _parse_float(item, "BIDP1", 0.0)
    if ask1 > 0 and bid1 > 0 and ask1 >= bid1:
        mid = (ask1 + bid1) / 2.0
        if mid > 0:
            return ((ask1 - bid1) / mid) * 100.0
    return default_spread


def _passes_quality(item: Dict[str, Any], min_price: float, max_price: float, min_chg: float, max_chg: float, min_strength: float, max_strength: float) -> bool:
    px = _parse_price(item)
    if min_price > 0 and px > 0 and px <= min_price:
        return False
    if max_price > 0 and px > max_price:
        return False

    chg = _parse_float(item, "prdy_ctrt", 0.0)
    if chg < min_chg or chg > max_chg:
        return False

    st = _parse_strength(item)
    if st > 0 and (st < min_strength or st > max_strength):
        return False

    return True


def _supplement_from_volume_rank(
    need_n: int,
    seen: set[str],
    min_price: float,
    min_tv: float,
    block_rise: float,
    meta: List[str],
    max_price: float,
    min_chg: float,
    max_chg: float,
    min_strength: float,
    max_strength: float,
) -> List[str]:
    if need_n <= 0:
        return []
    if os.getenv("WATCH_FILL_FROM_VOLUME_RANK", "1") != "1":
        return []

    markets = [m.strip() for m in os.getenv("VOLUME_RANK_MARKETS", os.getenv("RANK_MARKETS", "J,NX")).split(",") if m.strip()]
    rows: List[Dict[str, Any]] = []
    for m in markets:
        part, dbg = fetch_volume_rank(m)
        meta.extend(dbg)
        rows.extend(part)

    if not rows:
        meta.append("volume_rank empty")
        return []

    min_vol_pct, max_spread_pct = _selection_profile()

    candidates: List[Tuple[str, Dict[str, Any], float, float, float]] = []  # sym, raw, roc, tv, spread_pct
    for it in rows:
        sym = _parse_sym(it)
        if not sym or sym in seen:
            continue
        if not _passes_quality(it, min_price, max_price, min_chg, max_chg, min_strength, max_strength):
            continue
        r = _parse_float(it, "prdy_ctrt", 0.0)
        tv = _parse_float(it, "acml_tr_pbmn", 0.0)
        spread_pct = _parse_spread_pct(it, max_spread_pct * 0.8)
        if r >= block_rise:
            continue
        if tv > 0 and tv < min_tv:
            continue

        candidates.append((sym, it, r, tv, spread_pct))

    if not candidates:
        meta.append(f"volume_rank fill=0/{need_n} pool={len(rows)}")
        return []

    rocs = sorted([x[2] for x in candidates])
    tvs = sorted([x[3] for x in candidates])
    sp_is = sorted([(1.0 / max(x[4], 0.001)) for x in candidates])

    def _pct(arr: List[float], v: float) -> float:
        if not arr:
            return 0.0
        lo = 0
        hi = len(arr)
        while lo < hi:
            md = (lo + hi) // 2
            if arr[md] <= v:
                lo = md + 1
            else:
                hi = md
        return max(0.0, min(1.0, lo / len(arr)))

    scored: List[Tuple[float, str]] = []
    for sym, _, roc, tv, spread_pct in candidates:
        roc_pct = _pct(rocs, roc)
        tv_pct = _pct(tvs, tv)
        spread_score_pct = _pct(sp_is, 1.0 / max(spread_pct, 0.001))

        # 추천식 반영:
        # Score = 0.40*rank_pct(ROC) + 0.30*rank_pct(LiquiditySpread) + 0.30*rank_pct(Vol_Breakout)
        liquidity_spread_score = (0.5 * tv_pct) + (0.5 * spread_score_pct)
        vol_breakout_pct = tv_pct  # 120일 퍼센타일 대체로 당일 cross-section 퍼센타일 사용

        if vol_breakout_pct < min_vol_pct:
            continue
        if spread_pct > max_spread_pct:
            continue

        score = (0.40 * roc_pct) + (0.30 * liquidity_spread_score) + (0.30 * vol_breakout_pct)
        scored.append((score, sym))

    scored.sort(reverse=True)
    out: List[str] = []
    for _, sym in scored:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= need_n:
            break
    meta.append(
        f"volume_rank fill={len(out)}/{need_n} pool={len(rows)} "
        f"formula=0.40*roc_pct+0.30*liqspr_pct+0.30*vol_pct vol>={min_vol_pct:.2f} spread<={max_spread_pct:.2f}"
    )
    return out


def _supplement_from_strength(
    need_n: int,
    seen: set[str],
    min_price: float,
    min_tv: float,
    block_rise: float,
    meta: List[str],
) -> List[str]:
    if need_n <= 0:
        return []
    if os.getenv("WATCH_FILL_FROM_STRENGTH", "1") != "1":
        return []

    markets = [m.strip() for m in os.getenv("STRENGTH_MARKETS", os.getenv("RANK_MARKETS", "J,NX")).split(",") if m.strip()]
    if not markets:
        return []

    rows: List[Dict[str, Any]] = []
    for m in markets:
        part, dbg = fetch_strength_rank(m)
        meta.extend(dbg)
        rows.extend(part)

    if not rows:
        meta.append("strength empty")
        return []

    scored: List[Tuple[float, str]] = []
    for it in rows:
        sym = _parse_sym(it)
        if not sym or sym in seen:
            continue

        r = _parse_float(it, "prdy_ctrt", 0.0)
        tv = _parse_float(it, "acml_tr_pbmn", 0.0)
        px = _parse_price(it)
        strength = _parse_strength(it)

        if min_price > 0 and px > 0 and px <= min_price:
            continue
        if r >= block_rise:
            continue
        if tv > 0 and tv < min_tv:
            continue

        score = (strength * 1e7) + tv + (r * 1e6)
        scored.append((score, sym))

    scored.sort(reverse=True)
    out: List[str] = []
    for _, sym in scored:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= need_n:
            break

    meta.append(f"strength fill={len(out)}/{need_n} pool={len(rows)}")
    return out


def _supplement_from_conditions(
    need_n: int,
    seen: set[str],
    min_price: float,
    min_tv: float,
    block_rise: float,
    meta: List[str],
) -> List[str]:
    if need_n <= 0:
        return []

    if os.getenv("WATCH_FILL_FROM_CONDITION", "1") != "1":
        return []

    seqs = [x.strip() for x in os.getenv("WATCH_COND_SEQS", "").split(",") if x.strip()]
    if not seqs:
        meta.append("cond skip(no seq)")
        return []

    try:
        cond_syms = scan_conditions(seqs)
    except Exception as e:
        meta.append(f"cond err(scan {type(e).__name__})")
        return []

    if not cond_syms:
        meta.append("cond empty")
        return []

    try:
        items = multi_quote(cond_syms)
    except Exception as e:
        meta.append(f"cond err(quote {type(e).__name__})")
        return []

    scored: List[Tuple[float, str]] = []
    for it in items:
        sym = _parse_sym(it)
        if not sym or sym in seen:
            continue

        r = _parse_float(it, "prdy_ctrt", 0.0)
        tv = _parse_float(it, "acml_tr_pbmn", 0.0)
        px = _parse_price(it)

        if min_price > 0 and px > 0 and px <= min_price:
            continue
        if r >= block_rise:
            continue
        if tv > 0 and tv < min_tv:
            continue

        scored.append((score_item(it), sym))

    scored.sort(reverse=True)
    out: List[str] = []
    for _, sym in scored:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= need_n:
            break

    meta.append(f"cond fill={len(out)}/{need_n} pool={len(cond_syms)}")
    return out


def get_last_build_meta() -> str:
    return _LAST_BUILD_META


def get_last_source_map() -> Dict[str, str]:
    return dict(_LAST_SOURCE_MAP)


def check_watchlist_integrity(symbols: List[str]) -> Dict[str, int]:
    """watchlist 기본 무결성 점검.

    - 형식(6자리 숫자), 중복, 저가 필터 위반
    - 가능하면 멀티시세로 현재가 확인하여 저가/미응답 개수도 집계
    """
    min_price = float(os.getenv("WATCH_MIN_PRICE", "10000"))

    total = len(symbols)
    bad_format = 0
    dup_count = 0
    seen = set()
    valid_syms: List[str] = []

    for s in symbols:
        ss = str(s).strip()
        if not (len(ss) == 6 and ss.isdigit()):
            bad_format += 1
            continue
        if ss in seen:
            dup_count += 1
            continue
        seen.add(ss)
        valid_syms.append(ss)

    quote_miss = 0
    low_price = 0
    use_quote = os.getenv("WATCH_INTEGRITY_WITH_QUOTE", "0") == "1"
    if valid_syms and use_quote:
        try:
            items = multi_quote(valid_syms)
            by_sym: Dict[str, Dict[str, Any]] = {}
            for it in items:
                sym = _parse_sym(it)
                if sym and sym not in by_sym:
                    by_sym[sym] = it

            for sym in valid_syms:
                it = by_sym.get(sym)
                if not it:
                    quote_miss += 1
                    continue
                px = _parse_price(it)
                if min_price > 0 and px > 0 and px <= min_price:
                    low_price += 1
        except Exception:
            quote_miss = len(valid_syms)
    elif valid_syms:
        quote_miss = -1

    return {
        "total": total,
        "unique": len(valid_syms),
        "bad_format": bad_format,
        "dup": dup_count,
        "low_price": low_price,
        "quote_miss": quote_miss,
    }


def build_watchlist() -> List[str]:
    """API 결과로 최대 N개 채움.
    1) 필터 통과 종목 우선
    2) 부족하면 같은 API 결과에서 필터 탈락분으로 보충
    3) 그래도 비면 FALLBACK_SYMBOLS 사용
    """
    global _LAST_BUILD_META, _LAST_SOURCE_MAP

    want_n = int(os.getenv("WATCH_TOP_N", "30"))
    min_tv = float(os.getenv("WATCH_MIN_TR_VALUE", "300000000"))
    block_rise = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "12.0"))
    min_price = float(os.getenv("WATCH_MIN_PRICE", "10000"))
    max_price = float(os.getenv("WATCH_MAX_PRICE", "500000"))
    min_chg = float(os.getenv("WATCH_MIN_CHANGE_PCT", "2.0"))
    max_chg = float(os.getenv("WATCH_MAX_CHANGE_PCT", "18.0"))
    min_strength = float(os.getenv("WATCH_MIN_STRENGTH", "110"))
    max_strength = float(os.getenv("WATCH_MAX_STRENGTH", "300"))

    markets = [m.strip() for m in os.getenv("RANK_MARKETS", "J,NX").split(",") if m.strip()]
    sort_codes = [s.strip() for s in os.getenv("RANK_SORT_CODES", "1,0").split(",") if s.strip()]

    out: List[str] = []
    seen = set()
    src_map: Dict[str, str] = {}

    preferred: List[Tuple[float, str]] = []
    backup: List[Tuple[float, str]] = []
    meta: List[str] = []
    drop_low_price = 0

    # 1) 거래량 상위(legacy-style)로 우선 30개를 채운다.
    vol_first = _supplement_from_volume_rank(
        want_n, seen, min_price, min_tv, block_rise, meta,
        max_price, min_chg, max_chg, min_strength, max_strength,
    )
    out.extend(vol_first)
    for sym in vol_first:
        src_map[sym] = "volume_rank"

    if len(out) >= want_n:
        meta.append("primary=volume_rank")
        _LAST_BUILD_META = " | ".join(meta[-24:])
        _LAST_SOURCE_MAP = src_map
        return out[:want_n]

    meta.append(f"volume_rank_short={len(out)}/{want_n}")

    # 2) 부족분은 기존 rank 소스에서 보충
    for m in markets:
        for sc in sort_codes:
            rows, dbg = fetch_rank(m, sc)
            meta.extend(dbg)
            for it in rows:
                sym = _parse_sym(it)
                if not sym or sym in seen:
                    continue

                r = _parse_float(it, "prdy_ctrt", 0.0)
                tv = _parse_float(it, "acml_tr_pbmn", 0.0)
                px = _parse_price(it)
                score = tv + (r * 1e7)

                if min_price > 0 and px > 0 and px <= min_price:
                    drop_low_price += 1
                    continue
                if not _passes_quality(it, min_price, max_price, min_chg, max_chg, min_strength, max_strength):
                    continue

                if r < block_rise and (tv <= 0 or tv >= min_tv):
                    preferred.append((score, sym))
                else:
                    backup.append((score, sym))

    for src_name, src in (("rank_pref", sorted(preferred, reverse=True)), ("rank_backup", sorted(backup, reverse=True))):
        for _, sym in src:
            if sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
            src_map[sym] = src_name
            if len(out) >= want_n:
                meta.append(f"rank_drop_low_price={drop_low_price}")
                _LAST_BUILD_META = " | ".join(meta[-24:])
                _LAST_SOURCE_MAP = src_map
                return out

    if len(out) < want_n:
        added = _supplement_from_volume_rank(
            want_n - len(out), seen, min_price, min_tv, block_rise, meta,
            max_price, min_chg, max_chg, min_strength, max_strength,
        )
        out.extend(added)
        for sym in added:
            src_map[sym] = "volume_rank"

    if len(out) < want_n:
        added = _supplement_from_strength(want_n - len(out), seen, min_price, min_tv, block_rise, meta)
        out.extend(added)
        for sym in added:
            src_map[sym] = "strength"

    if len(out) < want_n:
        added = _supplement_from_conditions(want_n - len(out), seen, min_price, min_tv, block_rise, meta)
        out.extend(added)
        for sym in added:
            src_map[sym] = "condition"

    if out:
        meta.append(f"rank_drop_low_price={drop_low_price}")
        _LAST_BUILD_META = " | ".join(meta[-24:])
        _LAST_SOURCE_MAP = src_map
        return out

    fb = _fallback_symbols()
    _LAST_BUILD_META = " | ".join(meta[-24:])
    _LAST_SOURCE_MAP = {sym: "fallback" for sym in fb[:want_n]}
    return fb[:want_n]
