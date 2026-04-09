"""
Contrarian intraday strategy — IC -0.16의 정반대.

intraday_ic.py 결과:
  mom5 → fwd5: IC=-0.1454 (t=-99)
  range_pos → fwd5: IC=-0.1630 (t=-171)
  → 5분봉 가장 많이 빠진 종목이 다음 5분에 mean revert

전략:
  매 N분마다 cross-section (모든 종목 동시)
  - mom5 (또는 range_pos) 가장 음수인 종목 K개 long
  - hold M분 후 청산 (다음 cross section 또는 fixed)
  - 등가중

비용:
  - 종목당 매수+매도 = 0.3% (수수료+세금+slippage)
  - 매우 빈번한 회전이므로 비용 민감

시험 변수:
  - feature: mom5, mom10, range_pos
  - K: 5, 10, 20
  - hold: 5, 15, 30분
  - sample 시간: 매 5분 / 15분 / 30분
"""
import os, csv, math
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

DIR = "data/volume_profiles"
syms = sorted([f[:-4] for f in os.listdir(DIR) if f.endswith(".csv")])
print(f"syms: {len(syms)}")


def load_sym(sym):
    rows = []
    try:
        with open(f"{DIR}/{sym}.csv", "r") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({
                        "date": r["date"],
                        "time": r["time"],
                        "o": int(r["open"]), "h": int(r["high"]),
                        "l": int(r["low"]), "c": int(r["close"]),
                        "v": int(r["volume"]),
                    })
                except: continue
    except: return []
    return rows


def by_date(rows):
    out = defaultdict(list)
    for r in rows: out[r["date"]].append(r)
    for d in out: out[d].sort(key=lambda x: x["time"])
    return out


print("loading all syms...")
all_data = {}
for i, sym in enumerate(syms):
    rows = load_sym(sym)
    if not rows: continue
    all_data[sym] = by_date(rows)
    if (i+1) % 100 == 0:
        print(f"  {i+1}/{len(syms)}")

all_dates = sorted(set(d for v in all_data.values() for d in v))
print(f"dates: {len(all_dates)}")


def run_strategy(feature, K, hold_min, sample_step, exclude_first=30, exclude_last=30, cost=0.30):
    """
    매 sample_step 분마다 cross-section에서 feature 가장 음수인 K개 long.
    hold_min 분 후 청산. 모든 거래 P&L 등가중 평균.
    """
    all_trades = []  # (date, time_idx, sym, ret_pct)

    for date in all_dates:
        # 해당 일에 데이터 있는 종목들
        day_bars = {}
        for sym, dd in all_data.items():
            bars = dd.get(date)
            if bars and len(bars) >= 90:
                day_bars[sym] = bars

        if len(day_bars) < K * 3: continue

        # 모든 종목이 같은 분봉 시간 격자라고 가정 (1분 = 1 index)
        # 길이 가장 짧은 거 기준
        n_min = min(len(b) for b in day_bars.values())

        # sample 시점들
        for t in range(exclude_first, n_min - hold_min - exclude_last, sample_step):
            cands = []
            for sym, bars in day_bars.items():
                if t < 10: continue
                c_now = bars[t]["c"]
                c_5 = bars[t-5]["c"]
                if c_now <= 0 or c_5 <= 0: continue
                if feature == "mom5":
                    val = (c_now/c_5 - 1) * 100
                elif feature == "mom10":
                    if t < 10: continue
                    c_10 = bars[t-10]["c"]
                    if c_10 <= 0: continue
                    val = (c_now/c_10 - 1) * 100
                elif feature == "range_pos":
                    h = bars[t]["h"]; l = bars[t]["l"]
                    val = (c_now - l)/(h - l) if h > l else 0.5
                else:
                    continue
                cands.append((sym, val, c_now))
            if len(cands) < K * 3: continue
            # 가장 음수인 K개
            cands.sort(key=lambda x: x[1])
            picks = cands[:K]
            for sym, _, entry in picks:
                bars = day_bars[sym]
                if t + hold_min >= len(bars): continue
                exit_p = bars[t + hold_min]["c"]
                if exit_p <= 0: continue
                r = (exit_p/entry - 1) * 100
                all_trades.append((date, t, sym, r))

    if not all_trades: return None
    rs = [r for _,_,_,r in all_trades]
    rs_c = [r - cost for r in rs]
    n = len(rs)
    avg = mean(rs); avg_c = mean(rs_c)
    var = sum((x-avg_c)**2 for x in rs_c)/(n-1) if n > 1 else 0
    sd = math.sqrt(var)
    sharpe_per_trade = avg_c/sd if sd > 0 else 0
    wins_c = sum(1 for r in rs_c if r > 0)
    # daily aggregation
    by_day = defaultdict(list)
    for d,_,_,r in all_trades:
        by_day[d].append(r - cost)
    daily_pnls = [sum(rs_d)/K for rs_d in by_day.values()]  # K종목 등가중이면 일평균 = sum/K? 아니다, 각 거래가 1종목 1단위라 평균
    # 실제로는 매 시점 K종목 동시 보유 → 자본분할 → 일평균 = mean of trade rets
    daily_avg = [mean(rs_d) for rs_d in by_day.values()]
    n_days = len(daily_avg)
    daily_sharpe = (mean(daily_avg)/(math.sqrt(sum((x-mean(daily_avg))**2 for x in daily_avg)/(n_days-1))))*math.sqrt(252) if n_days > 1 else 0
    eq = 100.0; peak = 100.0; mdd = 0.0
    for r in daily_avg:
        eq *= (1 + r/100)
        if eq > peak: peak = eq
        dd = (eq/peak-1)*100
        if dd < mdd: mdd = dd
    return {
        "n": n, "n_days": n_days,
        "trades_per_day": n/n_days if n_days else 0,
        "avg": avg, "avg_c": avg_c,
        "win_pct": wins_c*100/n,
        "daily_avg": mean(daily_avg),
        "daily_sharpe": daily_sharpe,
        "eq": eq, "mdd": mdd,
    }


print(f"\n{'='*100}")
print("Contrarian intraday — 분봉 mean reversion (가장 빠진 종목 long)")
print("  비용: 0.30%/trade (전략 회전 빠르므로 비용 critical)")
print(f"{'='*100}")
print(f"  {'feature':<10s} {'K':>3s} {'hold':>5s} {'step':>5s} {'n':>6s} {'/day':>5s} "
      f"{'avg':>9s} {'avg-c':>9s} {'win%':>5s} {'sharpe':>7s} {'eq':>7s} {'mdd':>7s}")

variants = []
for feat in ["mom5", "mom10", "range_pos"]:
    for K in [5, 10, 20]:
        for hold in [5, 15, 30]:
            for step in [hold]:  # 동일하게
                variants.append((feat, K, hold, step))

for feat, K, hold, step in variants:
    r = run_strategy(feat, K, hold, step)
    if not r:
        print(f"  {feat:<10s} {K:>3d} {hold:>5d} {step:>5d}  데이터부족")
        continue
    print(f"  {feat:<10s} {K:>3d} {hold:>5d} {step:>5d} {r['n']:>6d} {r['trades_per_day']:>4.0f}  "
          f"{r['avg']:>+7.3f}% {r['avg_c']:>+7.3f}% {r['win_pct']:>4.1f}% "
          f"{r['daily_sharpe']:>+6.2f} {r['eq']:>6.1f} {r['mdd']:>+6.2f}%")

print(f"\n해석:")
print("  avg_c > 0.05% per trade → 비용 후 진짜 alpha")
print("  daily_sharpe > 1.0 → 운용 가능")
print("  단 거래수/일이 너무 많으면 (>50) execution risk + 호가 slippage 누락")
