"""
Q4 (대형주) 외인 매수 long + 인버스 ETF hedge.

발견: Q4 universe에서 외인 long 종목 oc alpha +0.086%, L-S +0.19%
가설: 시장 약세는 인버스 ETF로 hedge → 순수 alpha만 남음

구조:
  매일 D 종가 후
    Long: Q4 종목 중 외인 norm top K (등가중)
    Short equivalent: 114800 (인버스 ETF) 동일 금액
  → market-neutral, retail 100% feasible

비교:
  - Long-only (Q4 외인 top)
  - Long Q4 + 인버스 hedge (50%/100%)
  - 122630 buy&hold
  - 069500 buy&hold
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


def oc_ret(sym, d):
    o = get(sym, d, "open"); c = get(sym, d, "close")
    if o <= 0 or c <= 0: return None
    return (c/o - 1) * 100


def cc_ret(sym, d_prev, d):
    p = get(sym, d_prev, "close"); c = get(sym, d, "close")
    if p <= 0 or c <= 0: return None
    return (c/p - 1) * 100


# Q4 종목 (상위 25% 거래대금)
avg_tr = {}
for sym in syms:
    if sym in LIKELY_ETFS: continue
    trs = [get(sym, d, "tr") for d in ohlc.get(sym, {}).keys()]
    trs = [t for t in trs if t > 0]
    if trs: avg_tr[sym] = mean(trs)
sorted_syms = sorted(avg_tr.items(), key=lambda x: -x[1])
Q4 = set(s for s, _ in sorted_syms[:len(sorted_syms)//4])
print(f"Q4: {len(Q4)} 종목 (대형주)")


def select_q4_long(d, top_k):
    cands = []
    for sym in Q4:
        if d not in inv[sym]: continue
        if get(sym, d, "tr") < 1e10: continue  # 100억+
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0: continue
        rec = inv[sym][d]
        norm = rec["frgn_qty"] / avgv
        if norm <= 0: continue
        cands.append((sym, norm))
    cands.sort(key=lambda x: -x[1])
    return [s for s, _ in cands[:top_k]]


def sharpe(rs):
    if len(rs) < 2: return 0
    m = mean(rs); var = sum((x-m)**2 for x in rs)/(len(rs)-1)
    s = math.sqrt(var)
    return (m/s)*math.sqrt(252) if s > 0 else 0


def equity(rs, cost=0.0):
    eq = 100.0; peak = 100.0; mdd = 0.0
    for r in rs:
        eq *= (1 + (r-cost)/100)
        if eq > peak: peak = eq
        dd = (eq/peak-1)*100
        if dd < mdd: mdd = dd
    return eq, mdd


def run(top_k, hedge_ratio, mode="oc"):
    """매일 long Q4 top_k + 인버스 ETF hedge_ratio (0=long-only, 1=fully hedged)"""
    daily = []
    for di in range(len(all_dates)-1):
        d = all_dates[di]; d_next = all_dates[di+1]
        longs = select_q4_long(d, top_k)
        if not longs: continue
        long_rs = []
        for sym in longs:
            if mode == "oc":
                r = oc_ret(sym, d_next)
            else:
                r = cc_ret(sym, d, d_next)
            if r is not None: long_rs.append(r)
        if not long_rs: continue
        long_avg = mean(long_rs)
        # 인버스 hedge
        if hedge_ratio > 0:
            if mode == "oc":
                inv_r = oc_ret("114800", d_next)
            else:
                inv_r = cc_ret("114800", d, d_next)
            if inv_r is None: inv_r = 0
        else:
            inv_r = 0
        # net daily return
        # long 100% + inverse hedge_ratio
        net = long_avg + hedge_ratio * inv_r
        # 비용: long 회전 0.3%/day + 인버스 hedge_ratio * 0.15%/day
        cost = 0.3 + hedge_ratio * 0.15
        daily.append((d_next, net, cost))
    rs_raw = [r for _, r, _ in daily]
    rs_net = [r - c for _, r, c in daily]
    eq_raw, mdd_raw = equity(rs_raw)
    eq_net, mdd_net = equity(rs_net)
    return {
        "n": len(daily),
        "avg_raw": mean(rs_raw), "sh_raw": sharpe(rs_raw),
        "avg_net": mean(rs_net), "sh_net": sharpe(rs_net),
        "eq_raw": eq_raw, "eq_net": eq_net,
        "mdd_raw": mdd_raw, "mdd_net": mdd_net,
        "win_pct": sum(1 for r in rs_net if r > 0)*100/len(rs_net),
    }


print(f"\n{'='*100}")
print("Q4 외인 long + 인버스 ETF hedge")
print(f"{'='*100}")
print(f"  {'mode':<6s} {'long_k':>6s} {'hedge':>6s} {'n':>4s} "
      f"{'avg_raw':>9s} {'avg_net':>9s} {'sh_net':>7s} {'eq_net':>7s} {'mdd':>7s} {'win%':>6s}")

for mode in ["oc", "cc"]:
    for top_k in [3, 5, 10]:
        for hedge in [0.0, 0.5, 1.0]:
            r = run(top_k, hedge, mode)
            print(f"  {mode:<6s} top{top_k:<3d} {hedge:>5.1f}  {r['n']:>4d}  "
                  f"{r['avg_raw']:>+7.3f}% {r['avg_net']:>+7.3f}% {r['sh_net']:>+6.2f} "
                  f"{r['eq_net']:>6.1f} {r['mdd_net']:>+6.2f}% {r['win_pct']:>5.1f}%")
    print()

# baselines
print(f"baselines (oc):")
for tgt in ["069500", "122630", "114800"]:
    rs = []
    for di in range(len(all_dates)-1):
        d_next = all_dates[di+1]
        r = oc_ret(tgt, d_next)
        if r is not None: rs.append(r)
    if rs:
        eq, mdd = equity(rs)
        print(f"  {tgt} oc buy&hold: avg={mean(rs):+.3f}% sh={sharpe(rs):+.2f} eq={eq:.1f} mdd={mdd:+.2f}%")
