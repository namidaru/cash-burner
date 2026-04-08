"""
Leverage ETF + KOSPI MA regime backtest.

Strategy: KOSPI(069500) close vs MA_N
  - close > MA_N → 122630 (KODEX 레버리지) hold
  - close < MA_N → cash (or 252670 KODEX인버스2X)

Compare:
  - Buy & hold 069500 (basic KOSPI)
  - Buy & hold 122630 (raw leverage)
  - Strategy: regime filter only (long/cash)
  - Strategy: long/short (long lev, short via inverse2x)
  - Sweep MA periods 5, 10, 20, 60
"""
import os, json
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)

KOSPI = "069500"      # KODEX 200 (시장 프록시)
LEV   = "122630"      # KODEX 레버리지 (2x long)
INV2X = "252670"      # KODEX 200선물 인버스 2X

def get_dates(sym):
    return sorted(cache.get(sym, {}).keys())


def get_close(sym, d):
    return cache[sym].get(d, {}).get("close", 0)


# common date range = intersection of all 3
all_dates = sorted(set(get_dates(KOSPI)) & set(get_dates(LEV)) & set(get_dates(INV2X)))
print(f"common dates: {len(all_dates)}  ({all_dates[0]} ~ {all_dates[-1]})")


def buy_and_hold(sym, dates):
    closes = [get_close(sym, d) for d in dates if get_close(sym, d) > 0]
    if len(closes) < 2:
        return 0.0
    return (closes[-1] / closes[0] - 1) * 100


def equity_curve(daily_returns):
    eq = 100.0
    peak = 100.0
    max_dd = 0.0
    for r in daily_returns:
        eq *= (1 + r/100)
        if eq > peak:
            peak = eq
        dd = (eq / peak - 1) * 100
        if dd < max_dd:
            max_dd = dd
    return eq, max_dd


def daily_return(sym, d_prev, d):
    p = get_close(sym, d_prev)
    c = get_close(sym, d)
    if p <= 0 or c <= 0:
        return 0.0
    return (c / p - 1) * 100


def regime_signal(d_idx, ma_period, mode="long_only"):
    """At end of day d_idx, decide tomorrow's holding.
    long_only: bull=LEV, bear=cash
    long_short: bull=LEV, bear=INV2X
    """
    if d_idx < ma_period - 1:
        return None
    closes = [get_close(KOSPI, all_dates[i]) for i in range(d_idx - ma_period + 1, d_idx + 1)]
    if any(c <= 0 for c in closes):
        return None
    ma = mean(closes)
    cur = closes[-1]
    if cur > ma:
        return "lev"
    else:
        return "cash" if mode == "long_only" else "inv"


def run_strategy(ma_period, mode="long_only"):
    """At end of day i, compute signal. Hold from open of i+1 to close of i+1.
    For simplicity: hold from close of i to close of i+1 (signal at close, execute next day open).
    Actually: signal at close of day i → open of day i+1 buy → close of day i+1 sell
    Simplification: assume execution at close-to-close (small slippage assumption).
    """
    daily_pnls = []
    for i in range(len(all_dates) - 1):
        sig = regime_signal(i, ma_period, mode)
        if sig is None:
            continue
        d_today = all_dates[i]
        d_next = all_dates[i + 1]
        if sig == "lev":
            r = daily_return(LEV, d_today, d_next)
        elif sig == "inv":
            r = daily_return(INV2X, d_today, d_next)
        else:
            r = 0.0
        daily_pnls.append(r)
    return daily_pnls


# ----- Baselines -----
print(f"\n{'='*100}")
print("BASELINES — buy & hold")
print(f"{'='*100}")

for sym, name in [(KOSPI, "069500 KODEX200"), (LEV, "122630 KODEX레버리지"), (INV2X, "252670 KODEX인버스2X")]:
    closes = [get_close(sym, d) for d in all_dates]
    if not all(c > 0 for c in closes):
        continue
    daily = [(closes[i+1]/closes[i]-1)*100 for i in range(len(closes)-1)]
    eq, dd = equity_curve(daily)
    total_ret = (closes[-1]/closes[0]-1)*100
    win = sum(1 for r in daily if r > 0)
    print(f"  {name:25s}  total={total_ret:+7.2f}%  max_dd={dd:+7.2f}%  win={win*100//len(daily)}%  vol={(mean([r**2 for r in daily])**0.5):.2f}%")

# ----- Strategies -----
print(f"\n{'='*100}")
print("REGIME STRATEGIES")
print(f"{'='*100}")
print(f"{'strategy':<35s} {'total':>9s} {'max_dd':>9s} {'win%':>5s} {'days':>5s} {'lev_days':>9s}")

for ma in [5, 10, 20, 60]:
    for mode in ["long_only", "long_short"]:
        pnls = run_strategy(ma, mode)
        if not pnls:
            continue
        eq, dd = equity_curve(pnls)
        total = sum(pnls)
        # actually compound:
        compound = (eq - 100)
        wins = sum(1 for r in pnls if r > 0)
        non_zero = sum(1 for r in pnls if r != 0)
        # signal distribution
        lev_days = 0
        for i in range(len(all_dates) - 1):
            sig = regime_signal(i, ma, mode)
            if sig == "lev":
                lev_days += 1
        label = f"MA{ma:>2d} {mode}"
        print(f"  {label:<33s}  {compound:+7.2f}%  {dd:+7.2f}%  {wins*100//max(non_zero,1):>4d}%  {len(pnls):>4d}  {lev_days:>7d}")

# ----- Detail for best -----
print(f"\n{'='*100}")
print("DAILY PnL — MA20 long_only")
print(f"{'='*100}")
pnls = run_strategy(20, "long_only")
eq, dd = equity_curve(pnls)
print(f"  total compound: {eq-100:+.2f}%  max_dd: {dd:+.2f}%")
print(f"  진입 일수: {sum(1 for p in pnls if p != 0)}/{len(pnls)}")
print(f"  best 5: {sorted(pnls, reverse=True)[:5]}")
print(f"  worst 5: {sorted(pnls)[:5]}")

# 정확한 시그널 변경 횟수
changes = 0
last_sig = None
for i in range(len(all_dates) - 1):
    sig = regime_signal(i, 20, "long_only")
    if sig is not None and sig != last_sig:
        changes += 1
        last_sig = sig
print(f"  시그널 변경 횟수: {changes} (= 거래 횟수)")

# 거래비용 차감 (시그널 변경 1회 = 매도+매수 = 0.3% 비용)
cost_per_change = 0.30
total_cost = changes * cost_per_change
print(f"\n  거래비용 -{cost_per_change}% × {changes}회 = -{total_cost:.2f}%")
print(f"  비용 차감 후: {eq - 100 - total_cost:+.2f}%")
