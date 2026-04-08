"""
strong_close 5-day swing — deep verification.

Checks:
  1. Outlier dependence (top trades contribution)
  2. Per-day breakdown + drawdown
  3. Cost impact (-0.3% per round trip)
  4. Stock diversity (which stocks get picked)
  5. Exit variants (close-to-close vs SL vs trail vs hybrid)
  6. Pseudo survivorship test: drop top-30 syms by total contribution, retest
"""
import os, sys, json
from collections import defaultdict, Counter
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)

day_data = defaultdict(dict)
sym_dates = {}
for sym, days in cache.items():
    if not days:
        continue
    sd = sorted(days.keys())
    sym_dates[sym] = sd
    for i, d in enumerate(sd):
        row = dict(days[d])
        row["prev_close"] = days[sd[i-1]]["close"] if i > 0 else 0
        day_data[d][sym] = row

all_dates = sorted(day_data.keys())
KOSPI_SYM = "069500"
kospi_close = {d: cache[KOSPI_SYM][d]["close"] for d in sym_dates.get(KOSPI_SYM, [])}
sorted_kdates = sorted(kospi_close.keys())
kospi_ma5 = {}
for i, d in enumerate(sorted_kdates):
    if i >= 4:
        kospi_ma5[d] = mean(kospi_close[sorted_kdates[j]] for j in range(i-4, i+1))


def is_bull(d):
    return d in kospi_ma5 and kospi_close[d] > kospi_ma5[d]


def select_strong_close(d, n=5, exclude=None):
    cands = []
    for sym, r in day_data[d].items():
        if exclude and sym in exclude:
            continue
        if r["high"] <= r["low"] or r["tr"] < 5e9:
            continue
        pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
        if pos >= 0.85:
            cands.append((sym, r["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


def get_close_after(sym, d, n_days):
    sd = sym_dates.get(sym, [])
    if d not in sd:
        return None, None
    i = sd.index(d)
    if i + n_days >= len(sd):
        return None, None
    target = sd[i + n_days]
    return target, day_data[target][sym]["close"]


def trade(sym, entry_d, hold=5, sl_pct=None):
    """close-to-close. optional intraday SL using high/low."""
    entry_close = day_data[entry_d].get(sym, {}).get("close", 0)
    if entry_close <= 0:
        return None, None, "no_entry"
    sd = sym_dates.get(sym, [])
    if entry_d not in sd:
        return None, None, "no_entry"
    i = sd.index(entry_d)
    for k in range(1, hold + 1):
        if i + k >= len(sd):
            return None, None, "no_data"
        d = sd[i + k]
        bar = day_data[d][sym]
        if sl_pct is not None:
            sl_price = entry_close * (1 + sl_pct / 100)
            if bar["low"] <= sl_price:
                return sl_pct, d, "sl"
    target = sd[i + hold]
    exit_close = day_data[target][sym]["close"]
    if exit_close <= 0:
        return None, None, "bad_exit"
    return (exit_close / entry_close - 1) * 100, target, "hold"


def kospi_ret(d_start, d_end):
    if d_start not in kospi_close or d_end not in kospi_close:
        return None
    return (kospi_close[d_end] / kospi_close[d_start] - 1) * 100


def collect(hold=5, bull_only=True, sl_pct=None, exclude=None):
    trades = []
    for d in all_dates:
        if bull_only and not is_bull(d):
            continue
        picks = select_strong_close(d, exclude=exclude)
        for sym in picks:
            ret, exit_d, reason = trade(sym, d, hold, sl_pct)
            if ret is None:
                continue
            mkt = kospi_ret(d, exit_d)
            if mkt is None:
                continue
            trades.append((d, sym, ret, mkt, ret - mkt, reason, exit_d))
    return trades


def report(label, trades, cost=0.30):
    print(f"\n{'='*100}")
    print(f"{label}")
    print(f"{'='*100}")
    if not trades:
        print("no trades")
        return
    pnls = [t[2] for t in trades]
    alphas = [t[4] for t in trades]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    print(f"  n={n}  win={wins*100//n}%  avg={mean(pnls):+.2f}%  total={sum(pnls):+.2f}%")
    print(f"  alpha avg={mean(alphas):+.2f}%  total={sum(alphas):+.2f}%")
    print(f"  best={max(pnls):+.2f}%  worst={min(pnls):+.2f}%  median={sorted(pnls)[n//2]:+.2f}%")

    sorted_p = sorted(pnls, reverse=True)
    print(f"\n  [Outlier 의존도]")
    print(f"    top 5 합: {sum(sorted_p[:5]):+.2f}% ({sum(sorted_p[:5])/sum(pnls)*100:.0f}%)")
    print(f"    top 10 합: {sum(sorted_p[:10]):+.2f}% ({sum(sorted_p[:10])/sum(pnls)*100:.0f}%)")
    print(f"    top 5 빼면: total={sum(pnls)-sum(sorted_p[:5]):+.2f}%  avg={(sum(pnls)-sum(sorted_p[:5]))/(n-5):+.2f}%")
    print(f"    top 10 빼면: total={sum(pnls)-sum(sorted_p[:10]):+.2f}%  avg={(sum(pnls)-sum(sorted_p[:10]))/(n-10):+.2f}%")

    after_cost = [p - cost for p in pnls]
    after_cost_alpha = [a - cost for a in alphas]
    wins2 = sum(1 for p in after_cost if p > 0)
    print(f"\n  [거래비용 -{cost}% 차감]")
    print(f"    win={wins2*100//n}%  avg={mean(after_cost):+.2f}%  total={sum(after_cost):+.2f}%")
    print(f"    alpha avg={mean(after_cost_alpha):+.2f}%  total={sum(after_cost_alpha):+.2f}%")

    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t[1]].append(t[2])
    sym_sum = sorted([(s, sum(p), len(p)) for s, p in by_sym.items()], key=lambda x: -x[1])
    print(f"\n  [종목 다양성] 총 unique syms = {len(by_sym)}")
    print(f"    top 10 contribution:")
    for s, tot, cnt in sym_sum[:10]:
        print(f"      {s} n={cnt} sum={tot:+.2f}%")

    pick_count = Counter()
    for _, sym, _, _, _, _, _ in trades:
        pick_count[sym] += 1
    print(f"\n  [픽업 빈도 top 10]")
    for s, c in pick_count.most_common(10):
        print(f"      {s}: {c}회 누적 {sum(by_sym[s]):+.2f}%")


# 1. baseline: 5일, bull only
trades_5d_bull = collect(hold=5, bull_only=True)
report("STRONG_CLOSE 5-day BULL only (baseline)", trades_5d_bull)

# 2. with SL -3.5% (intraday)
trades_5d_bull_sl = collect(hold=5, bull_only=True, sl_pct=-3.5)
report("STRONG_CLOSE 5-day BULL + SL -3.5%", trades_5d_bull_sl)

# 3. shorter SL -2%
trades_5d_bull_sl2 = collect(hold=5, bull_only=True, sl_pct=-2.0)
report("STRONG_CLOSE 5-day BULL + SL -2%", trades_5d_bull_sl2)

# 4. survivorship check: exclude top 20 contributors
trades_for_top = collect(hold=5, bull_only=True)
sym_contrib = defaultdict(float)
for _, sym, ret, _, _, _, _ in trades_for_top:
    sym_contrib[sym] += ret
top20_syms = set([s for s, _ in sorted(sym_contrib.items(), key=lambda x: -x[1])[:20]])
print(f"\n>> Excluding top-20 contributing syms: {sorted(top20_syms)}")
trades_no_top = collect(hold=5, bull_only=True, exclude=top20_syms)
report("STRONG_CLOSE 5-day BULL — top 20 종목 제외 (survivorship test)", trades_no_top)

# 5. Drawdown analysis (sequential equity curve, 1 unit per trade)
print(f"\n{'='*100}")
print("DRAWDOWN ANALYSIS — 5-day BULL baseline")
print(f"{'='*100}")
# group by entry date, sum daily PnL
by_day = defaultdict(list)
for t in trades_5d_bull:
    by_day[t[0]].append(t[2])
day_returns = sorted([(d, sum(ps)/5) for d, ps in by_day.items()])  # 5종목 균등 가정 → 일평균
equity = 100.0
peak = 100.0
max_dd = 0.0
ddays = []
for d, r in day_returns:
    equity *= (1 + r/100)
    if equity > peak:
        peak = equity
    dd = (equity / peak - 1) * 100
    if dd < max_dd:
        max_dd = dd
    ddays.append((d, equity, dd))
print(f"  최종 equity: {equity:.2f} (시작 100)")
print(f"  최대 drawdown: {max_dd:.2f}%")
print(f"  진입일수: {len(day_returns)}")
print(f"  worst 5 days:")
worst = sorted(day_returns, key=lambda x: x[1])[:5]
for d, r in worst:
    print(f"    {d}: {r:+.2f}%")

# 6. Hold period sweep (with SL variants)
print(f"\n{'='*100}")
print("HOLD PERIOD + SL VARIANTS — bull only, with cost -0.3% applied")
print(f"{'='*100}")
print(f"  {'hold':>4s} {'sl':>5s}  {'n':>4s} {'win%':>5s} {'avg':>7s} {'alpha':>7s} {'after_cost_alpha':>15s}")
for hold in [2, 3, 5, 7, 10]:
    for sl in [None, -2.0, -3.5, -5.0]:
        ts = collect(hold=hold, bull_only=True, sl_pct=sl)
        if not ts:
            continue
        pnls = [t[2] for t in ts]
        alphas = [t[4] for t in ts]
        wins = sum(1 for p in pnls if p > 0)
        ac_alpha = mean(a - 0.3 for a in alphas)
        sl_str = f"{sl}" if sl is not None else "none"
        print(f"  {hold:>4d} {sl_str:>5s}  {len(pnls):>4d} {wins*100//len(pnls):>4d}% "
              f"{mean(pnls):+6.2f}% {mean(alphas):+6.2f}% {ac_alpha:+14.2f}%")
