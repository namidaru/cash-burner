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

DEFAULT_TR_IDS = "FHPST01860000,VHPST01860000"
DEFAULT_STRENGTH_TR_IDS = "FHPST01710000,VHPST01710000"
DEFAULT_VOLUME_TR_IDS = "FHPST01710000,VHPST01710000"
_LAST_BUILD_META: str = ""
_LAST_SOURCE_MAP: Dict[str, str] = {}
_LAST_POOL_SYMS: List[str] = []
_LAST_DROPPED_DETAIL: List[str] = []
_SNAPSHOT_SAVED_TODAY: str = ""  # 당일 스냅샷 저장 여부 (YYYYMMDD)
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


def _save_selection_snapshot(pool_syms: List[str], selected: List[str],
                             src_map: Dict[str, str], dropped_detail: List[str],
                             scored: List[Tuple[float, str]]):
    """당일 첫 build_watchlist 결과를 스냅샷으로 저장 (하루 1회).

    장 마감 후 daily_compact에서 시가/종가를 붙여 종목선택 검증에 사용.
    """
    global _SNAPSHOT_SAVED_TODAY
    import time as _time
    today = _time.strftime("%Y%m%d")
    if _SNAPSHOT_SAVED_TODAY == today:
        return
    # 장 시작 전후(08:50~09:15)에만 스냅샷 저장 — 장중 리빌드는 무시
    hhmm = int(_time.strftime("%H%M"))
    if not (850 <= hhmm <= 915):
        return
    try:
        snap_dir = os.path.join("data", "logs", today)
        os.makedirs(snap_dir, exist_ok=True)
        snap_path = os.path.join(snap_dir, "selection_snapshot.json")
        # 탈락 사유를 dict로 정리
        drop_map: Dict[str, str] = {}
        for line in dropped_detail:
            parts = line.split(" DROP ", 1)
            if len(parts) == 2:
                drop_map[parts[0].strip()] = parts[1].strip()
        # scored → dict
        score_map = {sym: round(sc, 3) for sc, sym in scored}
        payload = {
            "date": today,
            "saved_at": _time.strftime("%H:%M:%S"),
            "pool_count": len(pool_syms),
            "selected_count": len(selected),
            "pool_symbols": pool_syms,
            "selected": selected,
            "source_map": {sym: src_map.get(sym, "") for sym in pool_syms if sym in src_map},
            "scores": score_map,
            "dropped": drop_map,
        }
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _SNAPSHOT_SAVED_TODAY = today
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


def _emit_watch_status(pool_size: int, selected: int, want_n: int, dropped: Dict[str, int], scored: List[Tuple[float, str]], quote_count: int,
                       pool_syms: List[str] | None = None, dropped_detail: List[str] | None = None):
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
    if pool_syms is not None:
        payload["pool_symbols"] = pool_syms
    if dropped_detail:
        payload["dropped_detail"] = dropped_detail
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



_preopen_fetched: bool = False


def _should_fetch_preopen() -> bool:
    global _preopen_fetched
    hhmm = _time_hhmm()
    if hhmm < 900:
        return True
    if hhmm < 920 and not _preopen_fetched:
        return True
    return False


def _normalize_rank_rows(j: Dict[str, Any]) -> List[Dict[str, Any]]:
    for k in ("output", "output1", "output2"):
        v = j.get(k)
        if isinstance(v, list) and v:
            return v
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


def fetch_strength_rank(market: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    path = os.getenv("STRENGTH_PATH", "/uapi/domestic-stock/v1/ranking/volume-power").strip() or "/uapi/domestic-stock/v1/ranking/volume-power"
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
                    syms_in = [_parse_sym(r) for r in rows]
                    debug_meta.append(f"s-ok m={market} tr={tr_id} scr={scr} rows={len(rows)} syms={syms_in}")
                else:
                    debug_meta.append(f"s-empty m={market} tr={tr_id} scr={scr} rt_cd={j.get('rt_cd','?')} msg1={j.get('msg1','')[:40]}")
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
            j = request("GET", "/uapi/domestic-stock/v1/quotations/volume-rank", tr_id, params=params)
            rows = _normalize_rank_rows(j)
            if rows:
                all_rows.extend(rows)
                syms_in = [_parse_sym(r) for r in rows]
                debug_meta.append(f"v-ok m={market} tr={tr_id} rows={len(rows)} syms={syms_in}")
            else:
                debug_meta.append(f"v-empty m={market} tr={tr_id} rt_cd={j.get('rt_cd','?')} msg1={j.get('msg1','')[:40]}")
        except Exception as e:
            debug_meta.append(f"v-err m={market} tr={tr_id} ex={type(e).__name__}")
        if len(all_rows) >= limit_n:
            return all_rows[:limit_n], debug_meta
    return all_rows[:limit_n], debug_meta


def fetch_fluctuation_rank(market: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    limit_n = int(os.getenv("FLUCTUATION_TOPK", "80"))
    params = {
        "fid_cond_mrkt_div_code": market,
        "fid_cond_scr_div_code": os.getenv("FLUCTUATION_SCR", "20006"),
        "fid_input_iscd": os.getenv("FLUCTUATION_INPUT_ISCD", "0000"),
        "fid_rank_sort_cls_code": os.getenv("FLUCTUATION_SORT", "0"),
        "fid_input_cnt_1": os.getenv("FLUCTUATION_CNT", "0"),
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": os.getenv("FLUCTUATION_VOL_CNT", "100000"),
        "fid_prc_cls_code": os.getenv("FLUCTUATION_PRC_CLS_CODE", "0"),
        "fid_div_cls_code": os.getenv("FLUCTUATION_DIV_CLS_CODE", "0"),
        "fid_rsfl_rate1": os.getenv("FLUCTUATION_RSFL_RATE1", ""),
        "fid_rsfl_rate2": os.getenv("FLUCTUATION_RSFL_RATE2", ""),
    }
    try:
        j = request("GET", "/uapi/domestic-stock/v1/ranking/fluctuation", "FHPST01700000", params=params)
        rows = _normalize_rank_rows(j)
        return rows[:limit_n], []
    except Exception as e:
        return [], [f"fetch_fluctuation_rank fail: {type(e).__name__}: {e}"]


def fetch_bulk_trans(market: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    limit_n = int(os.getenv("BULK_TRANS_TOPK", "60"))
    params = {
        "fid_cond_mrkt_div_code": market,
        "fid_cond_scr_div_code": os.getenv("BULK_TRANS_SCR", "20009"),
        "fid_input_iscd": "0000",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
    }
    try:
        j = request("GET", "/uapi/domestic-stock/v1/ranking/bulk-trans-num", "FHKST190900C0", params=params)
        rows = _normalize_rank_rows(j)
        return rows[:limit_n], []
    except Exception as e:
        return [], [f"fetch_bulk_trans fail: {type(e).__name__}: {e}"]


def fetch_preopen_rank() -> Tuple[List[Dict[str, Any]], List[str]]:
    errs: List[str] = []
    combined: List[Dict[str, Any]] = []
    seen_syms: set[str] = set()

    # 3-1. 시간외등락율순위 (API: v1_국내주식-104, FHPST02340000)
    overtime_limit = int(os.getenv("PREOPEN_OVERTIME_TOPK", "50"))
    params_ot = {
        "fid_cond_mrkt_div_code": os.getenv("PREOPEN_MARKET", "J"),
        "fid_cond_scr_div_code": "20234",
        "fid_input_iscd": "0000",
        "fid_mrkt_cls_code": "",           # 필수: 공백
        "fid_div_cls_code": "2",           # 필수: 2=상승률
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_trgt_cls_code": "",           # 필수: 공백
        "fid_trgt_exls_cls_code": "",      # 필수: 공백
    }
    try:
        j = request("GET", "/uapi/domestic-stock/v1/ranking/overtime-fluctuation", "FHPST02340000", params=params_ot)
        rt_cd = j.get("rt_cd", "")
        if rt_cd != "0":
            errs.append(f"fetch_preopen_rank(overtime) rt_cd={rt_cd} msg={j.get('msg1','')[:60]}")
        rows = _normalize_rank_rows(j)[:overtime_limit]
        for it in rows:
            sym = _parse_sym(it)
            if sym and sym not in seen_syms:
                seen_syms.add(sym)
                combined.append(it)
    except Exception as e:
        errs.append(f"fetch_preopen_rank(overtime) fail: {type(e).__name__}: {e}")

    # 3-2. 예상체결 상승상위 (API: v1_국내주식-103, FHPST01820000)
    exp_limit = int(os.getenv("PREOPEN_EXP_TOPK", "50"))
    params_exp = {
        "fid_cond_mrkt_div_code": os.getenv("PREOPEN_MARKET", "J"),
        "fid_cond_scr_div_code": "20182",
        "fid_input_iscd": "0000",
        "fid_rank_sort_cls_code": "0",     # 필수: 0=상승률
        "fid_div_cls_code": "0",           # 필수: 0=전체
        "fid_aply_rang_prc_1": "",         # 필수: 공백=전체
        "fid_vol_cnt": "",
        "fid_pbmn": "",                    # 필수: 공백=전체 (거래대금, 천원단위)
        "fid_blng_cls_code": "0",          # 필수: 0=전체
        "fid_mkop_cls_code": "0",          # 필수: 0=장전예상
    }
    try:
        j = request("GET", "/uapi/domestic-stock/v1/ranking/exp-trans-updown", "FHPST01820000", params=params_exp)
        rt_cd = j.get("rt_cd", "")
        if rt_cd != "0":
            errs.append(f"fetch_preopen_rank(exp_trans) rt_cd={rt_cd} msg={j.get('msg1','')[:60]}")
        rows = _normalize_rank_rows(j)[:exp_limit]
        for it in rows:
            sym = _parse_sym(it)
            if sym and sym not in seen_syms:
                seen_syms.add(sym)
                combined.append(it)
    except Exception as e:
        errs.append(f"fetch_preopen_rank(exp_trans) fail: {type(e).__name__}: {e}")

    return combined, errs


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
    for k in ("stck_prpr", "STCK_PRPR", "prpr", "PRPR", "stck_clpr", "STCK_CLPR", "close", "price", "cur_prc"):
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
    if min_price > 0 and px > 0 and px < min_price:
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
    accel = volume_acceleration(item)
    if accel > 0 and accel < min_accel:
        return False

    return True


def _supplement_from_volume_rank(
    need_n: int,
    seen: set[str],
    min_price: float,
    min_tv: float,
    block_rise: float,
    meta: List[str],
    max_price: float = 0.0,
    min_chg: float = 0.0,
    max_chg: float = 999.0,
    min_strength: float = 0.0,
    max_strength: float = 9999.0,
    min_vol: float = 0.0,
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
        vol = _parse_float(it, "acml_vol", 0.0)
        if min_vol > 0 and vol > 0 and vol < min_vol:
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
        # Score = 0.55*rank_pct(ROC) + 0.30*rank_pct(TV) + 0.15*rank_pct(SpreadScore)
        vol_breakout_pct = tv_pct  # 120일 퍼센타일 대체로 당일 cross-section 퍼센타일 사용

        supplement_vol_pct = float(os.getenv("SUPPLEMENT_VOL_PERCENTILE", "0.5"))
        if vol_breakout_pct < supplement_vol_pct:
            continue
        if spread_pct > max_spread_pct:
            continue

        score = (0.55 * roc_pct) + (0.30 * tv_pct) + (0.15 * spread_score_pct)
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
        f"formula=0.55*roc_pct+0.30*tv_pct+0.15*spread_score_pct vol>={min_vol_pct:.2f} spread<={max_spread_pct:.2f}"
    )
    return out


def _supplement_from_strength(
    need_n: int,
    seen: set[str],
    min_price: float,
    min_tv: float,
    block_rise: float,
    meta: List[str],
    max_price: float = 0.0,
    min_chg: float = 0.0,
    max_chg: float = 999.0,
    min_vol: float = 0.0,
    max_spread_pct: float = 999.0,
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

        if min_price > 0 and px > 0 and px < min_price:
            continue
        if max_price > 0 and px > max_price:
            continue
        if r < min_chg or r > max_chg:
            continue
        if r >= block_rise:
            continue
        if tv > 0 and tv < min_tv:
            continue
        vol = _parse_float(it, "acml_vol", 0.0)
        if min_vol > 0 and vol > 0 and vol < min_vol:
            continue
        spread_pct = _parse_spread_pct(it, 999.0)
        if max_spread_pct < 999.0 and spread_pct >= max_spread_pct:
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
    max_price: float = 0.0,
    min_chg: float = 0.0,
    max_chg: float = 999.0,
    min_vol: float = 0.0,
    max_spread_pct: float = 999.0,
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

        if min_price > 0 and px > 0 and px < min_price:
            continue
        if max_price > 0 and px > max_price:
            continue
        if r < min_chg or r > max_chg:
            continue
        if r >= block_rise:
            continue
        if tv > 0 and tv < min_tv:
            continue
        vol = _parse_float(it, "acml_vol", 0.0)
        if min_vol > 0 and vol > 0 and vol < min_vol:
            continue
        spread_pct = _parse_spread_pct(it, max_spread_pct * 0.8)
        if spread_pct >= max_spread_pct:
            continue

        sc = score_item(it)
        if math.isfinite(sc):
            scored.append((sc, sym))

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


def get_last_pool_syms() -> List[str]:
    return list(_LAST_POOL_SYMS)


def get_last_dropped_detail() -> List[str]:
    return list(_LAST_DROPPED_DETAIL)


def check_watchlist_integrity(symbols: List[str]) -> Dict[str, int]:
    """watchlist 기본 무결성 점검.

    - 형식(6자리 숫자), 중복, 저가 필터 위반
    - 가능하면 멀티시세로 현재가 확인하여 저가/미응답 개수도 집계
    """
    min_price = float(os.getenv("WATCH_MIN_PRICE", "5000"))

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
                if min_price > 0 and px > 0 and px < min_price:
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
    """REST 데이터로 1차 선별 후 점수 상위 종목만 워치리스트에 반영.

    단순화 원칙:
    - 소스: 거래량 랭크 + 체결강도 + 조건검색(있을 때)
    - 필터: 가격/등락/거래대금/스프레드 최소한만 적용
    - 정렬: score_item 기반 + 거래대금 보너스
    """
    global _LAST_BUILD_META, _LAST_SOURCE_MAP, _LAST_POOL_SYMS, _LAST_DROPPED_DETAIL, _preopen_fetched

    want_n = int(os.getenv("WATCH_TOP_N", "45"))  # 8:50 H0STANC0=1슬롯/종목 → 50슬롯 중 45개 사용
    min_price = float(os.getenv("WATCH_MIN_PRICE", "5000"))
    max_price = float(os.getenv("WATCH_MAX_PRICE", "150000"))
    # 장전(~09:00)에는 prdy_ctrt=0이므로 chg/tv/vol 필터 바이패스
    import time as _time
    _premarket = int(_time.strftime("%H%M")) < 900
    min_chg = 0.0 if _premarket else float(os.getenv("WATCH_MIN_CHANGE_PCT", "0.5"))
    max_chg = float(os.getenv("WATCH_MAX_CHANGE_PCT", "20.0"))
    hard_heat = float(os.getenv("WATCH_HARD_HEAT_PCT", "20.0"))
    soft_heat = float(os.getenv("WATCH_SOFT_HEAT_PCT", "14.0"))
    min_tv = 0.0 if _premarket else float(os.getenv("WATCH_MIN_TR_VALUE", "600000000"))
    min_vol = 0.0 if _premarket else float(os.getenv("WATCH_MIN_VOLUME", "30000"))
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
            # condition은 항상 덮어씀. strength는 condition이 없을 때만.
            if source == "condition" or "condition" not in source_seen[sym]:
                raw_by_sym[sym] = it

        if "condition" in source_seen[sym]:
            src_map[sym] = "condition"
        elif "strength" in source_seen[sym]:
            src_map[sym] = "strength"
        else:
            src_map[sym] = "volume_rank"

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

    # preopen / fluctuation / bulk_trans 후보 수집
    if _should_fetch_preopen():
        rows, errs = fetch_preopen_rank()
        meta.extend(errs)
        if rows:
            _preopen_fetched = True
        for it in rows:
            sym = _parse_sym(it)
            if sym and sym not in raw_by_sym:
                raw_by_sym[sym] = it
                src_map[sym] = "preopen"
    else:
        markets_j = [m for m in markets if m == "J"]  # J만 (API 제약)
        for m in markets_j:
            rows, errs = fetch_fluctuation_rank(m)
            meta.extend(errs)
            for it in rows:
                sym = _parse_sym(it)
                if sym and sym not in raw_by_sym:
                    raw_by_sym[sym] = it
                    src_map[sym] = "fluctuation"

            rows, errs = fetch_bulk_trans(m)
            meta.extend(errs)
            for it in rows:
                sym = _parse_sym(it)
                if sym and sym not in raw_by_sym:
                    raw_by_sym[sym] = it
                    src_map[sym] = "bulk_trans"

    if not raw_by_sym:
        _LAST_BUILD_META = "empty_pool -> no_trade"
        _LAST_SOURCE_MAP = {}
        _LAST_POOL_SYMS = []
        _LAST_DROPPED_DETAIL = []
        _emit_watch_status(0, 0, want_n, {"price": 0, "chg": 0, "heat": 0, "tv": 0, "vol": 0, "spread": 0, "score": 0, "largecap": 0}, [], 0,
                           pool_syms=[], dropped_detail=[])
        return []

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

    # 초대형주 블랙리스트 — 모멘텀 전략에 부적합한 시총 상위 종목
    # 대형주 블랙리스트 + 사용자 블랙리스트 통합
    _largecap_bl = set(s.strip() for s in os.getenv(
        "WATCH_LARGECAP_BLACKLIST",
        "005930,000660,035420,005380,005490,051910,006400,035720,068270,028260"
    ).split(",") if s.strip())
    _user_bl = set(s.strip() for s in os.getenv("WATCH_BLACKLIST", "").split(",") if s.strip())
    _largecap_bl |= _user_bl

    scored: List[Tuple[float, str]] = []
    dropped = {"price": 0, "chg": 0, "heat": 0, "tv": 0, "vol": 0, "spread": 0, "score": 0, "largecap": 0}
    dropped_detail: List[str] = []  # 탈락 종목 상세 로그

    for sym in syms:
        if sym in _largecap_bl:
            dropped["largecap"] += 1
            dropped_detail.append(f"{sym} DROP largecap_blacklist")
            continue

        it = by_sym_q.get(sym) or raw_by_sym.get(sym) or {}

        px = _parse_price(it)
        if (min_price > 0 and px > 0 and px < min_price) or (max_price > 0 and px > max_price):
            dropped["price"] += 1
            dropped_detail.append(f"{sym} DROP price={px:.0f}")
            continue

        chg = _parse_float(it, "prdy_ctrt", 0.0)
        if chg < min_chg or chg > max_chg:
            dropped["chg"] += 1
            dropped_detail.append(f"{sym} DROP chg={chg:.2f}%")
            continue
        # max_chg 완화 시를 대비한 추가 과열 차단
        if chg > hard_heat:
            dropped["heat"] += 1
            dropped_detail.append(f"{sym} DROP heat={chg:.2f}%")
            continue

        tv = _parse_float(it, "acml_tr_pbmn", 0.0)
        if tv > 0 and tv < min_tv:
            dropped["tv"] += 1
            dropped_detail.append(f"{sym} DROP tv={tv:.0f}<{min_tv:.0f}")
            continue

        vol = _parse_float(it, "acml_vol", 0.0)
        if vol > 0 and vol < min_vol:
            dropped["vol"] += 1
            dropped_detail.append(f"{sym} DROP vol={vol:.0f}<{min_vol:.0f}")
            continue

        spread_pct = _parse_spread_pct(it, max_spread_pct * 0.8)
        if spread_pct >= max_spread_pct:
            dropped["spread"] += 1
            dropped_detail.append(f"{sym} DROP spread={spread_pct:.3f}%>={max_spread_pct:.3f}%")
            continue

        score = score_item(it)
        if not math.isfinite(score):
            dropped["score"] += 1
            dropped_detail.append(f"{sym} DROP score=NaN")
            continue

        scored.append((score, sym))

    scored.sort(reverse=True)

    out: List[str] = []
    for _, sym in scored:
        out.append(sym)
        if len(out) >= want_n:
            break

    if len(out) < want_n:
        prev_n = len(out)
        out += _supplement_from_volume_rank(
            want_n - len(out), set(out), min_price, min_tv, hard_heat, meta,
            max_price=max_price, min_chg=min_chg, max_chg=max_chg,
            min_vol=min_vol,
        )
        for sym in out[prev_n:]:
            src_map[sym] = "volume_rank"
    if len(out) < want_n:
        prev_n = len(out)
        out += _supplement_from_strength(
            want_n - len(out), set(out), min_price, min_tv, hard_heat, meta,
            max_price=max_price, min_chg=min_chg, max_chg=max_chg,
            min_vol=min_vol, max_spread_pct=max_spread_pct,
        )
        for sym in out[prev_n:]:
            src_map[sym] = "strength"
    if len(out) < want_n:
        prev_n = len(out)
        out += _supplement_from_conditions(
            want_n - len(out), set(out), min_price, min_tv, hard_heat, meta,
            max_price=max_price, min_chg=min_chg, max_chg=max_chg,
            min_vol=min_vol, max_spread_pct=max_spread_pct,
        )
        for sym in out[prev_n:]:
            src_map[sym] = "condition"

    _LAST_POOL_SYMS = syms
    _LAST_DROPPED_DETAIL = dropped_detail

    if not out:
        _LAST_BUILD_META = f"all_filtered drop={dropped} -> no_trade"
        _LAST_SOURCE_MAP = {}
        _emit_watch_status(len(raw_by_sym), 0, want_n, dropped, scored, len(by_sym_q),
                           pool_syms=syms, dropped_detail=dropped_detail)
        return []

    c_preopen = sum(1 for s in out if src_map.get(s) == "preopen")
    c_fluctuation = sum(1 for s in out if src_map.get(s) == "fluctuation")
    c_bulk_trans = sum(1 for s in out if src_map.get(s) == "bulk_trans")
    _LAST_BUILD_META = (
        f"simple_rest pool={len(raw_by_sym)} quote={len(by_sym_q)} selected={len(out)} "
        f"drop_price={dropped['price']} drop_chg={dropped['chg']} drop_heat={dropped['heat']} "
        f"drop_tv={dropped['tv']} drop_vol={dropped['vol']} drop_spread={dropped['spread']} drop_score={dropped['score']} soft_heat={soft_heat:.1f} "
        f"pre={c_preopen} fluc={c_fluctuation} bulk={c_bulk_trans}"
    )
    _LAST_SOURCE_MAP = {sym: src_map.get(sym, "rest") for sym in out}
    _emit_watch_status(len(raw_by_sym), len(out), want_n, dropped, scored, len(by_sym_q),
                       pool_syms=syms, dropped_detail=dropped_detail)
    _save_selection_snapshot(syms, out, src_map, dropped_detail, scored)
    return out
