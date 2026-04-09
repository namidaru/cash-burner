"""
Contrarian intraday robustness check.

contrarian_intraday.py에서 mom10 K=5 hold=15 sharpe +3.98 발견.
이게 진짜인지 4가지 보정으로 검증:
  1) lag 1분 (t close 시점 측정 → t+1 open 매수 → t+16 close 청산)
  2) 비용 0.5% 보수
  3) 거래대금 50억+ universe 필터 (호가 두께)
  4) 종목별/일별 worst trade 분석
"""
import os, csv, math
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

DIR = "data/volume_profiles"
syms = sorted([f[:-4] for f in os.listdir(DIR) if f.endswith(".csv")])

# 일일 거래대금 평균 (universe 필터)
print("loading...")
all_data = {}
day_tr = {}
for i, sym in enumerate(syms):
    rows = []
    try:
        with open(f"{DIR}/{sym}.csv", "r") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({
                        "date": r["date"], "time": r["time"],
                        "o": int(r["open"]), "h": int(r["high"]),
                        "l": int(r["low"]), "c": int(r["close"]),
                        "v": int(r["volume"]),
                        "tr": int(r.get("tr_pbmn", 0)),
                    })
                except: continue
    except: continue
    if not rows: continue
    by_d = defaultdict(list)
    for r in rows: by_d[r["date"]].append(r)
    for d in by_d: by_d[d].sort(key=lambda x: x["time"])
    all_data[sym] = by_d
    # 일별 거래대금 합
    tr_per_day = {d: sum(r["tr"] for r in bars) for d, bars in by_d.items()}
    if tr_per_day:
        day_tr[sym] = mean(tr_per_day.values())

all_dates = sorted(set(d for v in all_data.values() for d in v))
print(f"syms={len(all_data)} dates={len(all_dates)}")


def run(K, hold, step, lag=1, cost=0.5, min_tr=5e9, exclude_first=30, exclude_last=30):
    """mom10 contrarian, lag보정, universe 필터"""
    trades = []  # (date, t, sym, entry, exit, ret)
    for date in all_dates:
        # 그 날 거래대금 50억+ 종목만
        day_bars = {}
        for sym, dd in all_data.items():
            bars = dd.get(date)
            if not bars or len(bars) < 90: continue
            tr_today = sum(r["tr"] for r in bars)
            if tr_today < min_tr: continue
            day_bars[sym] = bars
        if len(day_bars) < K * 3: continue
        n_min = min(len(b) for b in day_bars.values())
        for t in range(exclude_first, n_min - hold - lag - exclude_last, step):
            cands = []
            for sym, bars in day_bars.items():
                if t < 10: continue
                c_now = bars[t]["c"]
                c_10 = bars[t-10]["c"]
                if c_now <= 0 or c_10 <= 0: continue
                mom = (c_now/c_10 - 1) * 100
                cands.append((sym, mom))
            if len(cands) < K * 3: continue
            cands.sort(key=lambda x: x[1])  # 가장 음수 (가장 많이 빠짐)
            picks = cands[:K]
            for sym, _ in picks:
                bars = day_bars[sym]
                # lag: t+lag bar의 open으로 진입 (실제로는 1분 후 시가)
                if t + lag >= len(bars): continue
                entry = bars[t + lag]["o"]
                if entry <= 0: continue
                exit_idx = t + lag + hold
                if exit_idx >= len(bars): continue
                exit_p = bars[exit_idx]["c"]
                if exit_p <= 0: continue
                r = (exit_p/entry - 1) * 100
                trades.append((date, t, sym, entry, exit_p, r))
    if not trades: return None
    rs = [tr[5] for tr in trades]
    rs_c = [r - cost for r in rs]
    n = len(rs)
    avg = mean(rs); avg_c = mean(rs_c)
    wins = sum(1 for r in rs_c if r > 0)
    by_d = defaultdict(list)
    for tr in trades:
        by_d[tr[0]].append(tr[5] - cost)
    daily_avg = [mean(rs_d) for rs_d in by_d.values()]
    nd = len(daily_avg)
    if nd > 1:
        m = mean(daily_avg)
        sd = math.sqrt(sum((x-m)**2 for x in daily_avg)/(nd-1))
        sharpe = (m/sd)*math.sqrt(252) if sd > 0 else 0
    else:
        sharpe = 0
    eq = 100.0; peak = 100.0; mdd = 0.0
    for r in daily_avg:
        eq *= (1 + r/100)
        if eq > peak: peak = eq
        dd = (eq/peak-1)*100
        if dd < mdd: mdd = dd
    win_days = sum(1 for r in daily_avg if r > 0)
    return {
        "trades": trades, "n": n, "n_days": nd,
        "avg": avg, "avg_c": avg_c, "win_pct": wins*100/n,
        "win_days_pct": win_days*100/nd,
        "daily_avg": mean(daily_avg), "sharpe": sharpe,
        "eq": eq, "mdd": mdd,
        "best": max(rs), "worst": min(rs),
    }


# Variant matrix
print(f"\n{'='*100}")
print("mom10 contrarian — 4가지 보정 (lag1 / cost0.5 / 거래대금필터)")
print(f"{'='*100}")
print(f"  {'시나리오':<30s} {'n':>5s} {'days':>5s} {'/일':>4s} {'avg':>8s} "
      f"{'avg-c':>8s} {'win%':>5s} {'일승률':>6s} {'sh':>6s} {'eq':>6s} {'mdd':>7s}")

scenarios = [
    ("baseline (cost0.3 lag0 min0)",   {"K":5, "hold":15, "step":15, "lag":0, "cost":0.3, "min_tr":0}),
    ("+ lag1",                          {"K":5, "hold":15, "step":15, "lag":1, "cost":0.3, "min_tr":0}),
    ("+ cost0.5",                       {"K":5, "hold":15, "step":15, "lag":1, "cost":0.5, "min_tr":0}),
    ("+ tr 50억",                        {"K":5, "hold":15, "step":15, "lag":1, "cost":0.5, "min_tr":5e9}),
    ("+ tr 100억",                       {"K":5, "hold":15, "step":15, "lag":1, "cost":0.5, "min_tr":1e10}),
    ("K=10 hold=15 모든 보정",            {"K":10, "hold":15, "step":15, "lag":1, "cost":0.5, "min_tr":5e9}),
    ("K=3 hold=15 모든 보정",             {"K":3, "hold":15, "step":15, "lag":1, "cost":0.5, "min_tr":5e9}),
    ("K=5 hold=10 모든 보정",             {"K":5, "hold":10, "step":10, "lag":1, "cost":0.5, "min_tr":5e9}),
    ("K=5 hold=20 모든 보정",             {"K":5, "hold":20, "step":20, "lag":1, "cost":0.5, "min_tr":5e9}),
    ("K=5 hold=30 모든 보정",             {"K":5, "hold":30, "step":30, "lag":1, "cost":0.5, "min_tr":5e9}),
]

best_robust = None
for label, kw in scenarios:
    r = run(**kw)
    if not r:
        print(f"  {label:<30s}  데이터 부족")
        continue
    print(f"  {label:<30s} {r['n']:>5d} {r['n_days']:>5d} "
          f"{r['n']/r['n_days']:>3.0f}  {r['avg']:>+7.3f}% {r['avg_c']:>+7.3f}% "
          f"{r['win_pct']:>4.1f}% {r['win_days_pct']:>5.1f}% "
          f"{r['sharpe']:>+5.2f} {r['eq']:>5.1f} {r['mdd']:>+6.2f}%")
    if "모든 보정" in label and "K=5 hold=15" not in label:
        pass
    if label == "+ tr 50억":
        best_robust = r

# Detail of best surviving
print(f"\n{'='*100}")
print("DETAIL — K=5 hold=15 lag1 cost0.5 tr50억")
print(f"{'='*100}")
if best_robust:
    r = best_robust
    print(f"  총 거래: {r['n']} (일평균 {r['n']/r['n_days']:.1f}건, {r['n_days']}일)")
    print(f"  raw avg: {r['avg']:+.3f}%/trade")
    print(f"  비용후: {r['avg_c']:+.3f}%/trade  win {r['win_pct']:.1f}%")
    print(f"  best/worst trade: {r['best']:+.2f}% / {r['worst']:+.2f}%")
    print(f"  daily avg: {r['daily_avg']:+.3f}%  sharpe {r['sharpe']:+.2f}")
    print(f"  일승률: {r['win_days_pct']:.1f}%  eq {r['eq']:.1f}  mdd {r['mdd']:+.2f}%")

    # 분포
    rs = [tr[5] for tr in r["trades"]]
    buckets = [-100,-5,-3,-1,0,1,3,5,100]
    labels = ["<-5%","-5~-3","-3~-1","-1~0","0~1","1~3","3~5",">5%"]
    counts = [0]*8
    for x in rs:
        for i in range(8):
            if buckets[i] <= x < buckets[i+1]:
                counts[i] += 1; break
    print(f"\n  수익 분포 (raw, 비용 차감 전):")
    mx = max(counts) or 1
    for lbl, c in zip(labels, counts):
        bar = "█" * (c * 30 // mx)
        print(f"    {lbl:>8s}: {c:>4d} {bar}")

    # 최악/최선 일별
    by_d = defaultdict(list)
    for tr in r["trades"]:
        by_d[tr[0]].append(tr[5] - 0.5)
    daily = sorted([(d, mean(rs_d), len(rs_d)) for d, rs_d in by_d.items()], key=lambda x: x[1])
    print(f"\n  worst 5 days: {[(d, f'{a:+.2f}% n={n}') for d,a,n in daily[:5]]}")
    print(f"  best 5 days: {[(d, f'{a:+.2f}% n={n}') for d,a,n in daily[-5:]]}")

    # 최악 trade 5개
    worst_trades = sorted(r["trades"], key=lambda x: x[5])[:10]
    print(f"\n  worst 10 trades:")
    for date, t, sym, e, x, ret in worst_trades:
        print(f"    {date} t={t} {sym} {e}→{x} ({ret:+.2f}%)")
