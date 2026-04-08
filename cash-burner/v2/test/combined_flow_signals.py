"""
결합 시그널 탐색.

가설:
1. 외인 매수 + 개인 매도 (반대) 동시 → conviction이 더 강함, alpha 더 큼
2. 외인 매수 + 거래량 spike → real conviction (cf. quiet accumulation)
3. 외인 매수 + 가격 상승 (확정 추세) → 추격
4. 외인 매수 + 가격 하락 (역방향 매수) → smart money는 떨어질 때 산다는 통념

각각의 alpha (overnight + intraday + total) 측정.
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
    """모든 적격 종목과 feature 반환"""
    out = {}
    for sym in syms:
        if sym in LIKELY_ETFS: continue
        if d not in inv[sym]: continue
        if get(sym, d, "tr") < 5e9: continue
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0: continue
        c = get(sym, d, "close")
        prev_d_idx = all_dates.index(d) - 1 if d in all_dates else -1
        if prev_d_idx < 0: continue
        prev_c = get(sym, all_dates[prev_d_idx], "close")
        if c <= 0 or prev_c <= 0: continue
        v = get(sym, d, "vol")
        rec = inv[sym][d]
        out[sym] = {
            "frgn_norm": rec["frgn_qty"] / avgv,
            "orgn_norm": rec["orgn_qty"] / avgv,
            "prsn_norm": rec["prsn_qty"] / avgv,
            "vol_ratio": v / avgv,
            "ret_1d": (c/prev_c - 1)*100,
            "close": c,
        }
    return out


def alpha_for_picks(picks_per_day):
    """picks_per_day: {date: [syms]}, return overnight/intraday/cc alphas vs baseline"""
    on, id_, cc = [], [], []
    on_b, id_b, cc_b = [], [], []
    for di in range(len(all_dates) - 1):
        d = all_dates[di]
        d_next = all_dates[di+1]
        picks = picks_per_day.get(d, [])
        if not picks:
            continue
        cands = candidates_for_day(d)
        for sym in cands:
            c0 = get(sym, d, "close")
            o1 = get(sym, d_next, "open")
            c1 = get(sym, d_next, "close")
            if c0 <= 0 or o1 <= 0 or c1 <= 0: continue
            r_on = (o1/c0-1)*100; r_id = (c1/o1-1)*100; r_cc = (c1/c0-1)*100
            if sym in picks:
                on.append(r_on); id_.append(r_id); cc.append(r_cc)
            on_b.append(r_on); id_b.append(r_id); cc_b.append(r_cc)
    if not on:
        return None
    return {
        "n": len(on),
        "on_alpha": mean(on) - mean(on_b),
        "id_alpha": mean(id_) - mean(id_b),
        "cc_alpha": mean(cc) - mean(cc_b),
        "on_top": mean(on),
        "id_top": mean(id_),
        "cc_top": mean(cc),
    }


# Strategies (each picks 5 syms per day)
def make_picks(filter_fn, sort_fn, top_k=5):
    picks = {}
    for d in all_dates:
        cands = candidates_for_day(d)
        filtered = [(s, f) for s, f in cands.items() if filter_fn(f)]
        filtered.sort(key=lambda x: sort_fn(x[1]))
        picks[d] = [s for s, _ in filtered[:top_k]]
    return picks


STRATS = [
    # 단일
    ("S1: 외인 norm top5", lambda f: f["frgn_norm"] > 0,
     lambda f: -f["frgn_norm"]),
    # 결합 1: 외인 매수 + 개인 매도
    ("S2: 외인+ ∩ 개인-", lambda f: f["frgn_norm"] > 0 and f["prsn_norm"] < 0,
     lambda f: -f["frgn_norm"]),
    # 결합 2: 외인 매수 + 거래량 spike
    ("S3: 외인+ ∩ vol>1.5x", lambda f: f["frgn_norm"] > 0 and f["vol_ratio"] > 1.5,
     lambda f: -f["frgn_norm"]),
    # 결합 3: 외인 매수 + 가격 상승 (확정 추세)
    ("S4: 외인+ ∩ ret>0", lambda f: f["frgn_norm"] > 0 and f["ret_1d"] > 0,
     lambda f: -f["frgn_norm"]),
    # 결합 4: 외인 매수 + 가격 하락 (역방향)
    ("S5: 외인+ ∩ ret<0", lambda f: f["frgn_norm"] > 0 and f["ret_1d"] < 0,
     lambda f: -f["frgn_norm"]),
    # 결합 5: 외인 매수 + 기관 매수 (양쪽 합의)
    ("S6: 외인+ ∩ 기관+", lambda f: f["frgn_norm"] > 0 and f["orgn_norm"] > 0,
     lambda f: -(f["frgn_norm"] + f["orgn_norm"])),
    # 결합 6: triple — 외인+ 개인- 기관+
    ("S7: 외인+ 개인- 기관+", lambda f: f["frgn_norm"] > 0 and f["prsn_norm"] < 0 and f["orgn_norm"] > 0,
     lambda f: -(f["frgn_norm"] + f["orgn_norm"])),
    # 결합 7: 외인+ 개인- + 거래량
    ("S8: 외인+ 개인- vol>1.5", lambda f: f["frgn_norm"] > 0 and f["prsn_norm"] < 0 and f["vol_ratio"] > 1.5,
     lambda f: -f["frgn_norm"]),
    # 결합 8: 외인+ 개인- 가격하락 (smart accumulation)
    ("S9: 외인+ 개인- ret<0", lambda f: f["frgn_norm"] > 0 and f["prsn_norm"] < 0 and f["ret_1d"] < 0,
     lambda f: -f["frgn_norm"]),
]

print(f"{'='*100}")
print(f"결합 시그널 alpha 분해 (top5, baseline = 모든 적격 종목)")
print(f"{'='*100}")
print(f"  {'전략':<28s} {'n':>5s} {'on_alpha':>10s} {'id_alpha':>10s} {'cc_alpha':>10s} {'cc_top':>9s}")
for name, fl, sk in STRATS:
    picks = make_picks(fl, sk, top_k=5)
    n_picks_total = sum(len(p) for p in picks.values())
    if n_picks_total < 30:
        print(f"  {name:<28s}  너무 적음 (n={n_picks_total})")
        continue
    res = alpha_for_picks(picks)
    if not res:
        continue
    print(f"  {name:<28s} {res['n']:>5d} "
          f"{res['on_alpha']:>+8.3f}%  {res['id_alpha']:>+8.3f}%  {res['cc_alpha']:>+8.3f}%  {res['cc_top']:>+7.3f}%")

# net of cost
print(f"\n[비용 0.3% per round trip 차감 후 (close to close)]")
for name, fl, sk in STRATS:
    picks = make_picks(fl, sk, top_k=5)
    n_picks_total = sum(len(p) for p in picks.values())
    if n_picks_total < 30:
        continue
    res = alpha_for_picks(picks)
    if not res:
        continue
    after_cost = res["cc_alpha"] - 0.30
    flag = " ★" if after_cost > 0.10 else (" ✓" if after_cost > 0 else "")
    print(f"  {name:<28s}  cc_alpha={res['cc_alpha']:+.3f}%  after_cost={after_cost:+.3f}%{flag}")
