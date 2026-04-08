"""
Cross-sectional momentum (Jegadeesh-Titman style).

Each month: rank all syms by past N-month return, buy top K, hold M months, rebalance.
Skip last month (J-T classic: 12-1).

Compare to:
  - 069500 buy & hold
  - 122630 buy & hold
"""
import os, json, math
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)

# build per-day price for every sym
sym_dates = {}
for sym, days in cache.items():
    if not days:
        continue
    sym_dates[sym] = sorted(days.keys())

all_dates = sorted(set(d for sd in sym_dates.values() for d in sd))
print(f"all dates: {len(all_dates)}  ({all_dates[0]}~{all_dates[-1]})")

# month boundaries (last trading day of each month)
month_last = {}
for d in all_dates:
    month_last[d[:6]] = d
months = sorted(month_last.keys())
month_end_dates = [month_last[m] for m in months]
print(f"months: {len(months)}  ({months[0]}~{months[-1]})")


def get_close(sym, d):
    return cache.get(sym, {}).get(d, {}).get("close", 0)


def momentum_score(sym, end_d, lookback_months=12, skip_months=1):
    """Return % over [end - lookback, end - skip]."""
    end_idx = month_end_dates.index(end_d)
    if end_idx < lookback_months:
        return None
    start_idx = end_idx - lookback_months
    skip_idx = end_idx - skip_months
    start_d = month_end_dates[start_idx]
    skip_d = month_end_dates[skip_idx]
    p0 = get_close(sym, start_d)
    p1 = get_close(sym, skip_d)
    if p0 <= 0 or p1 <= 0:
        return None
    return (p1/p0 - 1) * 100


# ETFs and known broad indices to exclude (we want stock momentum, not ETF)
ETF_PREFIXES = set()
LIKELY_ETFS = {
    "069500", "122630", "252670", "233740", "379800", "114800", "229200", "251340",
    "153130", "132030", "278530", "294400", "069660", "294400", "139660", "143850",
    "117460", "148020", "117700", "139660", "278420", "385600",
}


def universe_at(end_d):
    """Syms with valid 12m+ data and not in ETF list."""
    out = []
    for sym in sym_dates:
        if sym in LIKELY_ETFS:
            continue
        # require enough history
        sd = sym_dates[sym]
        if not sd or sd[0] > all_dates[max(0, all_dates.index(end_d)-260)]:
            continue
        out.append(sym)
    return out


def run_xs_mom(top_k=10, lookback=12, skip=1, hold=1, include_etf=False):
    """Monthly rebalance. hold=1 → rebalance every month."""
    monthly_rets = []
    months_used = []
    for mi in range(lookback + skip, len(month_end_dates) - hold):
        d_form = month_end_dates[mi]
        d_exit = month_end_dates[mi + hold]
        # rank
        scored = []
        for sym in sym_dates:
            if not include_etf and sym in LIKELY_ETFS:
                continue
            s = momentum_score(sym, d_form, lookback, skip)
            if s is None:
                continue
            # need price at form date and exit date
            p_form = get_close(sym, d_form)
            p_exit = get_close(sym, d_exit)
            if p_form <= 0 or p_exit <= 0:
                continue
            scored.append((sym, s, p_form, p_exit))
        if len(scored) < top_k:
            continue
        scored.sort(key=lambda x: -x[1])
        picks = scored[:top_k]
        # equal weight
        port_ret = mean((p_exit/p_form - 1)*100 for _, _, p_form, p_exit in picks)
        monthly_rets.append(port_ret)
        months_used.append(d_form)
    return monthly_rets, months_used


def equity(rs):
    eq = 100.0
    peak = 100.0
    mdd = 0.0
    for r in rs:
        eq *= (1 + r/100)
        if eq > peak: peak = eq
        dd = (eq/peak-1)*100
        if dd < mdd: mdd = dd
    return eq, mdd


def sharpe_monthly(rs):
    if not rs: return 0
    m = mean(rs)
    var = sum((r-m)**2 for r in rs)/max(len(rs)-1,1)
    sd = math.sqrt(var)
    return m/sd*math.sqrt(12) if sd > 0 else 0


def cagr_monthly(eq, n_months):
    if n_months <= 0 or eq <= 0: return 0
    return (eq/100)**(12/n_months)*100 - 100


# Baselines
def baseline(sym):
    rs = []
    for mi in range(1, len(month_end_dates)):
        p0 = get_close(sym, month_end_dates[mi-1])
        p1 = get_close(sym, month_end_dates[mi])
        if p0 > 0 and p1 > 0:
            rs.append((p1/p0-1)*100)
    eq, mdd = equity(rs)
    return rs, eq, mdd


print(f"\n{'='*100}")
print(f"BASELINES (monthly returns)")
print(f"{'='*100}")
for sym, name in [("069500","KODEX200"),("122630","KODEX레버리지")]:
    rs, eq, mdd = baseline(sym)
    print(f"  {name:<20s}  final={eq:>7.1f}  cagr={cagr_monthly(eq,len(rs)):>+6.1f}%  "
          f"mdd={mdd:>+7.2f}%  sharpe={sharpe_monthly(rs):>+5.2f}  n={len(rs)}m")

print(f"\n{'='*100}")
print(f"CROSS-SECTIONAL MOMENTUM (Jegadeesh-Titman)")
print(f"{'='*100}")
print(f"  {'lookback':<8s} {'skip':<5s} {'top':<5s} {'hold':<5s} {'final':>8s} {'cagr':>8s} {'mdd':>9s} {'sharpe':>8s} {'n':>4s}")

for lookback in [3, 6, 12]:
    for top_k in [5, 10, 20]:
        for hold in [1, 3]:
            rs, _ = run_xs_mom(top_k=top_k, lookback=lookback, skip=1, hold=hold)
            if not rs:
                continue
            eq, mdd = equity(rs)
            print(f"  {lookback:<8d} {1:<5d} {top_k:<5d} {hold:<5d} {eq:>7.1f}  {cagr_monthly(eq,len(rs)*hold):>+6.1f}%  "
                  f"{mdd:>+7.2f}%  {sharpe_monthly(rs):>+5.2f}  {len(rs):>4d}")

# Best one — show details
print(f"\n{'='*100}")
print("DETAIL — 12m, top 10, hold 1m")
print(f"{'='*100}")
rs, months_u = run_xs_mom(top_k=10, lookback=12, skip=1, hold=1)
eq, mdd = equity(rs)
print(f"  final={eq:.1f}  cagr={cagr_monthly(eq,len(rs)):.1f}%  mdd={mdd:.2f}%")
print(f"  best month: {max(rs):+.1f}%")
print(f"  worst month: {min(rs):+.1f}%")
wins = sum(1 for r in rs if r > 0)
print(f"  win months: {wins}/{len(rs)} ({wins*100//len(rs)}%)")

# Show 5 best/worst months
print(f"\n  best 5 months: {sorted(rs, reverse=True)[:5]}")
print(f"  worst 5 months: {sorted(rs)[:5]}")

# Cost: 10종목 monthly turnover ~ 50%, 0.3% one-way → -1.5%/month roughly
print(f"\n[비용 시뮬: 매월 0.5% 차감]")
rs_cost = [r - 0.5 for r in rs]
eq2, mdd2 = equity(rs_cost)
print(f"  final={eq2:.1f}  cagr={cagr_monthly(eq2,len(rs_cost)):.1f}%  mdd={mdd2:.2f}%  sharpe={sharpe_monthly(rs_cost):.2f}")
