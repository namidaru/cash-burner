from __future__ import annotations

import os
import math
import re
import json
import datetime as _dt
from typing import Any, Dict, List, Tuple

from kis_http import request
from scanner_conditions import scan as scan_conditions
from quote_multi import multi_quote, score_item, volume_acceleration

PATH = "/uapi/domestic-stock/v1/ranking/traded-by-company"
STRENGTH_PATH = "/uapi/domestic-stock/v1/ranking/volume-power"
VOLUME_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
DEFAULT_TR_IDS = "FHPST01860000,VHPST01860000"
DEFAULT_STRENGTH_TR_IDS = "FHPST01710000,VHPST01710000"
DEFAULT_VOLUME_TR_IDS = "FHPST01710000,VHPST01710000"
_LAST_BUILD_META: str = ""
_LAST_SOURCE_MAP: Dict[str, str] = {}
_WATCH_STATUS_FILE = os.getenv("WATCH_STATUS_FILE", os.path.join("data", "watch_status.json"))


def _write_watch_status(payload: Dict[str, Any]):
    try:
        d = os.path.dirname(_WATCH_STATUS_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_WATCH_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _operator_summary_watch(selected: int, want_n: int, filtered_total: int, pool_size: int, top5_scores: List[Tuple[float, str]]) -> str:
    watch_ok = selected >= max(3, want_n // 2)
    filter_ratio = (filtered_total / max(1, pool_size)) if pool_size > 0 else 0.0
    avg_top_score = sum(sc for sc, _ in top5_scores) / max(1, len(top5_scores))

    if selected <= max(2, want_n // 3) and filter_ratio >= 0.70:
        return "watch_small and scanner_overfiltered"
    if watch_ok and avg_top_score < 20.0:
        return "watch_ok but score_blocked"
    if watch_ok and avg_top_score >= 20.0 and filter_ratio >= 0.50:
        return "scores_ok but hard_gates_blocking"
    if watch_ok and avg_top_score >= 20.0 and filter_ratio < 0.50:
        return "buying_normally"
    return "watch_ok monitoring"


def _emit_watch_status(pool_size: int, selected: int, want_n: int, dropped: Dict[str, int], scored: List[Tuple[float, str]], quote_count: int):
    top5 = [{"symbol": sym, "score": round(float(sc), 3)} for sc, sym in scored[:5]]
    filtered_total = int(sum(int(v) for v in dropped.values()))
    filter_ratio = (filtered_total / max(1, pool_size)) if pool_size > 0 else 0.0
    payload = {
        "ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scanner_pool_summary": {
            "pool_size": int(pool_size),
            "quote_count": int(quote_count),
            "selected": int(selected),
            "want_n": int(want_n),
        },
        "scanner_drop_summary": {
            **{k: int(v) for k, v in dropped.items()},
            "filtered_total": filtered_total,
            "filter_ratio": round(filter_ratio, 4),
        },
        "top5_scores": top5,
        "operator_summary": _operator_summary_watch(selected, want_n, filtered_total, pool_size, scored[:5]),
    }
    _write_watch_status(payload)


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


def _in_preopen_window() -> bool:
    start_hhmm = int(os.getenv("SCAN_PREOPEN_START_HHMM", "900"))
    track_min = int(os.getenv("SCAN_PREOPEN_TRACK_MIN", "15"))
    hhmm = _time_hhmm()
    start_min = (start_hhmm // 100) * 60 + (start_hhmm % 100)
    now_min = (hhmm // 100) * 60 + (hhmm % 100)
    return start_min <= now_min < (start_min + track_min)


def _preopen_combo_seed(
    seen: set[str],
    meta: List[str],
    src_map: Dict[str, str],
    min_price: float,
    max_price: float,
    min_chg: float,
    max_chg: float,
    min_strength: float,
    max_strength: float,
    min_tv: float,
    block_rise: float,
) -> List[str]:
    if os.getenv("SCAN_PREOPEN_COMBO_ENABLE", "1") != "1":
        return []
    if not _in_preopen_window():
        return []

    preopen_n = int(os.getenv("PREOPEN_GOOD_TOP_N", "15"))
    volume_n = int(os.getenv("PREOPEN_VOLUME_TOP_N", "15"))

    scored_by_sym: Dict[str, Tuple[float, str]] = {}

    seqs = [x.strip() for x in os.getenv("WATCH_COND_SEQS", "").split(",") if x.strip()]
    if seqs and preopen_n > 0:
        try:
            cond_syms = scan_conditions(seqs)[: max(preopen_n * 4, preopen_n)]
            cond_items = multi_quote(cond_syms) if cond_syms else []
        except Exception as e:
            meta.append(f"preopen cond err({type(e).__name__})")
            cond_items = []

        cond_scored: List[Tuple[float, str]] = []
        for it in cond_items:
            sym = _parse_sym(it)
            if not sym:
                continue
            if not _passes_quality(it, min_price, max_price, min_chg, max_chg, min_strength, max_strength):
                continue
            r = _parse_float(it, "prdy_ctrt", 0.0)
            tv = _parse_float(it, "acml_tr_pbmn", 0.0)
            if r >= block_rise:
                continue
            if tv > 0 and tv < min_tv:
                continue
            cond_scored.append((score_item(it), sym))

        cond_scored.sort(reverse=True)
        for score, sym in cond_scored[:preopen_n]:
            old = scored_by_sym.get(sym)
            if old is None or score > old[0]:
                scored_by_sym[sym] = (score, "preopen_good")

    if volume_n > 0:
        markets = [m.strip() for m in os.getenv("VOLUME_RANK_MARKETS", os.getenv("RANK_MARKETS", "J,NX")).split(",") if m.strip()]
        rows: List[Dict[str, Any]] = []
        for m in markets:
            part, dbg = fetch_volume_rank(m)
            meta.extend(dbg)
            rows.extend(part)

        vol_scored: List[Tuple[float, str]] = []
        for it in rows:
            sym = _parse_sym(it)
            if not sym:
                continue
            if not _passes_quality(it, min_price, max_price, min_chg, max_chg, min_strength, max_strength):
                continue
            r = _parse_float(it, "prdy_ctrt", 0.0)
            tv = _parse_float(it, "acml_tr_pbmn", 0.0)
            if r >= block_rise:
                continue
            if tv > 0 and tv < min_tv:
                continue
            vol_scored.append((score_item(it), sym))

        vol_scored.sort(reverse=True)
        for score, sym in vol_scored[:volume_n]:
            old = scored_by_sym.get(sym)
            if old is None or score > old[0]:
                scored_by_sym[sym] = (score, "volume_rank")

    min_score = float(os.getenv("PREOPEN_SCORE_MIN", "0"))
    merged = sorted(((sc, sym, src) for sym, (sc, src) in scored_by_sym.items()), reverse=True)

    out: List[str] = []
    for score, sym, src in merged:
        if score < min_score:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        src_map[sym] = src
        out.append(sym)

    meta.append(
        f"preopen_combo pick={len(out)} cond_top={preopen_n} vol_top={volume_n} min_score={min_score:.1f}"
    )
    return out


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

    min_accel = float(os.getenv("WATCH_MIN_VOLUME_ACCEL", "0.8"))
    if volume_acceleration(item) < min_accel:
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
    min_price = float(os.getenv("WATCH_MIN_PRICE", "1000"))

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




def _radar_mode_enabled() -> bool:
    return os.getenv("WATCH_RADAR_MODE", "0") == "1"

def build_watchlist() -> List[str]:
    """REST 데이터로 1차 선별 후 점수 상위 종목만 워치리스트에 반영.

    단순화 원칙:
    - 소스: 거래량 랭크 + 체결강도 + 조건검색(있을 때)
    - 필터: 가격/등락/거래대금/스프레드 최소한만 적용
    - 정렬: score_item 기반 + 거래대금 보너스
    """
    global _LAST_BUILD_META, _LAST_SOURCE_MAP

    want_n = int(os.getenv("WATCH_TOP_N", "12"))
    min_price = float(os.getenv("WATCH_MIN_PRICE", "1000"))
    max_price = float(os.getenv("WATCH_MAX_PRICE", "200000"))
    min_chg = float(os.getenv("WATCH_MIN_CHANGE_PCT", "1.0"))
    max_chg = float(os.getenv("WATCH_MAX_CHANGE_PCT", "8.0"))
    hard_heat = float(os.getenv("WATCH_HARD_HEAT_PCT", "9.5"))
    min_tv = float(os.getenv("WATCH_MIN_TR_VALUE", "600000000"))
    min_vol = float(os.getenv("WATCH_MIN_VOLUME", "30000"))
    max_spread_pct = float(os.getenv("WATCH_MAX_SPREAD_PCT", "0.35"))

    markets = [m.strip() for m in os.getenv("RANK_MARKETS", "J,NX").split(",") if m.strip()]
    seqs = [x.strip() for x in os.getenv("WATCH_COND_SEQS", "").split(",") if x.strip()]

    src_map: Dict[str, str] = {}
    meta: List[str] = []
    raw_by_sym: Dict[str, Dict[str, Any]] = {}
    source_seen: Dict[str, set[str]] = {}

    def _add_candidate(it: Dict[str, Any], source: str):
        sym = _parse_sym(it)
        if not sym:
            return
        source_seen.setdefault(sym, set()).add(source)
        if sym not in raw_by_sym:
            raw_by_sym[sym] = it
        elif source in ("condition", "strength"):
            # condition/strength row는 quote 필드가 더 풍부한 경우가 많아 우선 반영
            raw_by_sym[sym] = it

        if "condition" in source_seen[sym]:
            src_map[sym] = "condition"
        elif "strength" in source_seen[sym]:
            src_map[sym] = "strength"
        else:
            src_map[sym] = "volume_rank"

    if _radar_mode_enabled():
        radar_markets = [m.strip() for m in os.getenv("RADAR_VOLUME_MARKETS", os.getenv("VOLUME_RANK_MARKETS", "J")).split(",") if m.strip()]
        radar_pool = int(os.getenv("RADAR_VOLUME_TOPK", "100"))
        strength_markets = [m.strip() for m in os.getenv("RADAR_STRENGTH_MARKETS", os.getenv("RANK_MARKETS", "J,NX")).split(",") if m.strip()]
        strength_pool = int(os.getenv("RADAR_STRENGTH_TOPK", "120"))
        cond_pool = int(os.getenv("RADAR_CONDITION_TOPK", "80"))
        src_counts = {"volume_rank": 0, "strength": 0, "condition": 0}
        dropped = {"price": 0, "chg": 0, "heat": 0, "tv": 0, "vol": 0, "spread": 0, "score": 0}
        scored: List[Tuple[float, str]] = []

        for m in radar_markets:
            rows, dbg = fetch_volume_rank(m)
            meta.extend(dbg)
            if radar_pool > 0:
                rows = rows[:radar_pool]
            for it in rows:
                before = len(raw_by_sym)
                _add_candidate(it, "volume_rank")
                if len(raw_by_sym) > before:
                    src_counts["volume_rank"] += 1

        for m in strength_markets:
            rows, dbg = fetch_strength_rank(m)
            meta.extend(dbg)
            if strength_pool > 0:
                rows = rows[:strength_pool]
            for it in rows:
                before = len(raw_by_sym)
                _add_candidate(it, "strength")
                if len(raw_by_sym) > before:
                    src_counts["strength"] += 1

        if seqs:
            try:
                cond_syms = scan_conditions(seqs)
                if cond_pool > 0:
                    cond_syms = cond_syms[:cond_pool]
                items = multi_quote(cond_syms) if cond_syms else []
            except Exception as e:
                meta.append(f"cond err({type(e).__name__})")
                items = []
            for it in items:
                before = len(raw_by_sym)
                _add_candidate(it, "condition")
                if len(raw_by_sym) > before:
                    src_counts["condition"] += 1

        for sym, it in raw_by_sym.items():
            px = _parse_price(it)
            if (min_price > 0 and px > 0 and px < min_price) or (max_price > 0 and px > max_price):
                dropped["price"] += 1
                continue

            chg = _parse_float(it, "prdy_ctrt", 0.0)
            if chg > hard_heat:
                dropped["heat"] += 1
                continue
            if chg < min_chg or chg > max_chg:
                dropped["chg"] += 1
                continue

            tv = _parse_float(it, "acml_tr_pbmn", 0.0)
            if tv > 0 and tv < min_tv:
                dropped["tv"] += 1
                continue

            vol = _parse_float(it, "acml_vol", 0.0)
            if vol > 0 and vol < min_vol:
                dropped["vol"] += 1
                continue

            spread_pct = _parse_spread_pct(it, max_spread_pct * 0.8)
            if spread_pct >= max_spread_pct:
                dropped["spread"] += 1
                continue

            momentum_score = chg
            liquidity_score = math.log1p(max(tv, 0.0))
            volume_score = math.log1p(max(vol, 0.0))
            volume_accel = min(max(volume_acceleration(it), 0.0), 3.0)

            score = (
                (momentum_score * 2.0)
                + (liquidity_score * 0.7)
                + (volume_score * 0.3)
                + (volume_accel * 1.8)
                - (spread_pct * 2.5)
            )
            if not math.isfinite(score):
                dropped["score"] += 1
                continue
            scored.append((score, sym))

        scored.sort(reverse=True)
        out = [sym for _, sym in scored[:want_n]]
        if not out:
            fb = _fallback_symbols()[:want_n]
            _LAST_BUILD_META = f"radar_volume empty drop={dropped} -> fallback"
            _LAST_SOURCE_MAP = {sym: "fallback" for sym in fb}
            _emit_watch_status(len(raw_by_sym), len(fb), want_n, dropped, scored, len(raw_by_sym))
            return fb

        _LAST_BUILD_META = (
            f"radar_combo markets={radar_markets} pool={len(raw_by_sym)} selected={len(out)} "
            f"volume_rank={src_counts['volume_rank']} strength={src_counts['strength']} condition={src_counts['condition']} "
            f"drop_price={dropped['price']} drop_chg={dropped['chg']} drop_heat={dropped['heat']} "
            f"drop_tv={dropped['tv']} drop_vol={dropped['vol']} drop_spread={dropped['spread']} drop_score={dropped['score']}"
        )
        _LAST_SOURCE_MAP = {sym: src_map.get(sym, "volume_rank") for sym in out}
        _emit_watch_status(len(raw_by_sym), len(out), want_n, dropped, scored, len(raw_by_sym))
        return out

    for m in markets:
        rows, dbg = fetch_volume_rank(m)
        meta.extend(dbg)
        for it in rows:
            _add_candidate(it, "volume_rank")

    for m in markets:
        rows, dbg = fetch_strength_rank(m)
        meta.extend(dbg)
        for it in rows:
            _add_candidate(it, "strength")

    if seqs:
        try:
            cond_syms = scan_conditions(seqs)
            items = multi_quote(cond_syms) if cond_syms else []
        except Exception as e:
            meta.append(f"cond err({type(e).__name__})")
            items = []
        for it in items:
            _add_candidate(it, "condition")

    if not raw_by_sym:
        fb = _fallback_symbols()[:want_n]
        _LAST_BUILD_META = "empty_pool -> fallback"
        _LAST_SOURCE_MAP = {sym: "fallback" for sym in fb}
        _emit_watch_status(0, len(fb), want_n, {"price": 0, "chg": 0, "heat": 0, "tv": 0, "vol": 0, "spread": 0, "score": 0}, [], 0)
        return fb

    syms = list(raw_by_sym.keys())
    try:
        q_items = multi_quote(syms)
    except Exception:
        q_items = []

    by_sym_q: Dict[str, Dict[str, Any]] = {}
    for it in q_items:
        sym = _parse_sym(it)
        if sym and sym not in by_sym_q:
            by_sym_q[sym] = it

    scored: List[Tuple[float, str]] = []
    dropped = {"price": 0, "chg": 0, "heat": 0, "tv": 0, "vol": 0, "spread": 0, "score": 0}

    for sym in syms:
        it = by_sym_q.get(sym) or raw_by_sym.get(sym) or {}

        px = _parse_price(it)
        if (min_price > 0 and px > 0 and px < min_price) or (max_price > 0 and px > max_price):
            dropped["price"] += 1
            continue

        chg = _parse_float(it, "prdy_ctrt", 0.0)
        if chg > hard_heat:
            dropped["heat"] += 1
            continue
        if chg < min_chg or chg > max_chg:
            dropped["chg"] += 1
            continue

        tv = _parse_float(it, "acml_tr_pbmn", 0.0)
        if tv > 0 and tv < min_tv:
            dropped["tv"] += 1
            continue

        vol = _parse_float(it, "acml_vol", 0.0)
        if vol > 0 and vol < min_vol:
            dropped["vol"] += 1
            continue

        spread_pct = _parse_spread_pct(it, max_spread_pct * 0.8)
        if spread_pct >= max_spread_pct:
            dropped["spread"] += 1
            continue

        momentum_score = chg
        liquidity_score = math.log1p(max(tv, 0.0))
        volume_score = math.log1p(max(vol, 0.0))
        volume_accel = min(max(volume_acceleration(it), 0.0), 3.0)

        score = (
            (momentum_score * 2.0)
            + (liquidity_score * 0.7)
            + (volume_score * 0.3)
            + (volume_accel * 1.8)
            - (spread_pct * 2.5)
        )
        if not math.isfinite(score):
            dropped["score"] += 1
            continue

        scored.append((score, sym))

    scored.sort(reverse=True)

    out: List[str] = []
    for _, sym in scored:
        out.append(sym)
        if len(out) >= want_n:
            break

    if not out:
        fb = _fallback_symbols()[:want_n]
        _LAST_BUILD_META = f"all_filtered drop={dropped} -> fallback"
        _LAST_SOURCE_MAP = {sym: "fallback" for sym in fb}
        _emit_watch_status(len(raw_by_sym), len(fb), want_n, dropped, scored, len(by_sym_q))
        return fb

    _LAST_BUILD_META = (
        f"simple_rest pool={len(raw_by_sym)} quote={len(by_sym_q)} selected={len(out)} "
        f"drop_price={dropped['price']} drop_chg={dropped['chg']} drop_heat={dropped['heat']} "
        f"drop_tv={dropped['tv']} drop_vol={dropped['vol']} drop_spread={dropped['spread']} drop_score={dropped['score']}"
    )
    _LAST_SOURCE_MAP = {sym: src_map.get(sym, "rest") for sym in out}
    _emit_watch_status(len(raw_by_sym), len(out), want_n, dropped, scored, len(by_sym_q))
    return out


def get_last_build_meta() -> str:
    return _LAST_BUILD_META


def get_last_source_map() -> Dict[str, str]:
    return dict(_LAST_SOURCE_MAP)
