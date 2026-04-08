"""
Overnight strategies breakdown — verify if results are real or outlier-driven.
"""
import os, sys, json
from collections import defaultdict, Counter

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, "src")

CACHE_PATH = "data/daily_ohlc_cache.json"
with open(CACHE_PATH, "r", encoding="utf-8") as f:
    cache = json.load(f)

# build day_data
day_data = defaultdict(dict)
sym_dates = {}
for sym, days in cache.items():
    if not days:
        continue
    sd = sorted(days.keys())
    sym_dates[sym] = sd
    for i, d in enumerate(sd):
        row = dict(days[d])
        row["prev_close"] = days[sd[i-1]]["close"] if i > 0 else 0
        day_data[d][sym] = row

all_dates = sorted(day_data.keys())


def get_next(sym, d):
    sd = sym_dates.get(sym, [])
    if d not in sd:
        return None
    i = sd.index(d)
    return sd[i+1] if i + 1 < len(sd) else None


def overnight_ret(sym, d):
    nd = get_next(sym, d)
    if not nd:
        return None
    td = day_data[d].get(sym, {})
    nd_data = day_data[nd].get(sym, {})
    if not td or not nd_data or td["close"] <= 0 or nd_data["open"] <= 0:
        return None
    return (nd_data["open"] / td["close"] - 1) * 100


# selection rules
def strat_strong_close(d, n=5):
    cands = []
    for sym, r in day_data[d].items():
        if r["high"] <= r["low"] or r["tr"] < 1e9:
            continue
        pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
        if pos >= 0.85:
            cands.append((sym, r["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


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


def strat_strong_high_tr(d, n=5):
    cands = []
    for sym, r in day_data[d].items():
        if r["high"] <= r["low"] or r["tr"] < 5e9:
            continue
        pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
        if pos >= 0.9:
            chg = (r["close"] / r["prev_close"] - 1) * 100 if r["prev_close"] > 0 else 0
            if chg > 1:
                cands.append((sym, r["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


def collect(strat_fn):
    trades = []  # (date, sym, prev_close, close, next_open, gap%)
    for d in all_dates:
        for sym in strat_fn(d):
            nd = get_next(sym, d)
            if not nd:
                continue
            td = day_data[d][sym]
            ndd = day_data[nd][sym]
            if td["close"] <= 0 or ndd["open"] <= 0:
                continue
            gap = (ndd["open"] / td["close"] - 1) * 100
            trades.append((d, sym, td["close"], ndd["open"], gap))
    return trades


def report(name, trades, cost_pct=0.30, gap_clip=15.0):
    print(f"\n{'='*100}")
    print(f"{name}")
    print(f"{'='*100}")
    if not trades:
        print("no trades")
        return
    pnls = [t[4] for t in trades]
    print(f"\n[원본]")
    print(f"  n={len(pnls)}  win={sum(1 for p in pnls if p>0)*100//len(pnls)}%  "
          f"avg={sum(pnls)/len(pnls):+.2f}%  total={sum(pnls):+.2f}%")
    print(f"  best={max(pnls):+.2f}%  worst={min(pnls):+.2f}%  "
          f"median={sorted(pnls)[len(pnls)//2]:+.2f}%")

    # outlier dependence
    sorted_p = sorted(pnls, reverse=True)
    print(f"\n[Outlier 의존도]")
    print(f"  top 5 합: {sum(sorted_p[:5]):+.2f}% ({sum(sorted_p[:5])/sum(pnls)*100:.0f}% 기여)")
    print(f"  top 10 합: {sum(sorted_p[:10]):+.2f}% ({sum(sorted_p[:10])/sum(pnls)*100:.0f}% 기여)")
    print(f"  top 5 빼면: {sum(pnls)-sum(sorted_p[:5]):+.2f}%  (남은 평균 {(sum(pnls)-sum(sorted_p[:5]))/(len(pnls)-5):+.2f}%)")
    print(f"  top 10 빼면: {sum(pnls)-sum(sorted_p[:10]):+.2f}%")

    # 큰 갭 (권리락/상한가 의심)
    big = [t for t in trades if abs(t[4]) >= gap_clip]
    print(f"\n[극단 갭 ±{gap_clip}% 이상] {len(big)}건")
    for t in sorted(big, key=lambda x: -abs(x[4]))[:10]:
        print(f"  {t[0]} {t[1]} close={t[2]:,} → open={t[3]:,} ({t[4]:+.1f}%)")

    # 갭 클리핑 후 (extreme outlier 제거)
    filtered = [p for p in pnls if abs(p) < gap_clip]
    if filtered:
        print(f"\n[극단 ±{gap_clip}% 제외] n={len(filtered)}")
        print(f"  win={sum(1 for p in filtered if p>0)*100//len(filtered)}%  "
              f"avg={sum(filtered)/len(filtered):+.2f}%  total={sum(filtered):+.2f}%")

    # 거래비용 차감
    after_cost = [p - cost_pct for p in pnls]
    after_cost_filt = [p - cost_pct for p in filtered]
    print(f"\n[거래비용 -{cost_pct}% 차감 (수수료+세금+슬리피지)]")
    print(f"  원본:  n={len(after_cost)}  win={sum(1 for p in after_cost if p>0)*100//len(after_cost)}%  "
          f"avg={sum(after_cost)/len(after_cost):+.2f}%  total={sum(after_cost):+.2f}%")
    if after_cost_filt:
        print(f"  필터: n={len(after_cost_filt)}  win={sum(1 for p in after_cost_filt if p>0)*100//len(after_cost_filt)}%  "
              f"avg={sum(after_cost_filt)/len(after_cost_filt):+.2f}%  total={sum(after_cost_filt):+.2f}%")

    # 일별 분포
    by_day = defaultdict(list)
    for t in trades:
        by_day[t[0]].append(t[4])
    day_totals = sorted([(d, sum(p), len(p)) for d, p in by_day.items()], key=lambda x: x[1])
    pos_days = sum(1 for _, t, _ in day_totals if t > 0)
    neg_days = sum(1 for _, t, _ in day_totals if t < 0)
    print(f"\n[일별 분포] +일={pos_days}  -일={neg_days}  총={len(day_totals)}")
    print(f"  best 3 days: {[(d, f'{t:+.1f}%') for d, t, _ in day_totals[-3:][::-1]]}")
    print(f"  worst 3 days: {[(d, f'{t:+.1f}%') for d, t, _ in day_totals[:3]]}")


# run
trades_o1 = collect(strat_strong_close)
report("O1: 강세마감 (close ≥ 0.85)", trades_o1)

trades_o4 = collect(strat_top_gainer)
report("O4: 어제 상승률 상위 5", trades_o4)

trades_o6 = collect(strat_strong_high_tr)
report("O6: 강세마감 + 거래대금 50억+ + 어제대비 +1%", trades_o6)

# 비상한가 + 강세 + 큰종목 — 상한가 outlier 제거 의도
def strat_clean(d, n=5):
    cands = []
    for sym, r in day_data[d].items():
        if r["high"] <= r["low"] or r["tr"] < 5e9 or r["prev_close"] <= 0:
            continue
        chg = (r["close"] / r["prev_close"] - 1) * 100
        if chg > 15 or chg < -15:  # 상한가/하한가 제외
            continue
        pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
        if pos >= 0.85 and chg > 0.5:
            cands.append((sym, r["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


trades_clean = collect(strat_clean)
report("CLEAN: 강세마감 + 거래대금 50억+ + ±15% 이내 + 어제대비 +0.5%", trades_clean)
