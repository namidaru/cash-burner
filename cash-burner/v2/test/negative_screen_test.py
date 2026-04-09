"""
Negative screen 가설 검증.

frgn_long_short.py에서 발견:
  - 외인 매도 종목 short alpha sharpe +3.19 (oc 기준)
  - 매도 정보는 시가에서도 반영 안 됨

가설: 모든 종목을 universe로 매수할 때, "외인 매도 강한 종목"을 제외하면
      평균 다음날 oc 수익률이 개선되어야 한다.

방법:
  1) baseline: 거래대금 5억+ 모든 종목, D+1 시가매수→종가매도
  2) screen_p10: 외인 norm 하위 10% 제외
  3) screen_p20: 하위 20% 제외
  4) screen_neg: frgn_norm < 0인 모든 종목 제외 (외인 순매도 종목 제외)
  5) screen_strong_neg: frgn_norm < -0.5 (강한 매도)만 제외

비교:
  - 평균 oc 수익률 (절대치, baseline 차감 아님)
  - 승률, MDD, sharpe
  - 그리고 외인 매수 신호 결합과의 시너지
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
    """모든 적격 종목 + frgn_norm 반환"""
    out = []
    for sym in syms:
        if sym in LIKELY_ETFS: continue
        if d not in inv[sym]: continue
        if get(sym, d, "tr") < 5e9: continue
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0: continue
        rec = inv[sym][d]
        out.append((sym, rec["frgn_qty"] / avgv))
    return out


def oc_ret(sym, d_next):
    o = get(sym, d_next, "open")
    c = get(sym, d_next, "close")
    if o <= 0 or c <= 0: return None
    return (c/o - 1) * 100


def run_screen(name, screen_fn):
    """screen_fn: list of (sym, frgn_norm) → filtered list"""
    daily_avg = []  # daily 평균 수익률
    n_total = 0
    for di in range(len(all_dates) - 1):
        d = all_dates[di]; d_next = all_dates[di+1]
        cands = candidates(d)
        if not cands: continue
        kept = screen_fn(cands)
        rs = []
        for sym, _ in kept:
            r = oc_ret(sym, d_next)
            if r is not None: rs.append(r)
        if rs:
            daily_avg.append((d_next, mean(rs), len(rs)))
            n_total += len(rs)
    if not daily_avg:
        return None
    avgs = [a for _, a, _ in daily_avg]
    n_days = len(avgs)
    m = mean(avgs)
    var = sum((x-m)**2 for x in avgs) / max(n_days-1, 1)
    sd = math.sqrt(var)
    sharpe = (m / sd * math.sqrt(252)) if sd > 0 else 0
    eq = 100.0; peak = 100.0; mdd = 0.0
    for a in avgs:
        eq *= (1 + (a - 0.3)/100)
        if eq > peak: peak = eq
        dd = (eq/peak-1)*100
        if dd < mdd: mdd = dd
    win_days = sum(1 for a in avgs if a > 0)
    avg_kept = n_total / n_days
    return {
        "name": name, "n_days": n_days, "avg_kept": avg_kept,
        "daily_avg": m, "sharpe": sharpe,
        "win_days": win_days*100/n_days, "eq_c": eq, "mdd_c": mdd,
    }


print("="*100)
print("Negative Screen 검증 — 외인 매도 종목 제외 효과")
print("  baseline: 모든 적격 종목 D+1 시가매수→종가매도 (등가중)")
print("  비용 0.3%/round trip")
print("="*100)

screens = [
    ("baseline (전체)", lambda c: c),
    ("frgn_norm > 0 (외인 매수만)", lambda c: [x for x in c if x[1] > 0]),
    ("frgn_norm > -0.1 (약매도까지)", lambda c: [x for x in c if x[1] > -0.1]),
    ("frgn_norm > -0.5 (강매도 제외)", lambda c: [x for x in c if x[1] > -0.5]),
    ("frgn_norm > -1.0 (초강매도 제외)", lambda c: [x for x in c if x[1] > -1.0]),
    ("하위 20% 제외", lambda c: sorted(c, key=lambda x: -x[1])[:int(len(c)*0.8)]),
    ("하위 10% 제외", lambda c: sorted(c, key=lambda x: -x[1])[:int(len(c)*0.9)]),
    ("하위 5% 제외", lambda c: sorted(c, key=lambda x: -x[1])[:int(len(c)*0.95)]),
    ("상위 50% (외인 매수 강도순)", lambda c: sorted(c, key=lambda x: -x[1])[:int(len(c)*0.5)]),
]

print(f"\n  {'screen':<32s} {'days':>5s} {'kept/일':>8s} {'avg':>9s} {'sharpe':>8s} {'win일%':>7s} {'eq후':>7s} {'mdd':>7s}")
results = []
for name, fn in screens:
    r = run_screen(name, fn)
    if not r: continue
    results.append(r)
    print(f"  {r['name']:<32s} {r['n_days']:>5d} {r['avg_kept']:>7.0f}  "
          f"{r['daily_avg']:>+7.3f}% {r['sharpe']:>+7.2f} "
          f"{r['win_days']:>6.1f}% {r['eq_c']:>6.1f} {r['mdd_c']:>+6.2f}%")

# 개선 효과 (baseline 대비)
print(f"\n{'='*100}")
print("baseline 대비 개선 효과")
print(f"{'='*100}")
base = results[0]
print(f"  baseline: {base['daily_avg']:+.3f}%/day, sharpe={base['sharpe']:+.2f}, eq={base['eq_c']:.1f}")
for r in results[1:]:
    delta_avg = r['daily_avg'] - base['daily_avg']
    delta_sharpe = r['sharpe'] - base['sharpe']
    delta_eq = r['eq_c'] - base['eq_c']
    flag = " ★" if delta_avg > 0.05 and delta_sharpe > 0.5 else (" ✓" if delta_avg > 0 else "")
    print(f"  {r['name']:<32s}  Δavg={delta_avg:+.3f}%  Δsharpe={delta_sharpe:+.2f}  Δeq={delta_eq:+.1f}{flag}")
