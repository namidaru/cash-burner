"""
외인 매수 신호의 진짜 alpha가 어디에 있는지 분해.

각 trade를 3구간으로 쪼갬:
  1. overnight: D close → D+1 open  (gap)
  2. intraday: D+1 open → D+1 close
  3. total: D close → D+1 close

만약 alpha가 모두 overnight에 있으면 → 우리(retail)는 못 잡음 (open 진입 시 이미 premium)
intraday에 있으면 → 잡을 수 있음
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


def select_frgn(d, top_k=5, min_tr=5e9):
    cands = []
    for sym in syms:
        if sym in LIKELY_ETFS:
            continue
        if d not in inv[sym]: continue
        tr = get(sym, d, "tr")
        if tr < min_tr: continue
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0: continue
        frgn = inv[sym][d]["frgn_qty"]
        if frgn <= 0: continue
        cands.append((sym, frgn/avgv))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:top_k]]


def backtest_decompose(top_k=5):
    overnight = []  # close → next open
    intraday = []   # next open → next close
    total_cc = []   # close → next close
    full_o2c = []   # next open → next close (real-tradable)
    by_decile = defaultdict(lambda: {"o":[],"i":[],"t":[]})

    for di in range(len(all_dates) - 1):
        d = all_dates[di]
        d_next = all_dates[di+1]
        picks = select_frgn(d, top_k=top_k)
        for sym in picks:
            c0 = get(sym, d, "close")
            o1 = get(sym, d_next, "open")
            c1 = get(sym, d_next, "close")
            if c0 <= 0 or o1 <= 0 or c1 <= 0:
                continue
            r_on = (o1/c0 - 1)*100
            r_id = (c1/o1 - 1)*100
            r_cc = (c1/c0 - 1)*100
            overnight.append(r_on)
            intraday.append(r_id)
            total_cc.append(r_cc)

    # 모든 종목 baseline (선정 안한 비교군)
    overnight_all = []
    intraday_all = []
    total_cc_all = []
    for di in range(len(all_dates) - 1):
        d = all_dates[di]; d_next = all_dates[di+1]
        for sym in syms:
            if sym in LIKELY_ETFS: continue
            if d not in inv[sym]: continue
            if get(sym, d, "tr") < 5e9: continue
            c0 = get(sym, d, "close")
            o1 = get(sym, d_next, "open")
            c1 = get(sym, d_next, "close")
            if c0 <= 0 or o1 <= 0 or c1 <= 0: continue
            overnight_all.append((o1/c0-1)*100)
            intraday_all.append((c1/o1-1)*100)
            total_cc_all.append((c1/c0-1)*100)

    return overnight, intraday, total_cc, overnight_all, intraday_all, total_cc_all


print(f"외인 시그널 alpha 분해 (universe vs 외인 top N)")
print(f"{'='*100}")
print(f"  {'top_k':<8s} {'segment':<15s} {'top_avg':>10s} {'all_avg':>10s} {'alpha':>10s} {'top_n':>6s}")
print(f"{'='*100}")
for top_k in [5, 10, 20]:
    on, id_, cc, on_a, id_a, cc_a = backtest_decompose(top_k)
    if not on:
        continue
    print(f"  top{top_k:<5d} overnight       {mean(on):>+8.3f}%  {mean(on_a):>+8.3f}%  {mean(on)-mean(on_a):>+8.3f}%  {len(on):>5d}")
    print(f"  top{top_k:<5d} intraday(o→c)   {mean(id_):>+8.3f}%  {mean(id_a):>+8.3f}%  {mean(id_)-mean(id_a):>+8.3f}%  {len(id_):>5d}")
    print(f"  top{top_k:<5d} total c→c       {mean(cc):>+8.3f}%  {mean(cc_a):>+8.3f}%  {mean(cc)-mean(cc_a):>+8.3f}%  {len(cc):>5d}")
    print()

print(f"\n해석:")
print(f"  overnight alpha > 0 & intraday alpha ≈ 0  → 신호는 진짜지만 시가에서 다 반영, 매수 불가")
print(f"  intraday alpha > 0  → 진짜 잡을 수 있는 alpha")
print(f"  total alpha > 0이지만 overnight + intraday로 분리되면 → overnight 부분만 가짜 alpha")
