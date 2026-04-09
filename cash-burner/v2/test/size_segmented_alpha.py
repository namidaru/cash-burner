"""
거래대금 segment별 oc alpha 분해.

가설: 외인이 안 만지는 작은 종목은 정보 누수가 시가에 다 반영 안 됨
      -> oc alpha가 살아있을 수 있음

방법:
  매일 종목을 거래대금 quartile로 나눔 (Q1=작은~Q4=큰)
  각 quartile에서 외인 매수/매도 시그널의 oc 수익률 측정
  Q1 oc alpha vs Q4 oc alpha 비교
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


# 종목별 평균 거래대금 (전 기간)
avg_tr = {}
for sym in syms:
    if sym in LIKELY_ETFS: continue
    trs = [get(sym, d, "tr") for d in ohlc.get(sym, {}).keys()]
    trs = [t for t in trs if t > 0]
    if trs:
        avg_tr[sym] = mean(trs)

# quartile 분류
sorted_syms = sorted(avg_tr.items(), key=lambda x: x[1])
n = len(sorted_syms)
q1 = set(s for s, _ in sorted_syms[:n//4])
q2 = set(s for s, _ in sorted_syms[n//4:n//2])
q3 = set(s for s, _ in sorted_syms[n//2:3*n//4])
q4 = set(s for s, _ in sorted_syms[3*n//4:])
quartiles = [("Q1 작은", q1), ("Q2", q2), ("Q3", q3), ("Q4 큰", q4)]
print(f"종목 수: {n}, Q1~Q4: {len(q1)}/{len(q2)}/{len(q3)}/{len(q4)}")
print(f"평균 거래대금:")
for label, q in quartiles:
    avg_t = mean(avg_tr[s] for s in q)
    print(f"  {label}: {avg_t/1e8:.0f}억")


def analyze_quartile(q_set, label):
    """q_set 종목만 대상으로 외인 시그널 oc/cc alpha 측정"""
    long_oc = []; long_cc = []  # 외인 매수 top
    short_oc = []; short_cc = []  # 외인 매도 top
    base_oc = []; base_cc = []  # baseline (q_set 전체)

    for di in range(len(all_dates)-1):
        d = all_dates[di]; d_next = all_dates[di+1]
        # 일별 q_set 내 후보
        cands = []
        for sym in q_set:
            if d not in inv[sym]: continue
            if get(sym, d, "tr") < 1e9: continue
            avgv = avg_vol(sym, d, 20)
            if avgv <= 0: continue
            rec = inv[sym][d]
            cands.append((sym, rec["frgn_qty"]/avgv))
        if len(cands) < 6: continue
        cands.sort(key=lambda x: -x[1])
        k = max(3, len(cands)//5)
        longs = [s for s, _ in cands[:k]]
        shorts = [s for s, _ in cands[-k:]]

        # baseline
        for sym, _ in cands:
            r_oc = oc_ret(sym, d_next); r_cc = cc_ret(sym, d, d_next)
            if r_oc is not None: base_oc.append(r_oc)
            if r_cc is not None: base_cc.append(r_cc)
        for sym in longs:
            r_oc = oc_ret(sym, d_next); r_cc = cc_ret(sym, d, d_next)
            if r_oc is not None: long_oc.append(r_oc)
            if r_cc is not None: long_cc.append(r_cc)
        for sym in shorts:
            r_oc = oc_ret(sym, d_next); r_cc = cc_ret(sym, d, d_next)
            if r_oc is not None: short_oc.append(r_oc)
            if r_cc is not None: short_cc.append(r_cc)

    if not base_oc: return None
    return {
        "label": label, "n_obs": len(base_oc), "n_long": len(long_oc),
        "base_oc": mean(base_oc), "base_cc": mean(base_cc),
        "long_oc": mean(long_oc) if long_oc else 0,
        "long_cc": mean(long_cc) if long_cc else 0,
        "short_oc": mean(short_oc) if short_oc else 0,
        "short_cc": mean(short_cc) if short_cc else 0,
        "long_oc_alpha": (mean(long_oc) - mean(base_oc)) if long_oc else 0,
        "long_cc_alpha": (mean(long_cc) - mean(base_cc)) if long_cc else 0,
        "short_oc_alpha": (mean(short_oc) - mean(base_oc)) if short_oc else 0,
        "ls_oc_alpha": (mean(long_oc) - mean(short_oc)) if long_oc and short_oc else 0,
        "ls_cc_alpha": (mean(long_cc) - mean(short_cc)) if long_cc and short_cc else 0,
    }


print(f"\n{'='*100}")
print("거래대금 quartile별 외인 신호 alpha")
print(f"{'='*100}")
print(f"  {'segment':<10s} {'n_obs':>6s} {'base_oc':>9s} {'long_oc':>9s} {'short_oc':>9s} "
      f"{'long_a':>9s} {'L-S oc':>9s} {'L-S cc':>9s}")

for label, q in quartiles:
    r = analyze_quartile(q, label)
    if not r: continue
    print(f"  {r['label']:<10s} {r['n_obs']:>6d} "
          f"{r['base_oc']:>+8.3f}% {r['long_oc']:>+8.3f}% {r['short_oc']:>+8.3f}% "
          f"{r['long_oc_alpha']:>+8.3f}% {r['ls_oc_alpha']:>+8.3f}% {r['ls_cc_alpha']:>+8.3f}%")

print(f"\n해석:")
print(f"  Q1 (작은 종목)의 L-S oc alpha > Q4 -> 작은 종목에서 정보 누수 덜됨")
print(f"  Q1 long_oc_alpha > 0 -> 작은 종목 외인 매수는 진짜 잡힘")
print(f"  모든 quartile에서 L-S oc < 0 -> microstructure 장벽 segment-invariant")
