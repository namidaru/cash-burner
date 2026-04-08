"""
Overnight gap backtest.

Strategy: 어제 종가에 매수, 오늘 시가에 매도. 갭만큼 수익.

Step 1: KIS 일봉 API로 풀 종목 100일치 OHLC 캐시 (data/daily_ohlc_cache.json)
Step 2: 캐시에서 백테스트
  - 다양한 selection rule 비교
  - 5종목 균등 매수, 다음날 시가 전량 매도
"""
import os, sys, json, time
from collections import defaultdict, Counter

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, "src")

from kis_http import request

CACHE_PATH = "data/daily_ohlc_cache.json"
VP_DIR = "data/volume_profiles"


def fetch_daily(sym, start="20260101", end="20260408"):
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": sym,
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    j = request(
        "GET",
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "FHKST03010100",
        params=params,
    )
    rows = j.get("output2", [])
    out = {}
    for r in rows:
        d = r.get("stck_bsop_date", "")
        try:
            out[d] = {
                "open": int(r["stck_oprc"]),
                "high": int(r["stck_hgpr"]),
                "low": int(r["stck_lwpr"]),
                "close": int(r["stck_clpr"]),
                "vol": int(r.get("acml_vol", 0) or 0),
                "tr": int(r.get("acml_tr_pbmn", 0) or 0),
            }
        except Exception:
            pass
    return out


def build_pool():
    """volume_profiles 600+ syms = 거래대금 큰 종목 풀 (이미 존재)"""
    syms = []
    for fname in os.listdir(VP_DIR):
        if not fname.endswith(".csv"):
            continue
        sym = fname[:-4]
        if len(sym) == 6 and sym.isdigit():
            syms.append(sym)
    return sorted(syms)


def cache_all():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"loaded cache: {len(cache)} syms")
    else:
        cache = {}

    pool = build_pool()
    print(f"pool: {len(pool)}")
    todo = [s for s in pool if s not in cache or len(cache.get(s, {})) < 50]
    print(f"todo: {len(todo)}")
    if not todo:
        return cache

    t0 = time.time()
    for i, sym in enumerate(todo):
        try:
            cache[sym] = fetch_daily(sym)
        except Exception as e:
            print(f"  {sym} ERR: {e}")
            cache[sym] = {}
        if i % 50 == 49:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (len(todo) - i - 1)
            print(f"  {i+1}/{len(todo)} elapsed={elapsed:.0f}s eta={eta:.0f}s", flush=True)
            # save partial
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        time.sleep(0.08)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    print(f"cached {len(cache)} syms in {time.time()-t0:.0f}s")
    return cache


def backtest(cache):
    # build per-day data: day_data[date][sym] = {open, high, low, close, tr, prev_close}
    day_data = defaultdict(dict)
    sym_dates = {}
    for sym, days in cache.items():
        if not days:
            continue
        sorted_dates = sorted(days.keys())
        sym_dates[sym] = sorted_dates
        for i, d in enumerate(sorted_dates):
            row = days[d]
            row = dict(row)
            row["prev_close"] = days[sorted_dates[i-1]]["close"] if i > 0 else 0
            day_data[d][sym] = row

    all_dates = sorted(day_data.keys())
    print(f"\nbacktest dates: {len(all_dates)} ({all_dates[0]} ~ {all_dates[-1]})")

    # ---------- selection rules ----------
    def overnight_return(sym, today_date, next_date):
        """held from today_close to next_open. return %"""
        td = day_data[today_date].get(sym, {})
        nd = day_data[next_date].get(sym, {})
        if not td or not nd:
            return None
        if td["close"] <= 0 or nd["open"] <= 0:
            return None
        return (nd["open"] / td["close"] - 1) * 100

    def get_next_date(sym, today_date):
        sd = sym_dates.get(sym, [])
        if today_date not in sd:
            return None
        idx = sd.index(today_date)
        if idx + 1 >= len(sd):
            return None
        return sd[idx + 1]

    # --- baseline: random 5 each day ---
    import random
    random.seed(42)

    def strat_random(d, n=5):
        syms = list(day_data[d].keys())
        random.shuffle(syms)
        return syms[:n]

    def strat_strong_close(d, n=5):
        """오늘 종가가 장중 고점 부근 (≥95%) — 강세 마감."""
        cands = []
        for sym, r in day_data[d].items():
            if r["high"] <= r["low"] or r["tr"] < 1e9:
                continue
            pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
            if pos >= 0.85:
                cands.append((sym, pos, r["tr"]))
        cands.sort(key=lambda x: -x[2])  # 거래대금 큰 거 우선
        return [c[0] for c in cands[:n]]

    def strat_weak_close(d, n=5):
        """오늘 종가 = 장중 저점 부근 (≤15%)."""
        cands = []
        for sym, r in day_data[d].items():
            if r["high"] <= r["low"] or r["tr"] < 1e9:
                continue
            pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
            if pos <= 0.15:
                cands.append((sym, pos, r["tr"]))
        cands.sort(key=lambda x: -x[2])
        return [c[0] for c in cands[:n]]

    def strat_high_tr(d, n=5):
        cands = sorted(day_data[d].items(), key=lambda x: -x[1]["tr"])
        return [c[0] for c in cands[:n] if c[1]["tr"] > 0]

    def strat_top_gainer(d, n=5):
        cands = []
        for sym, r in day_data[d].items():
            if r["prev_close"] <= 0 or r["tr"] < 1e9:
                continue
            chg = (r["close"] / r["prev_close"] - 1) * 100
            if chg > 0:
                cands.append((sym, chg))
        cands.sort(key=lambda x: -x[1])
        return [c[0] for c in cands[:n]]

    def strat_top_loser(d, n=5):
        cands = []
        for sym, r in day_data[d].items():
            if r["prev_close"] <= 0 or r["tr"] < 1e9:
                continue
            chg = (r["close"] / r["prev_close"] - 1) * 100
            if chg < 0:
                cands.append((sym, chg))
        cands.sort(key=lambda x: x[1])
        return [c[0] for c in cands[:n]]

    def strat_strong_high_tr(d, n=5):
        """강세마감 + 거래대금 5억+ 상위 — strong_close 더 엄격."""
        cands = []
        for sym, r in day_data[d].items():
            if r["high"] <= r["low"] or r["tr"] < 5e9:
                continue
            pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
            if pos >= 0.9:
                chg = (r["close"] / r["prev_close"] - 1) * 100 if r["prev_close"] > 0 else 0
                if chg > 1:  # 어제대비 양수
                    cands.append((sym, r["tr"]))
        cands.sort(key=lambda x: -x[1])
        return [c[0] for c in cands[:n]]

    def run(name, fn):
        all_pnls = []
        per_day = []
        days_used = 0
        for d in all_dates:
            picks = fn(d)
            if not picks:
                continue
            day_pnls = []
            for sym in picks:
                nd = get_next_date(sym, d)
                if not nd:
                    continue
                r = overnight_return(sym, d, nd)
                if r is None:
                    continue
                day_pnls.append(r)
            if day_pnls:
                days_used += 1
                per_day.append((d, sum(day_pnls), len(day_pnls)))
                all_pnls.extend(day_pnls)
        if not all_pnls:
            print(f"{name:35s} | no trades")
            return
        wins = sum(1 for p in all_pnls if p > 0)
        avg = sum(all_pnls) / len(all_pnls)
        total = sum(all_pnls)
        pos_days = sum(1 for _, t, _ in per_day if t > 0)
        print(f"{name:35s} | n={len(all_pnls):4d} win={wins*100//len(all_pnls):2d}% "
              f"avg={avg:+5.2f}% total={total:+8.2f}% days+={pos_days}/{days_used}")

    print(f"\n{'='*100}")
    print("OVERNIGHT GAP BACKTEST — buy at close, sell next open")
    print(f"{'='*100}\n")

    run("R0: random 5 (baseline)", strat_random)
    run("O1: 강세마감 (close near high)", strat_strong_close)
    run("O2: 약세마감 (close near low)", strat_weak_close)
    run("O3: 거래대금 상위 5 (방향무관)", strat_high_tr)
    run("O4: 어제 상승률 상위 5", strat_top_gainer)
    run("O5: 어제 하락률 상위 5", strat_top_loser)
    run("O6: 강세마감 + 거래대금 50억+", strat_strong_high_tr)

    # universe-wide stats
    print(f"\n{'='*100}")
    print("UNIVERSE STATS — 전체 종목 평균 오버나잇 갭")
    print(f"{'='*100}")
    all_gaps = []
    for d in all_dates:
        for sym in day_data[d]:
            nd = get_next_date(sym, d)
            if not nd:
                continue
            r = overnight_return(sym, d, nd)
            if r is not None:
                all_gaps.append(r)
    if all_gaps:
        avg = sum(all_gaps) / len(all_gaps)
        wins = sum(1 for g in all_gaps if g > 0)
        print(f"  총 샘플: {len(all_gaps)}")
        print(f"  평균 오버나잇 갭: {avg:+.3f}%")
        print(f"  +갭 비율: {wins*100//len(all_gaps)}%")
        print(f"  중간값: {sorted(all_gaps)[len(all_gaps)//2]:+.3f}%")


if __name__ == "__main__":
    cache = cache_all()
    backtest(cache)
