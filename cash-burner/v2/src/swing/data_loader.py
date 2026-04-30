"""KIS 일봉 데이터 수집 + 캐시 + gap 검증.

저장 형식:
  data/daily/{sym}.csv  : date,open,high,low,close,volume,value
  data/daily/_calendar.csv : 거래일 캘린더 (005930 기준)
  data/daily/_index.json   : {sym: last_date} (빠른 증분 업데이트)

핵심:
  download_daily(sym, start, end)  -> DataFrame (페이지네이션 처리)
  update_daily(sym, end_date)      -> 캐시 마지막 날짜 이후만 fetch
  load_daily(sym)                  -> 캐시 DataFrame (date를 index로)
  build_calendar(end_date)         -> 거래일 리스트 추출
  check_gap(sym)                   -> 캐시와 캘린더 비교, 빠진 날짜 리포트
  update_universe(syms, end_date)  -> 일괄 업데이트
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR / "src"))

from kis_http import request

DAILY_DIR = BASE_DIR / "data" / "daily"
DAILY_DIR.mkdir(parents=True, exist_ok=True)
CAL_PATH = DAILY_DIR / "_calendar.csv"
IDX_PATH = DAILY_DIR / "_index.json"

API_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
TR_ID = "FHKST03010100"
CALL_DELAY = float(os.getenv("DAILY_CALL_DELAY", "0.25"))
CALENDAR_REF_SYM = "005930"  # 삼성전자 = 한국 거래일 기준


def _yyyymmdd(d: datetime | str) -> str:
    if isinstance(d, str):
        return d.replace("-", "")[:8]
    return d.strftime("%Y%m%d")


def _api_chunk(sym: str, start: str, end: str, adj: bool = True) -> list[dict]:
    """1회 API 호출. 최대 100건 (약 5개월)."""
    j = request("GET", API_PATH, TR_ID, params={
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": sym,
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0" if adj else "1",  # 0=수정주가, 1=원주가
    })
    rows = j.get("output2", []) or []
    return [r for r in rows if r.get("stck_bsop_date")]


def _parse_row(r: dict) -> dict:
    return {
        "date": r["stck_bsop_date"],
        "open": float(r.get("stck_oprc") or 0),
        "high": float(r.get("stck_hgpr") or 0),
        "low": float(r.get("stck_lwpr") or 0),
        "close": float(r.get("stck_clpr") or 0),
        "volume": int(r.get("acml_vol") or 0),
        "value": int(r.get("acml_tr_pbmn") or 0),  # 거래대금 (원)
    }


def download_daily(sym: str, start: str, end: str, adj: bool = True) -> pd.DataFrame:
    """페이지네이션 처리. start~end 전체 일봉을 받아옴."""
    start = _yyyymmdd(start)
    end = _yyyymmdd(end)
    out: list[dict] = []
    cur_end = end
    seen_min = end

    while True:
        rows = _api_chunk(sym, start, cur_end, adj=adj)
        if not rows:
            break
        for r in rows:
            out.append(_parse_row(r))

        # KIS는 cur_end 부터 역순으로 ~100건 반환. 가장 오래된 날짜 = rows[-1]
        oldest = rows[-1]["stck_bsop_date"]
        if oldest <= start or oldest >= seen_min:
            break  # 더 받을 게 없거나 진전 없음
        seen_min = oldest
        # 다음 chunk: oldest의 하루 전을 새 end로
        d = datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)
        cur_end = d.strftime("%Y%m%d")
        time.sleep(CALL_DELAY)

    if not out:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "value"])
    df = pd.DataFrame(out).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return df


def _load_index() -> dict[str, str]:
    if IDX_PATH.exists():
        return json.loads(IDX_PATH.read_text(encoding="utf-8"))
    return {}


def _save_index(idx: dict[str, str]) -> None:
    IDX_PATH.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")


def load_daily(sym: str) -> pd.DataFrame:
    """캐시 → DataFrame (date string index)."""
    p = DAILY_DIR / f"{sym}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, dtype={"date": str})
    df = df.set_index("date").sort_index()
    return df


def _save_daily(sym: str, df: pd.DataFrame) -> None:
    p = DAILY_DIR / f"{sym}.csv"
    if df.empty:
        return
    cols = ["date", "open", "high", "low", "close", "volume", "value"]
    df.to_csv(p, columns=cols, index=False)


def update_daily(sym: str, end_date: str | None = None,
                 default_start: str = "20180101", adj: bool = True) -> int:
    """캐시 last_date 이후 ~ end_date 까지만 fetch + merge. 신규 추가 행 수 반환."""
    end_date = _yyyymmdd(end_date or datetime.now())
    cur = load_daily(sym)
    if cur.empty:
        start = default_start
    else:
        last = cur.index.max()
        nxt = datetime.strptime(last, "%Y%m%d") + timedelta(days=1)
        start = nxt.strftime("%Y%m%d")
    if start > end_date:
        return 0

    new = download_daily(sym, start, end_date, adj=adj)
    if new.empty:
        return 0

    if cur.empty:
        merged = new.set_index("date")
    else:
        new_idx = new.set_index("date")
        merged = pd.concat([cur, new_idx[~new_idx.index.isin(cur.index)]]).sort_index()

    out = merged.reset_index()
    _save_daily(sym, out)

    idx = _load_index()
    idx[sym] = out["date"].max()
    _save_index(idx)
    return len(new)


def build_calendar(end_date: str | None = None,
                   default_start: str = "20180101") -> list[str]:
    """삼성전자 일봉으로 거래일 캘린더 추출."""
    update_daily(CALENDAR_REF_SYM, end_date=end_date, default_start=default_start)
    df = load_daily(CALENDAR_REF_SYM)
    dates = sorted(df.index.tolist())
    pd.DataFrame({"date": dates}).to_csv(CAL_PATH, index=False)
    return dates


def load_calendar() -> list[str]:
    if not CAL_PATH.exists():
        return build_calendar()
    return pd.read_csv(CAL_PATH, dtype={"date": str})["date"].tolist()


def check_gap(sym: str, calendar: list[str] | None = None,
              min_first_date: str | None = None) -> dict:
    """캐시와 캘린더 비교. 캐시 첫 날짜 이후 빠진 거래일을 리포트.

    상폐/신규상장 종목은 캘린더 일부만 가질 수 있으므로
    실제 데이터 기간(first~last)에서만 gap 체크.
    """
    df = load_daily(sym)
    if df.empty:
        return {"sym": sym, "status": "no_data", "missing": []}
    cal = calendar or load_calendar()
    cal_set = set(cal)
    have = set(df.index)
    first = df.index.min()
    last = df.index.max()
    if min_first_date and first < min_first_date:
        first = min_first_date
    expected = {d for d in cal_set if first <= d <= last}
    missing = sorted(expected - have)
    return {
        "sym": sym,
        "status": "ok" if not missing else "gap",
        "first": first, "last": last,
        "n_have": len(have & expected),
        "n_expect": len(expected),
        "missing": missing,
    }


def update_universe(syms: Iterable[str], end_date: str | None = None,
                    default_start: str = "20180101") -> dict:
    """일괄 업데이트. 진행상황 출력 + gap 리포트."""
    syms = list(syms)
    end_date = _yyyymmdd(end_date or datetime.now())
    print(f"=== 일봉 일괄 업데이트: {len(syms)}종목, ~{end_date} ===")
    cal = build_calendar(end_date=end_date, default_start=default_start)
    print(f"  캘린더: {len(cal)}거래일 ({cal[0]}~{cal[-1]})")

    t0 = time.time()
    new_total = 0
    errs = []
    gaps = []

    for i, sym in enumerate(syms):
        if sym == CALENDAR_REF_SYM:
            continue
        try:
            n = update_daily(sym, end_date=end_date, default_start=default_start)
            new_total += n
            if n > 0 or i % 50 == 0:
                elapsed = time.time() - t0
                rate = (i + 1) / max(elapsed, 1)
                eta = (len(syms) - i - 1) / max(rate, 0.01)
                print(f"  [{i+1}/{len(syms)}] {sym}: +{n}행 ({elapsed:.0f}s, ETA {eta:.0f}s)")
            time.sleep(CALL_DELAY)
        except Exception as e:
            errs.append((sym, str(e)))
            print(f"  [{i+1}/{len(syms)}] {sym}: ERROR {e}")
            time.sleep(1.0)

    # gap 검증
    for sym in syms:
        info = check_gap(sym, calendar=cal)
        if info["status"] == "gap" and len(info["missing"]) > 2:
            gaps.append(info)

    print(f"\n=== 완료: +{new_total}행, 에러 {len(errs)}개, gap {len(gaps)}종목, {time.time()-t0:.0f}s ===")
    if gaps[:5]:
        print(f"  gap 상위 5: {[(g['sym'], len(g['missing'])) for g in gaps[:5]]}")
    return {"new_rows": new_total, "errors": errs, "gaps": gaps}


def list_cached_symbols() -> list[str]:
    """data/daily/*.csv 중 종목 코드만."""
    out = []
    for p in DAILY_DIR.glob("*.csv"):
        s = p.stem
        if s.startswith("_"):
            continue
        if len(s) == 6 and s.isdigit():
            out.append(s)
    return sorted(out)


def load_universe(syms: list[str], min_value: float = 0.0) -> dict[str, pd.DataFrame]:
    """여러 종목 캐시 일괄 로드."""
    out = {}
    for s in syms:
        df = load_daily(s)
        if df.empty:
            continue
        if min_value > 0 and df["value"].mean() < min_value:
            continue
        out[s] = df
    return out
