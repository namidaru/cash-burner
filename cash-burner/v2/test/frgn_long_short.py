"""
외인 시그널 long-short 시장중립 시뮬레이션.

가설: cc alpha (close-to-close, baseline 차감)가 진짜라면
long-short으로 잡을 수 있어야 한다. 시장 베타 hedge하면 alpha만 남음.

구조:
  매일 D 종가 후
    Long  bucket = 외인 norm 상위 N
    Short bucket = 외인 norm 하위 N (외인 매도 강한)
  포지션: D+1 시가 진입, D+1 종가 청산 (혹은 close→close로도 시험)

비교:
  - cc 모드 (D close → D+1 close): 이론치, overnight 포함
  - oc 모드 (D+1 open → D+1 close): 실전, retail tradable
  - 비용: long 0.3% + short 0.3% = 0.6% per round trip
"""
import os, json
from collections import defaultdict
from statistics import mean
import math

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


def candidates(d):
    out = {}
    for sym in syms:
        if sym in LIKELY_ETFS: continue
        if d not in inv[sym]: continue
        if get(sym, d, "tr") < 5e9: continue
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0: continue
        c = get(sym, d, "close")
        if c <= 0: continue
        rec = inv[sym][d]
        out[sym] = rec["frgn_qty"] / avgv
    return out


def long_short_day(d, top_k, mode="cc"):
    """반환: (long_avg_ret, short_avg_ret, ls_ret)"""
    di = all_dates.index(d)
    if di + 1 >= len(all_dates): return None
    d_next = all_dates[di+1]
    cands = candidates(d)
    if len(cands) < top_k * 2 + 2: return None
    sorted_syms = sorted(cands.items(), key=lambda x: -x[1])
    longs = [s for s, _ in sorted_syms[:top_k]]
    shorts = [s for s, _ in sorted_syms[-top_k:]]

    def ret(sym):
        if mode == "cc":
            c0 = get(sym, d, "close")
            c1 = get(sym, d_next, "close")
            if c0 <= 0 or c1 <= 0: return None
            return (c1/c0 - 1) * 100
        elif mode == "oc":
            o1 = get(sym, d_next, "open")
            c1 = get(sym, d_next, "close")
            if o1 <= 0 or c1 <= 0: return None
            return (c1/o1 - 1) * 100

    lr = [ret(s) for s in longs]; lr = [r for r in lr if r is not None]
    sr = [ret(s) for s in shorts]; sr = [r for r in sr if r is not None]
    if not lr or not sr: return None
    long_avg = mean(lr); short_avg = mean(sr)
    return long_avg, short_avg, long_avg - short_avg


def equity_curve(daily_returns, cost_per_day=0.0):
    eq = 100.0; peak = 100.0; mdd = 0.0; curve = [100.0]
    for r in daily_returns:
        eq *= (1 + (r - cost_per_day)/100)
        if eq > peak: peak = eq
        dd = (eq/peak-1)*100
        if dd < mdd: mdd = dd
        curve.append(eq)
    return eq, mdd, curve


def sharpe(rs):
    if len(rs) < 2: return 0
    m = mean(rs)
    var = sum((x-m)**2 for x in rs) / (len(rs)-1)
    s = math.sqrt(var)
    if s == 0: return 0
    return (m / s) * math.sqrt(252)


def run(top_k, mode):
    rows = []
    for d in all_dates:
        r = long_short_day(d, top_k, mode)
        if r is None: continue
        rows.append((d, r[0], r[1], r[2]))
    if not rows: return None
    long_rs = [r[1] for r in rows]
    short_rs = [r[2] for r in rows]
    ls_rs = [r[3] for r in rows]
    n = len(rows)
    long_eq, long_mdd, _ = equity_curve(long_rs)
    short_eq, short_mdd, _ = equity_curve([-r for r in short_rs])  # 숏은 반대
    # ls: long 100% + short -100%, 비용 0.6%/day (round trip)
    ls_eq_raw, ls_mdd_raw, _ = equity_curve(ls_rs, 0.0)
    ls_eq_c, ls_mdd_c, _ = equity_curve(ls_rs, 0.6)
    win_days = sum(1 for r in ls_rs if r > 0)
    return {
        "n": n,
        "long_avg": mean(long_rs), "long_sharpe": sharpe(long_rs),
        "long_eq": long_eq, "long_mdd": long_mdd,
        "short_only_avg": -mean(short_rs), "short_only_sharpe": sharpe([-r for r in short_rs]),
        "short_only_eq": short_eq, "short_only_mdd": short_mdd,
        "ls_avg": mean(ls_rs), "ls_sharpe": sharpe(ls_rs),
        "ls_eq_raw": ls_eq_raw, "ls_mdd_raw": ls_mdd_raw,
        "ls_eq_c": ls_eq_c, "ls_mdd_c": ls_mdd_c,
        "ls_win_days": win_days*100/n,
        "best_day": max(ls_rs), "worst_day": min(ls_rs),
    }


print("="*100)
print("외인 시그널 long-short 시장중립 백테스트")
print("  long: 외인 norm 상위 K, short: 하위 K (외인 매도)")
print("  비용: 0.6% per day (long 0.3 + short 0.3)")
print("="*100)

for mode_label, mode in [("cc (close→close, overnight 포함)", "cc"),
                          ("oc (open→close, retail 실전)", "oc")]:
    print(f"\n[{mode_label}]")
    print(f"  {'top_k':<7s} {'days':>5s} {'long%':>8s} {'short_only':>10s} {'L-S avg':>8s} "
          f"{'L-S sharpe':>10s} {'L-S eq raw':>10s} {'L-S eq -c':>9s} {'mdd_c':>7s} {'win일%':>7s}")
    for k in [3, 5, 10, 20]:
        r = run(k, mode)
        if not r:
            print(f"  top{k:<5d}  데이터 부족")
            continue
        print(f"  top{k:<5d} {r['n']:>5d} "
              f"{r['long_avg']:>+7.3f}% {r['short_only_avg']:>+9.3f}% "
              f"{r['ls_avg']:>+7.3f}% {r['ls_sharpe']:>+9.2f}  "
              f"{r['ls_eq_raw']:>9.1f} {r['ls_eq_c']:>8.1f} "
              f"{r['ls_mdd_c']:>+6.2f}% {r['ls_win_days']:>6.1f}%")

# Detail: top5 cc + oc
print(f"\n{'='*100}")
print("DETAIL — top5 long-short")
print(f"{'='*100}")
for mode_label, mode in [("cc", "cc"), ("oc", "oc")]:
    r = run(5, mode)
    if not r: continue
    print(f"\n[{mode_label}] n={r['n']}일")
    print(f"  long avg: {r['long_avg']:+.3f}%/day  sharpe={r['long_sharpe']:+.2f}  eq={r['long_eq']:.1f}  mdd={r['long_mdd']:+.2f}%")
    print(f"  short_only(반대): {r['short_only_avg']:+.3f}%/day  sharpe={r['short_only_sharpe']:+.2f}")
    print(f"  L-S spread: {r['ls_avg']:+.3f}%/day  sharpe={r['ls_sharpe']:+.2f}")
    print(f"  L-S eq raw: {r['ls_eq_raw']:.1f}  비용 후: {r['ls_eq_c']:.1f}  mdd_c={r['ls_mdd_c']:+.2f}%")
    print(f"  L-S 일 승률: {r['ls_win_days']:.1f}%  best/worst day: {r['best_day']:+.2f}%/{r['worst_day']:+.2f}%")

# 가장 중요: 외인 매수 vs 외인 매도가 진짜 다른가?
print(f"\n{'='*100}")
print("결론 가이드")
print(f"{'='*100}")
print("  L-S sharpe > 1.0 (after cost) → market-neutral 봇 가능")
print("  L-S sharpe 0~1.0 → marginal, MDD 보고 결정")
print("  L-S sharpe < 0 → 시그널 완전 fake, alpha 사망")
