"""
Selection strategy backtest using data/volume_profiles/*.csv (1-min bars, 602 syms × 24 days).

Goal: 종목선정 방식이 결과를 얼마나 결정하는지 측정.
- 같은 진입/청산 룰 (9:00 시가 매수, trail+SL+force)
- 다른 종목 선정 룰만 비교
- 24일치 결과 집계
"""
import os
import csv
from collections import defaultdict

VP_DIR = "data/volume_profiles"

# ---------- load data ----------
print("loading volume profiles...", flush=True)
# bars[sym][date] = list of (hms_int, open, high, low, close, vol, tr_pbmn)
bars = defaultdict(lambda: defaultdict(list))
files = [f for f in os.listdir(VP_DIR) if f.endswith(".csv")]
n_loaded = 0
for fname in files:
    sym = fname[:-4]
    if not (len(sym) == 6 and sym.isdigit()):
        continue
    path = os.path.join(VP_DIR, fname)
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    d = r["date"]
                    t = int(r["time"])
                    bars[sym][d].append((
                        t,
                        float(r["open"]),
                        float(r["high"]),
                        float(r["low"]),
                        float(r["close"]),
                        float(r["volume"]),
                        float(r["tr_pbmn"]),
                    ))
                except Exception:
                    pass
        n_loaded += 1
    except Exception:
        pass
print(f"loaded {n_loaded} symbols")

# sort each (sym,date) bars by time
for sym in bars:
    for d in bars[sym]:
        bars[sym][d].sort()

# get all dates sorted
all_dates = sorted({d for sym in bars for d in bars[sym]})
print(f"dates: {len(all_dates)}, range {all_dates[0]} ~ {all_dates[-1]}")

# ---------- per-day per-sym summary ----------
# day_stats[date][sym] = {open, high, low, close, vol_total, tr_total, prev_close}
day_stats = defaultdict(dict)
for sym in bars:
    sym_dates = sorted(bars[sym].keys())
    for i, d in enumerate(sym_dates):
        b = bars[sym][d]
        if not b:
            continue
        op = b[0][1]
        hi = max(x[2] for x in b)
        lo = min(x[3] for x in b)
        cl = b[-1][4]
        vol = sum(x[5] for x in b)
        tr = sum(x[6] for x in b)
        prev_cl = bars[sym][sym_dates[i-1]][-1][4] if i > 0 else 0
        day_stats[d][sym] = {
            "open": op, "high": hi, "low": lo, "close": cl,
            "vol": vol, "tr": tr, "prev_close": prev_cl,
        }

print(f"day_stats prepared for {len(day_stats)} dates")


# ---------- entry/exit simulator ----------
def simulate_trade(bar_list, trail_arm=5.0, trail_drop=3.0, sl=-3.5, force_hms=131500):
    """Buy at first bar's open, simulate exit. Returns pnl%."""
    if not bar_list:
        return 0.0
    entry_px = bar_list[0][1]  # open of first bar
    high_since = entry_px
    armed = False
    for hms, op, hi, lo, cl, _, _ in bar_list[1:]:
        # use bar's high to update peak
        if hi > high_since:
            high_since = hi
        # check sl using bar low
        sl_px = entry_px * (1 + sl / 100)
        if lo <= sl_px:
            return sl
        # check trail
        high_ret = (high_since / entry_px - 1) * 100
        if not armed and high_ret >= trail_arm:
            armed = True
        if armed:
            trail_px = high_since * (1 - trail_drop / 100)
            if lo <= trail_px:
                return (trail_px / entry_px - 1) * 100
        if hms >= force_hms:
            return (cl / entry_px - 1) * 100
    # eod
    return (bar_list[-1][4] / entry_px - 1) * 100


# ---------- selection strategies ----------
def strategy_yesterday_up(date, n=5):
    """어제 상승률 상위 5개 (현재 방식 모방)."""
    cands = []
    for sym, s in day_stats[date].items():
        if s["prev_close"] <= 0 or s["open"] <= 0:
            continue
        # yesterday's gain (use previous day stats)
        # we already have prev_close stored. need previous day's open too?
        # simpler: use yesterday close vs day-before close
        sym_dates = sorted(bars[sym].keys())
        idx = sym_dates.index(date) if date in sym_dates else -1
        if idx < 2:
            continue
        ydate = sym_dates[idx - 1]
        ystats = day_stats[ydate].get(sym)
        if not ystats or ystats["prev_close"] <= 0:
            continue
        y_gain = (ystats["close"] / ystats["prev_close"] - 1) * 100
        if not (3.0 <= y_gain <= 15.0):
            continue
        cands.append((sym, y_gain, ystats["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


def strategy_yesterday_down(date, n=5):
    """어제 하락률 상위 5개 (역발상)."""
    cands = []
    for sym, s in day_stats[date].items():
        if s["prev_close"] <= 0:
            continue
        sym_dates = sorted(bars[sym].keys())
        idx = sym_dates.index(date) if date in sym_dates else -1
        if idx < 2:
            continue
        ydate = sym_dates[idx - 1]
        ystats = day_stats[ydate].get(sym)
        if not ystats or ystats["prev_close"] <= 0:
            continue
        y_gain = (ystats["close"] / ystats["prev_close"] - 1) * 100
        if not (-7.0 <= y_gain <= -3.0):
            continue
        cands.append((sym, y_gain, ystats["tr"]))
    cands.sort(key=lambda x: x[1])  # most negative first
    return [c[0] for c in cands[:n]]


def strategy_yesterday_flat_high_volume(date, n=5):
    """어제 잠잠 (-2~+2%) + 거래대금 상위."""
    cands = []
    for sym, s in day_stats[date].items():
        if s["prev_close"] <= 0:
            continue
        sym_dates = sorted(bars[sym].keys())
        idx = sym_dates.index(date) if date in sym_dates else -1
        if idx < 2:
            continue
        ydate = sym_dates[idx - 1]
        ystats = day_stats[ydate].get(sym)
        if not ystats or ystats["prev_close"] <= 0:
            continue
        y_gain = (ystats["close"] / ystats["prev_close"] - 1) * 100
        if not (-2.0 <= y_gain <= 2.0):
            continue
        cands.append((sym, ystats["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


def strategy_high_volume_only(date, n=5):
    """어제 거래대금 1위 5개 (방향 무시)."""
    cands = []
    for sym, s in day_stats[date].items():
        sym_dates = sorted(bars[sym].keys())
        idx = sym_dates.index(date) if date in sym_dates else -1
        if idx < 2:
            continue
        ydate = sym_dates[idx - 1]
        ystats = day_stats[ydate].get(sym)
        if not ystats:
            continue
        cands.append((sym, ystats["tr"]))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


def strategy_intraday_open30_breakout(date, n=5):
    """9:00~9:30 거래대금 폭증 종목 9:30에 매수."""
    cands = []
    for sym, s in day_stats[date].items():
        b = bars[sym][date]
        if not b:
            continue
        first30 = [x for x in b if 90000 <= x[0] < 93000]
        if len(first30) < 10:
            continue
        # average daily tr from past days for normalization
        sym_dates = sorted(bars[sym].keys())
        idx = sym_dates.index(date) if date in sym_dates else -1
        if idx < 5:
            continue
        past_trs = [day_stats[sym_dates[i]].get(sym, {}).get("tr", 0) for i in range(max(0, idx-5), idx)]
        avg_tr = sum(past_trs) / max(1, len(past_trs))
        if avg_tr <= 0:
            continue
        first30_tr = sum(x[6] for x in first30)
        # 폭증 ratio: first30_tr * (240/30) vs avg_tr (extrapolate to full day)
        ratio = (first30_tr * 8) / avg_tr
        if ratio < 2.0:
            continue
        cands.append((sym, ratio))
    cands.sort(key=lambda x: -x[1])
    return [c[0] for c in cands[:n]]


def simulate_strategy(strategy_fn, name, entry_hms_min=90000):
    """Run strategy across all dates."""
    results_per_day = []
    all_pnls = []
    for d in all_dates:
        picks = strategy_fn(d)
        if not picks:
            results_per_day.append((d, [], 0.0))
            continue
        day_pnls = []
        for sym in picks:
            b = bars[sym][d]
            # filter to entry time onwards
            b_after = [x for x in b if x[0] >= entry_hms_min]
            if not b_after:
                continue
            pnl = simulate_trade(b_after)
            day_pnls.append((sym, pnl))
            all_pnls.append(pnl)
        day_total = sum(p for _, p in day_pnls)
        results_per_day.append((d, day_pnls, day_total))
    if not all_pnls:
        print(f"{name:40s} | no trades")
        return
    wins = sum(1 for p in all_pnls if p > 0)
    avg = sum(all_pnls) / len(all_pnls)
    total = sum(all_pnls)
    n_days_pos = sum(1 for _, _, t in results_per_day if t > 0)
    n_days_with_trades = sum(1 for _, p, _ in results_per_day if p)
    print(f"{name:40s} | n={len(all_pnls):3d} win={wins*100//len(all_pnls):2d}% "
          f"avg={avg:+5.2f}% total={total:+7.2f}% days+={n_days_pos}/{n_days_with_trades}")
    return results_per_day


print(f"\n{'='*100}")
print(f"SELECTION STRATEGY BACKTEST — {len(all_dates)} days, exit: trail(5%/3%) + SL(-3.5%) + force 13:15")
print(f"{'='*100}\n")

simulate_strategy(strategy_yesterday_up, "A. 어제 상승 상위 (현재 방식)")
simulate_strategy(strategy_yesterday_down, "B. 어제 하락 상위 (역발상)")
simulate_strategy(strategy_yesterday_flat_high_volume, "C. 어제 잠잠+거래대금 상위")
simulate_strategy(strategy_high_volume_only, "D. 어제 거래대금만 (방향 무시)")
simulate_strategy(strategy_intraday_open30_breakout, "E. 9:30 거래대금 폭증 (장중신호)", entry_hms_min=93000)

# ---------- detail for best ----------
print(f"\n{'='*100}")
print("DAILY DETAIL — A vs B vs E")
print(f"{'='*100}")
print(f"{'date':10s} {'A_total':>10s} {'B_total':>10s} {'E_total':>10s}")

a = simulate_strategy.__globals__  # noop
def get_day_total(strategy_fn, entry_hms=90000):
    res = {}
    for d in all_dates:
        picks = strategy_fn(d)
        total = 0.0
        for sym in picks:
            b = bars[sym][d]
            b_after = [x for x in b if x[0] >= entry_hms]
            if not b_after:
                continue
            total += simulate_trade(b_after)
        res[d] = total
    return res

a_tot = get_day_total(strategy_yesterday_up)
b_tot = get_day_total(strategy_yesterday_down)
e_tot = get_day_total(strategy_intraday_open30_breakout, 93000)
for d in all_dates:
    print(f"{d:10s} {a_tot[d]:>+9.2f}% {b_tot[d]:>+9.2f}% {e_tot[d]:>+9.2f}%")
