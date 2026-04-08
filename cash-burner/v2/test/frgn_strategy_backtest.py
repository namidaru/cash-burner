"""
외인 매수 시그널 기반 실전 strategy backtest.

전략:
  매일 D 종가 후, 외인 순매수 (수량/평균거래량) 상위 N 종목 선정.
  D+1 시가 매수, D+1 종가 매도 (혹은 D+M 보유).

variants:
  - top 5/10/20
  - hold 1/2/3 days
  - frgn streak filter (연속 매수 종목만)
  - 거래대금 필터 (50억 이상)
"""
import os, json, math
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/investor_cache.json", "r", encoding="utf-8") as f:
    inv = json.load(f)
with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    ohlc = json.load(f)

syms = sorted(set(inv.keys()) & set(ohlc.keys()))
LIKELY_ETFS = {
    "069500","122630","252670","233740","379800","114800","229200","251340",
    "153130","132030","278530","294400",
}

# 모든 날짜 (외인 cache 기준)
all_dates = sorted(set(d for s in syms for d in inv[s]))
print(f"dates: {len(all_dates)}  ({all_dates[0]}~{all_dates[-1]})")
print(f"syms: {len(syms)}")


def get_close(sym, d):
    return ohlc.get(sym, {}).get(d, {}).get("close", 0)
def get_open(sym, d):
    return ohlc.get(sym, {}).get(d, {}).get("open", 0)
def get_vol(sym, d):
    return ohlc.get(sym, {}).get(d, {}).get("vol", 0)
def get_tr(sym, d):
    return ohlc.get(sym, {}).get(d, {}).get("tr", 0)


def avg_vol(sym, d, n=20):
    days = sorted(ohlc.get(sym, {}).keys())
    if d not in days:
        return 0
    i = days.index(d)
    if i < n:
        return 0
    return mean(ohlc[sym][days[j]].get("vol", 0) for j in range(i-n, i))


def get_prev_n_dates(sym, d, n):
    days = sorted(inv[sym].keys())
    if d not in days:
        return []
    i = days.index(d)
    if i < n - 1:
        return []
    return days[i-n+1:i+1]


def select_top_frgn(d, top_k=10, exclude_etf=True, min_tr=5e9, streak_only=False):
    cands = []
    for sym in syms:
        if exclude_etf and sym in LIKELY_ETFS:
            continue
        if d not in inv[sym]:
            continue
        # 거래대금 필터
        tr = get_tr(sym, d)
        if tr < min_tr:
            continue
        # 외인 매수 정규화
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0:
            continue
        frgn = inv[sym][d]["frgn_qty"]
        norm = frgn / avgv
        if norm <= 0:
            continue  # only positive
        # streak 필터
        if streak_only:
            prev = get_prev_n_dates(sym, d, 3)
            if len(prev) < 3:
                continue
            if not all(inv[sym][p]["frgn_qty"] > 0 for p in prev):
                continue
        cands.append((sym, norm))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:top_k]]


def backtest(top_k, hold_days, exclude_etf=True, min_tr=5e9, streak=False, exit_close=True):
    """매일 D 종가 후 선정 → D+1 시가 매수 → D+hold 종가 매도."""
    daily_pnls = []
    by_day = defaultdict(list)
    n_picks = 0
    for di in range(len(all_dates) - hold_days):
        d = all_dates[di]
        picks = select_top_frgn(d, top_k=top_k, exclude_etf=exclude_etf, min_tr=min_tr, streak_only=streak)
        if not picks:
            continue
        d_entry = all_dates[di+1]
        d_exit = all_dates[di + hold_days]
        for sym in picks:
            entry = get_open(sym, d_entry)
            exit_p = get_close(sym, d_exit)
            if entry <= 0 or exit_p <= 0:
                continue
            r = (exit_p/entry - 1)*100
            daily_pnls.append(r)
            by_day[d_entry].append(r)
            n_picks += 1
    return daily_pnls, by_day, n_picks


def equity(rs):
    eq = 100.0
    peak = 100.0
    mdd = 0.0
    for r in rs:
        eq *= (1 + r/100)
        if eq > peak: peak = eq
        dd = (eq/peak-1)*100
        if dd < mdd: mdd = dd
    return eq, mdd


# baseline: kospi200 매일 매수 (1일)
print(f"\n[BASELINE] 069500 매일 시가매수 → 종가매도 (1일)")
rs = []
for di in range(len(all_dates)-1):
    d_e = all_dates[di+1]
    o = get_open("069500", d_e); c = get_close("069500", d_e)
    if o > 0 and c > 0:
        rs.append((c/o - 1)*100)
if rs:
    eq, mdd = equity(rs)
    wins = sum(1 for r in rs if r > 0)
    print(f"  n={len(rs)}  avg={mean(rs):+.3f}%  total={sum(rs):+.2f}%  eq={eq:.1f}  mdd={mdd:.2f}%  win={wins*100//len(rs)}%")

print(f"\n{'='*100}")
print("외인 1일 순매수 top N strategies (D 선정 → D+1 시가매수 → D+H 종가매도)")
print(f"{'='*100}")
print(f"  {'전략':<35s} {'n':>5s} {'avg':>8s} {'total':>9s} {'win%':>5s} {'final':>8s} {'mdd':>9s}")

variants = [
    ("top5 hold1 (모든 종목)", {"top_k":5,"hold_days":1,"min_tr":1e9}),
    ("top5 hold1 (50억 필터)", {"top_k":5,"hold_days":1,"min_tr":5e9}),
    ("top10 hold1 (50억)", {"top_k":10,"hold_days":1,"min_tr":5e9}),
    ("top20 hold1 (50억)", {"top_k":20,"hold_days":1,"min_tr":5e9}),
    ("top5 hold2 (50억)", {"top_k":5,"hold_days":2,"min_tr":5e9}),
    ("top5 hold3 (50억)", {"top_k":5,"hold_days":3,"min_tr":5e9}),
    ("top5 hold5 (50억)", {"top_k":5,"hold_days":5,"min_tr":5e9}),
    ("top5 hold1 + 3일 streak", {"top_k":5,"hold_days":1,"min_tr":5e9,"streak":True}),
    ("top10 hold3 + 3일 streak", {"top_k":10,"hold_days":3,"min_tr":5e9,"streak":True}),
    ("top5 hold1 (100억 큰종목)", {"top_k":5,"hold_days":1,"min_tr":1e10}),
    ("top10 hold1 (100억)", {"top_k":10,"hold_days":1,"min_tr":1e10}),
]

for label, kw in variants:
    pnls, _, n = backtest(**kw)
    if not pnls:
        continue
    eq, mdd = equity(pnls)
    wins = sum(1 for r in pnls if r > 0)
    print(f"  {label:<35s} {len(pnls):>5d} {mean(pnls):>+7.3f}% {sum(pnls):>+8.2f}% "
          f"{wins*100//len(pnls):>4d}% {eq:>7.1f}  {mdd:>+7.2f}%")

# 비용 차감
print(f"\n[비용 -0.3% per trade 차감 후]")
for label, kw in variants:
    pnls, _, n = backtest(**kw)
    if not pnls:
        continue
    pnls_c = [r - 0.3 for r in pnls]
    eq, mdd = equity(pnls_c)
    wins = sum(1 for r in pnls_c if r > 0)
    print(f"  {label:<35s} avg={mean(pnls_c):>+7.3f}% total={sum(pnls_c):>+8.2f}% eq={eq:>7.1f} mdd={mdd:>+7.2f}%")

# 가장 유망한 거 detail
print(f"\n{'='*100}")
print("DETAIL — top10 hold3 + streak (외인 3일 연속 매수 강도순 10종목, 3일 보유)")
print(f"{'='*100}")
pnls, by_day, n = backtest(top_k=10, hold_days=3, streak=True, min_tr=5e9)
if pnls:
    eq, mdd = equity(pnls)
    wins = sum(1 for r in pnls if r > 0)
    print(f"  total trades: {n}")
    print(f"  avg: {mean(pnls):+.3f}%  median: {sorted(pnls)[len(pnls)//2]:+.3f}%")
    print(f"  best 5: {sorted(pnls, reverse=True)[:5]}")
    print(f"  worst 5: {sorted(pnls)[:5]}")
    print(f"  win rate: {wins*100//len(pnls)}%")
    print(f"  equity (1% per trade): {eq:.1f}, mdd {mdd:.2f}%")
    # 일별 분포
    daily_avg = sorted([(d, mean(rs)) for d, rs in by_day.items()])
    print(f"\n  worst 3 days: {[(d, f'{r:+.2f}%') for d, r in sorted(daily_avg, key=lambda x:x[1])[:3]]}")
    print(f"  best 3 days: {[(d, f'{r:+.2f}%') for d, r in sorted(daily_avg, key=lambda x:-x[1])[:3]]}")
