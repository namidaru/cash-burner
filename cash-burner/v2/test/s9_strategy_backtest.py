"""
S9 (외인+ ∩ 개인- ∩ ret<0) 실전 거래 시뮬레이션.

가설: 외인이 사고 개인이 팔고 가격이 떨어지는 종목 = smart accumulation
combined_flow_signals.py: cc_alpha +0.93%, after_cost +0.63%

확인할 것:
  1. open→close (실현가능, overnight gap 제외) 단독 수익률
  2. top_k 변화 (3/5/10)
  3. hold 1/2/3일
  4. 비용 차감 후 누적 equity
  5. 일별/월별 분포
  6. 최대 손실 / MDD / 승률
  7. baseline (외인 단독 S1) 대비 우위
"""
import os, json
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/investor_cache.json", "r", encoding="utf-8") as f:
    inv = json.load(f)
with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    ohlc = json.load(f)

syms = sorted(set(inv.keys()) & set(ohlc.keys()))
LIKELY_ETFS = {"069500","122630","252670","233740","379800","114800","229200","251340","153130","132030","278530","294400"}
all_dates = sorted(set(d for s in syms for d in inv[s]))


def get(sym, d, k):
    return ohlc.get(sym, {}).get(d, {}).get(k, 0)


def avg_vol(sym, d, n=20):
    days = sorted(ohlc.get(sym, {}).keys())
    if d not in days: return 0
    i = days.index(d)
    if i < n: return 0
    return mean(ohlc[sym][days[j]].get("vol", 0) for j in range(i-n, i))


def candidates_for_day(d):
    out = {}
    for sym in syms:
        if sym in LIKELY_ETFS: continue
        if d not in inv[sym]: continue
        if get(sym, d, "tr") < 5e9: continue
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0: continue
        c = get(sym, d, "close")
        di = all_dates.index(d) if d in all_dates else -1
        if di < 1: continue
        prev_c = get(sym, all_dates[di-1], "close")
        if c <= 0 or prev_c <= 0: continue
        rec = inv[sym][d]
        out[sym] = {
            "frgn_norm": rec["frgn_qty"] / avgv,
            "prsn_norm": rec["prsn_qty"] / avgv,
            "ret_1d": (c/prev_c - 1) * 100,
        }
    return out


def select_s9(d, top_k=5):
    cands = candidates_for_day(d)
    filtered = [(s, f["frgn_norm"]) for s, f in cands.items()
                if f["frgn_norm"] > 0 and f["prsn_norm"] < 0 and f["ret_1d"] < 0]
    filtered.sort(key=lambda x: -x[1])
    return [s for s, _ in filtered[:top_k]]


def select_s1(d, top_k=5):
    cands = candidates_for_day(d)
    filtered = [(s, f["frgn_norm"]) for s, f in cands.items() if f["frgn_norm"] > 0]
    filtered.sort(key=lambda x: -x[1])
    return [s for s, _ in filtered[:top_k]]


def backtest(selector, top_k, hold_days, exit_mode="close"):
    """D 종가 후 선정 → D+1 시가 매수 → D+hold 종가 매도 (open→close)"""
    trades = []  # (date, sym, ret_pct)
    by_day = defaultdict(list)
    for di in range(len(all_dates) - hold_days):
        d = all_dates[di]
        d_entry = all_dates[di+1]
        d_exit = all_dates[di + hold_days]
        picks = selector(d, top_k=top_k)
        for sym in picks:
            entry = get(sym, d_entry, "open")
            exit_p = get(sym, d_exit, "close")
            if entry <= 0 or exit_p <= 0:
                continue
            r = (exit_p/entry - 1) * 100
            trades.append((d_entry, sym, r))
            by_day[d_entry].append(r)
    return trades, by_day


def stats(trades, cost_per_trade=0.3):
    if not trades:
        return None
    rs = [r for _, _, r in trades]
    rs_c = [r - cost_per_trade for r in rs]
    n = len(rs)
    avg = mean(rs)
    avg_c = mean(rs_c)
    wins = sum(1 for r in rs if r > 0)
    wins_c = sum(1 for r in rs_c if r > 0)
    # equity (1% notional per trade, 누적 곱)
    eq = 100.0; peak = 100.0; mdd = 0.0
    for r in rs_c:
        eq *= (1 + r/100)
        if eq > peak: peak = eq
        dd = (eq/peak - 1) * 100
        if dd < mdd: mdd = dd
    # daily aggregation
    by_day = defaultdict(list)
    for d, _, r in trades:
        by_day[d].append(r)
    daily = [mean(rs_d) for rs_d in by_day.values()]
    daily_c = [mean(rs_d) - cost_per_trade for rs_d in by_day.values()]
    n_days = len(daily)
    win_days = sum(1 for r in daily_c if r > 0)
    return {
        "n": n, "n_days": n_days,
        "avg": avg, "avg_c": avg_c,
        "win_pct": wins*100/n,
        "win_pct_c": wins_c*100/n,
        "win_days_pct": win_days*100/n_days if n_days else 0,
        "eq": eq, "mdd": mdd,
        "best": max(rs), "worst": min(rs),
        "median": sorted(rs)[n//2],
    }


print("="*100)
print("S9 (외인+ ∩ 개인- ∩ ret<0) 실전 시뮬레이션")
print(f"  데이터: {all_dates[0]}~{all_dates[-1]} ({len(all_dates)}일)")
print("  진입: D+1 시가 매수 (overnight alpha 제외, retail 실현가능)")
print("  청산: D+hold 종가, 비용 0.3%/round trip")
print("="*100)

variants = [
    ("S9 top3 hold1", select_s9, 3, 1),
    ("S9 top5 hold1", select_s9, 5, 1),
    ("S9 top10 hold1", select_s9, 10, 1),
    ("S9 top5 hold2", select_s9, 5, 2),
    ("S9 top5 hold3", select_s9, 5, 3),
    ("S9 top5 hold5", select_s9, 5, 5),
    ("S1 top5 hold1 (대조)", select_s1, 5, 1),
    ("S1 top10 hold1 (대조)", select_s1, 10, 1),
]

print(f"\n  {'변형':<24s} {'n':>5s} {'days':>5s} {'avg':>8s} {'avg-c':>8s} {'win%':>6s} {'win일%':>7s} {'eq':>7s} {'mdd':>7s}")
for label, sel, tk, hd in variants:
    trades, _ = backtest(sel, tk, hd)
    s = stats(trades)
    if not s:
        print(f"  {label:<24s}  데이터 부족")
        continue
    print(f"  {label:<24s} {s['n']:>5d} {s['n_days']:>5d} "
          f"{s['avg']:>+7.3f}% {s['avg_c']:>+7.3f}% "
          f"{s['win_pct']:>5.1f}% {s['win_days_pct']:>6.1f}% "
          f"{s['eq']:>6.1f} {s['mdd']:>+6.2f}%")

# Detail S9 top5 hold1
print(f"\n{'='*100}")
print("DETAIL — S9 top5 hold1 (실전 후보)")
print(f"{'='*100}")
trades, by_day = backtest(select_s9, 5, 1)
s = stats(trades)
if s:
    print(f"  총 거래: {s['n']} (일평균 {s['n']/s['n_days']:.1f}건, {s['n_days']}일 활동)")
    print(f"  raw avg: {s['avg']:+.3f}%/trade   비용후: {s['avg_c']:+.3f}%/trade")
    print(f"  median: {s['median']:+.3f}%   best: {s['best']:+.2f}%   worst: {s['worst']:+.2f}%")
    print(f"  win rate (trade): {s['win_pct']:.1f}%   win rate (비용후): {s['win_pct_c']:.1f}%")
    print(f"  win rate (일별 평균>0): {s['win_days_pct']:.1f}%")
    print(f"  equity (1% per trade compound, 비용후): {s['eq']:.1f} (시작 100)")
    print(f"  MDD: {s['mdd']:+.2f}%")

    # 일별 best/worst
    daily_avg = sorted([(d, mean(rs)) for d, rs in by_day.items()], key=lambda x: x[1])
    print(f"\n  worst 5 days: {[(d, f'{r:+.2f}%') for d,r in daily_avg[:5]]}")
    print(f"  best 5 days: {[(d, f'{r:+.2f}%') for d,r in daily_avg[-5:]]}")

    # 분포
    rs = [r for _,_,r in trades]
    buckets = [-100, -5, -3, -1, 0, 1, 3, 5, 100]
    labels = ["<-5%", "-5~-3", "-3~-1", "-1~0", "0~1", "1~3", "3~5", ">5%"]
    counts = [0]*8
    for r in rs:
        for i in range(8):
            if buckets[i] <= r < buckets[i+1]:
                counts[i] += 1
                break
    print(f"\n  수익 분포:")
    for lbl, c in zip(labels, counts):
        bar = "█" * (c * 40 // max(counts))
        print(f"    {lbl:>8s}: {c:>4d} {bar}")

# 누적 equity 시계열
print(f"\n  누적 equity 곡선 (월별 마일스톤):")
trades_sorted = sorted(trades, key=lambda x: x[0])
eq = 100.0
peak = 100.0
mdd = 0.0
month_eq = {}
for d, sym, r in trades_sorted:
    eq *= (1 + (r - 0.3)/100)
    if eq > peak: peak = eq
    dd = (eq/peak-1)*100
    if dd < mdd: mdd = dd
    month_eq[d[:6]] = eq
for m in sorted(month_eq.keys()):
    print(f"    {m}: eq={month_eq[m]:>6.2f}")
print(f"  최종: eq={eq:.2f}, mdd={mdd:+.2f}%")
