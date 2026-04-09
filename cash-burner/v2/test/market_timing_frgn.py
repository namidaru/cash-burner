"""
시장 타이밍 - 외인 net flow 기반 ETF 회전.

가설: 외인이 시장 전체에서 net 매도한 날 → 다음날 KOSPI 약세 → 인버스 ETF long
      외인이 net 매수한 날 → 다음날 강세 → 레버리지 long

이건 retail 100% 실현 가능 (공매도 없음, ETF만).

데이터:
  - investor_cache: 종목별 외인 수량 → 모든 종목 합산해서 시장 net flow 계산
  - 거래대금 가중 vs 단순 합계 둘 다 시험
  - target ETF: 122630 (레버리지), 114800 (인버스), 069500 (KODEX200)

전략:
  S1: 외인 net > 0 → 122630 long, 외인 net < 0 → 114800 long
  S2: 외인 net > 0 → 122630 long, < 0 → 현금
  S3: 외인 net 강도에 따라 비중 (-1~+1 → -100%~+100%)
  S4: 단순 069500 buy&hold (baseline)
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


def compute_market_flow():
    """일별 외인 net flow (시장 전체 합산)."""
    flow = {}  # date → {qty_sum, tr_sum, n_buy, n_sell, ratio}
    for d in all_dates:
        qty_sum = 0; tr_sum = 0
        n_buy = 0; n_sell = 0
        n = 0
        for sym in syms:
            if sym in LIKELY_ETFS: continue
            if d not in inv[sym]: continue
            if get(sym, d, "tr") < 5e9: continue
            rec = inv[sym][d]
            qty_sum += rec["frgn_qty"]
            tr_sum += rec["frgn_tr"]
            if rec["frgn_qty"] > 0: n_buy += 1
            elif rec["frgn_qty"] < 0: n_sell += 1
            n += 1
        flow[d] = {
            "qty_sum": qty_sum, "tr_sum": tr_sum,
            "n_buy": n_buy, "n_sell": n_sell, "n": n,
            "buy_ratio": n_buy/n if n else 0,
        }
    return flow


def oc_ret(sym, d):
    o = get(sym, d, "open"); c = get(sym, d, "close")
    if o <= 0 or c <= 0: return None
    return (c/o - 1) * 100


def cc_ret(sym, d_prev, d):
    p = get(sym, d_prev, "close"); c = get(sym, d, "close")
    if p <= 0 or c <= 0: return None
    return (c/p - 1) * 100


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


print("외인 net flow 시장 타이밍 봇")
print("="*100)

flow = compute_market_flow()
print(f"일별 flow 계산 완료: {len(flow)}일")

# flow 분포
qtys = [f["qty_sum"] for f in flow.values()]
trs = [f["tr_sum"] for f in flow.values()]
print(f"qty_sum: min={min(qtys):,.0f} max={max(qtys):,.0f} mean={mean(qtys):,.0f}")
print(f"tr_sum (원): min={min(trs):,.0f} max={max(trs):,.0f} mean={mean(trs):,.0f}")
n_pos_qty = sum(1 for q in qtys if q > 0)
n_pos_tr = sum(1 for t in trs if t > 0)
print(f"qty>0 일수: {n_pos_qty}/{len(qtys)}, tr>0 일수: {n_pos_tr}/{len(trs)}")


# Strategy backtest: D 종가 후 결정 → D+1 일중 운용
def run_strategy(decide_fn, label):
    """decide_fn(d, flow) → ('122630'|'114800'|'069500'|None)"""
    rs_oc = []  # D+1 시가→종가
    rs_cc = []  # D+1 close (D close→D+1 close)
    n_lev = 0; n_inv = 0; n_cash = 0
    for di in range(len(all_dates)-1):
        d = all_dates[di]; d_next = all_dates[di+1]
        choice = decide_fn(d, flow.get(d))
        if choice is None:
            n_cash += 1
            rs_oc.append(0); rs_cc.append(0)
            continue
        if choice == "122630": n_lev += 1
        elif choice == "114800": n_inv += 1
        r_oc = oc_ret(choice, d_next)
        r_cc = cc_ret(choice, d, d_next)
        rs_oc.append(r_oc if r_oc is not None else 0)
        rs_cc.append(r_cc if r_cc is not None else 0)
    eq_oc, mdd_oc = equity(rs_oc, 0.15)  # 0.15% per turn (ETF)
    eq_cc, mdd_cc = equity(rs_cc, 0.15)
    sh_oc = sharpe(rs_oc); sh_cc = sharpe(rs_cc)
    return {
        "label": label, "n": len(rs_oc),
        "lev": n_lev, "inv": n_inv, "cash": n_cash,
        "avg_oc": mean(rs_oc), "sh_oc": sh_oc, "eq_oc": eq_oc, "mdd_oc": mdd_oc,
        "avg_cc": mean(rs_cc), "sh_cc": sh_cc, "eq_cc": eq_cc, "mdd_cc": mdd_cc,
    }


def s1_qty(d, f):
    if not f: return None
    return "122630" if f["qty_sum"] > 0 else "114800"

def s1_tr(d, f):
    if not f: return None
    return "122630" if f["tr_sum"] > 0 else "114800"

def s2_qty(d, f):
    if not f: return None
    return "122630" if f["qty_sum"] > 0 else None  # 외인 매도 시 현금

def s2_tr_strong(d, f):
    if not f: return None
    if f["tr_sum"] > 1e10: return "122630"
    if f["tr_sum"] < -1e10: return "114800"
    return None

def s3_buy_ratio(d, f):
    if not f: return None
    return "122630" if f["buy_ratio"] > 0.5 else "114800"

def s4_baseline(d, f):
    return "069500"  # 항상 KOSPI200

def s5_baseline_lev(d, f):
    return "122630"  # 항상 레버리지

def s6_baseline_inv(d, f):
    return "114800"  # 항상 인버스


strategies = [
    ("S1 qty: long122630/short114800", s1_qty),
    ("S1 tr:  long122630/short114800", s1_tr),
    ("S2 qty: long122630 / 현금", s2_qty),
    ("S2 tr 강도: ±100억", s2_tr_strong),
    ("S3 buy_ratio>0.5", s3_buy_ratio),
    ("S4 069500 buy&hold", s4_baseline),
    ("S5 122630 buy&hold", s5_baseline_lev),
    ("S6 114800 buy&hold", s6_baseline_inv),
]

print(f"\n[OC 모드 — D+1 시가매수→종가매도]")
print(f"  {'전략':<32s} {'lev':>4s} {'inv':>4s} {'cash':>4s}  {'avg':>8s} {'sharpe':>7s} {'eq':>7s} {'mdd':>7s}")
results = []
for label, fn in strategies:
    r = run_strategy(fn, label)
    results.append(r)
    print(f"  {label:<32s} {r['lev']:>4d} {r['inv']:>4d} {r['cash']:>4d}  "
          f"{r['avg_oc']:>+7.3f}% {r['sh_oc']:>+6.2f} {r['eq_oc']:>6.1f} {r['mdd_oc']:>+6.2f}%")

print(f"\n[CC 모드 — D close→D+1 close (overnight 포함, 이론치)]")
print(f"  {'전략':<32s}  {'avg':>8s} {'sharpe':>7s} {'eq':>7s} {'mdd':>7s}")
for r in results:
    print(f"  {r['label']:<32s}  {r['avg_cc']:>+7.3f}% {r['sh_cc']:>+6.2f} {r['eq_cc']:>6.1f} {r['mdd_cc']:>+6.2f}%")

# 외인 net 부호와 다음날 시장 수익률 상관
print(f"\n{'='*100}")
print("외인 net flow → 다음날 시장 수익률 상관")
print(f"{'='*100}")
xs = []; ys_oc = []; ys_cc = []
for di in range(len(all_dates)-1):
    d = all_dates[di]; d_next = all_dates[di+1]
    f = flow.get(d)
    if not f: continue
    r_oc = oc_ret("069500", d_next)
    r_cc = cc_ret("069500", d, d_next)
    if r_oc is None or r_cc is None: continue
    xs.append(f["tr_sum"])
    ys_oc.append(r_oc)
    ys_cc.append(r_cc)

def pearson(xs, ys):
    n = len(xs)
    mx = mean(xs); my = mean(ys)
    num = sum((xs[i]-mx)*(ys[i]-my) for i in range(n))
    sx = math.sqrt(sum((xs[i]-mx)**2 for i in range(n)))
    sy = math.sqrt(sum((ys[i]-my)**2 for i in range(n)))
    return num/(sx*sy) if sx > 0 and sy > 0 else 0

print(f"  외인 tr_sum vs 069500 oc 다음날: r = {pearson(xs, ys_oc):+.3f}")
print(f"  외인 tr_sum vs 069500 cc 다음날: r = {pearson(xs, ys_cc):+.3f}")

# bucketed
print(f"\n  외인 tr_sum 부호별 다음날 069500:")
pos = [(xs[i], ys_oc[i], ys_cc[i]) for i in range(len(xs)) if xs[i] > 0]
neg = [(xs[i], ys_oc[i], ys_cc[i]) for i in range(len(xs)) if xs[i] < 0]
if pos:
    print(f"    외인 매수 일 ({len(pos)}일): oc avg={mean(p[1] for p in pos):+.3f}%  cc avg={mean(p[2] for p in pos):+.3f}%")
if neg:
    print(f"    외인 매도 일 ({len(neg)}일): oc avg={mean(p[1] for p in neg):+.3f}%  cc avg={mean(p[2] for p in neg):+.3f}%")

# 122630/114800 분석도
for tgt in ["122630", "114800"]:
    print(f"\n  외인 tr_sum 부호별 다음날 {tgt}:")
    pos_r = []; neg_r = []
    for di in range(len(all_dates)-1):
        d = all_dates[di]; d_next = all_dates[di+1]
        f = flow.get(d)
        if not f: continue
        r_oc = oc_ret(tgt, d_next)
        if r_oc is None: continue
        if f["tr_sum"] > 0: pos_r.append(r_oc)
        elif f["tr_sum"] < 0: neg_r.append(r_oc)
    if pos_r:
        print(f"    외인 매수일 ({len(pos_r)}일): {tgt} oc avg={mean(pos_r):+.3f}%")
    if neg_r:
        print(f"    외인 매도일 ({len(neg_r)}일): {tgt} oc avg={mean(neg_r):+.3f}%")
