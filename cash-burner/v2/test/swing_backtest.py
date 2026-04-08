"""
Swing backtest — holding period sweep + regime filter + alpha vs market.

uses data/daily_ohlc_cache.json (already cached, no new API calls).

Output:
  - 7 selection rules × 6 holding periods
  - Each: raw return, alpha vs KOSPI, win rate
  - With/without regime filter (KOSPI MA5)
  - Baseline: KOSPI200 (069500) buy & hold
"""
import os, sys, json
from collections import defaultdict
from statistics import mean, stdev

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)

# Build day_data
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
print(f"backtest dates: {len(all_dates)} ({all_dates[0]} ~ {all_dates[-1]})")

# ----- KOSPI proxy via 069500 (KODEX200) -----
KOSPI_SYM = "069500"
kospi_dates = sym_dates.get(KOSPI_SYM, [])
kospi_close = {d: cache[KOSPI_SYM][d]["close"] for d in kospi_dates}

def kospi_return(d_start, d_end):
    if d_start not in kospi_close or d_end not in kospi_close:
        return None
    s = kospi_close[d_start]
    e = kospi_close[d_end]
    if s <= 0 or e <= 0:
        return None
    return (e / s - 1) * 100

# KOSPI MA5 for regime filter
kospi_ma5 = {}
sorted_kdates = sorted(kospi_close.keys())
for i, d in enumerate(sorted_kdates):
    if i < 4:
        continue
    last5 = [kospi_close[sorted_kdates[j]] for j in range(i-4, i+1)]
    kospi_ma5[d] = sum(last5) / 5

# ----- Selection rules -----
def sel_top_gainer(d, n=5):
    cands = []
    for sym, r in day_data[d].items():
        if r["prev_close"] <= 0 or r["tr"] < 1e9:
            continue
        chg = (r["close"] / r["prev_close"] - 1) * 100
        if 0 < chg < 15:  # 상한가 제외
            cands.append((sym, chg))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]

def sel_top_loser(d, n=5):
    cands = []
    for sym, r in day_data[d].items():
        if r["prev_close"] <= 0 or r["tr"] < 1e9:
            continue
        chg = (r["close"] / r["prev_close"] - 1) * 100
        if -15 < chg < 0:
            cands.append((sym, chg))
    cands.sort(key=lambda x: x[1])
    return [c[0] for c in cands[:n]]

def sel_strong_close(d, n=5):
    cands = []
    for sym, r in day_data[d].items():
        if r["high"] <= r["low"] or r["tr"] < 5e9:
            continue
        pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
        if pos >= 0.85:
            cands.append((sym, r["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]

def sel_weak_close(d, n=5):
    cands = []
    for sym, r in day_data[d].items():
        if r["high"] <= r["low"] or r["tr"] < 5e9:
            continue
        pos = (r["close"] - r["low"]) / (r["high"] - r["low"])
        if pos <= 0.15:
            cands.append((sym, r["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]

def sel_high_tr(d, n=5):
    cands = sorted(day_data[d].items(), key=lambda x: -x[1]["tr"])
    return [c[0] for c in cands[:n]]

import random
random.seed(42)
def sel_random(d, n=5):
    syms = list(day_data[d].keys())
    if not syms:
        return []
    return random.sample(syms, min(n, len(syms)))

def sel_low_tr(d, n=5):
    """contrarian: 거래대금 적은 종목"""
    cands = [(s, r) for s, r in day_data[d].items() if r["tr"] > 1e8]
    cands.sort(key=lambda x: x[1]["tr"])
    return [c[0] for c in cands[:n]]

STRATEGIES = [
    ("top_gainer", sel_top_gainer),
    ("top_loser", sel_top_loser),
    ("strong_close", sel_strong_close),
    ("weak_close", sel_weak_close),
    ("high_tr", sel_high_tr),
    ("low_tr", sel_low_tr),
    ("random", sel_random),
]

HOLDING_PERIODS = [1, 2, 3, 5, 10, 20]

# ----- Helpers -----
def get_close_after(sym, d, n_days):
    """N거래일 후 close. None if 데이터 없음."""
    sd = sym_dates.get(sym, [])
    if d not in sd:
        return None, None
    i = sd.index(d)
    if i + n_days >= len(sd):
        return None, None
    target_d = sd[i + n_days]
    return target_d, day_data[target_d][sym]["close"]

def trade_return(sym, entry_date, hold_days):
    """entry: close of entry_date. exit: close of entry_date + hold_days."""
    entry_close = day_data[entry_date].get(sym, {}).get("close", 0)
    if entry_close <= 0:
        return None, None
    exit_d, exit_close = get_close_after(sym, entry_date, hold_days)
    if not exit_close or exit_close <= 0:
        return None, None
    return (exit_close / entry_close - 1) * 100, exit_d

def regime_ok(d, mode):
    """mode: 'all' / 'bull' / 'bear'"""
    if mode == "all":
        return True
    if d not in kospi_ma5:
        return False
    if mode == "bull":
        return kospi_close[d] > kospi_ma5[d]
    if mode == "bear":
        return kospi_close[d] < kospi_ma5[d]

# ----- Main backtest -----
def run_strategy(sel_fn, hold_days, regime_mode):
    pnls = []
    alphas = []
    for d in all_dates:
        if not regime_ok(d, regime_mode):
            continue
        picks = sel_fn(d)
        if not picks:
            continue
        # market return for same period
        for sym in picks:
            ret, exit_d = trade_return(sym, d, hold_days)
            if ret is None:
                continue
            mkt = kospi_return(d, exit_d)
            if mkt is None:
                continue
            pnls.append(ret)
            alphas.append(ret - mkt)
    return pnls, alphas

print(f"\n{'='*120}")
print(f"SWING BACKTEST — selection × holding period × regime filter")
print(f"  Universe: {len(cache)} syms, KOSPI proxy = 069500 (KODEX200)")
print(f"{'='*120}\n")

# Print KOSPI baseline buy & hold for each holding period
print("[KOSPI200 BASELINE — 069500 매일 매수 → N일 후 매도]")
print(f"  {'hold':>5s}  {'n':>4s}  {'avg':>7s}  {'win%':>5s}  {'total':>9s}")
for h in HOLDING_PERIODS:
    rs = []
    for d in all_dates:
        r = None
        if d in kospi_close:
            ed, ec = get_close_after(KOSPI_SYM, d, h)
            if ec:
                r = (ec / kospi_close[d] - 1) * 100
        if r is not None:
            rs.append(r)
    if rs:
        wins = sum(1 for x in rs if x > 0)
        print(f"  {h:>5d}  {len(rs):>4d}  {mean(rs):+6.2f}%  {wins*100//len(rs):>4d}%  {sum(rs):+8.2f}%")

print()

for regime in ["all", "bull", "bear"]:
    print(f"\n{'─'*120}")
    print(f"REGIME = {regime.upper()}  (bull = KOSPI > MA5, bear = KOSPI < MA5)")
    print(f"{'─'*120}")
    print(f"{'strategy':<15s} {'hold':>5s} {'n':>4s} {'win%':>5s} {'avg':>7s} {'total':>9s}  {'alpha_avg':>9s} {'alpha_tot':>9s}")
    for sname, sfn in STRATEGIES:
        for h in HOLDING_PERIODS:
            pnls, alphas = run_strategy(sfn, h, regime)
            if not pnls:
                continue
            wins = sum(1 for p in pnls if p > 0)
            print(f"{sname:<15s} {h:>5d} {len(pnls):>4d} {wins*100//len(pnls):>4d}% "
                  f"{mean(pnls):+6.2f}% {sum(pnls):+8.2f}%  "
                  f"{mean(alphas):+8.2f}% {sum(alphas):+8.2f}%")

print(f"\n{'='*120}")
print("READING GUIDE")
print(f"{'='*120}")
print("""
- alpha = 종목 수익률 - 같은 기간 KOSPI 수익률. 진짜 종목선택의 가치.
- alpha_avg > 0 → 시장보다 나음 (real edge)
- alpha_avg ≈ 0 → 그냥 KOSPI 사는 거랑 같음
- alpha_avg < 0 → 시장보다 나쁨 (선택이 오히려 손해)

좋은 신호:
  1. 어떤 strategy/holding 조합에서 alpha_avg > 0.5%
  2. bull regime에서만 그 효과가 강함 (regime filter 효과)
  3. 일관된 win% > 55%

나쁜 신호:
  1. alpha 거의 0 → 그냥 시장 산 거랑 같음 (의미없음)
  2. alpha 음수 → 우리 selection이 오히려 손해
""")
