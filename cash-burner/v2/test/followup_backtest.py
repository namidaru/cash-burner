"""
Volume surge follow-up backtest.

Strategy: 분봉마다 거래대금 + 가격 모니터링하다가
  1. 직전 5분 거래대금이 그 이전 25분 평균의 3배 이상 (거래량 급증)
  2. 현재가가 직전 30분 고점 돌파 (가격 돌파)
  3. dayrise (시가 대비) 양수
  → 진입. trail/SL exit.

Pool: data/volume_profiles 602 syms.
Sweep: 거래량 배수, 모니터링 시작 시각, exit 룰.
"""
import os
import csv
from collections import defaultdict

VP_DIR = "data/volume_profiles"

# ---------- load ----------
print("loading...", flush=True)
bars = defaultdict(lambda: defaultdict(list))
files = [f for f in os.listdir(VP_DIR) if f.endswith(".csv")]
for fname in files:
    sym = fname[:-4]
    if not (len(sym) == 6 and sym.isdigit()):
        continue
    with open(os.path.join(VP_DIR, fname), "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                bars[sym][r["date"]].append((
                    int(r["time"]),
                    float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]),
                    float(r["volume"]), float(r["tr_pbmn"]),
                ))
            except Exception:
                pass
for sym in bars:
    for d in bars[sym]:
        bars[sym][d].sort()

all_dates = sorted({d for sym in bars for d in bars[sym]})
print(f"loaded {len(bars)} syms × {len(all_dates)} dates")


# ---------- simulator ----------
def simulate_exit(bar_list_after_entry, entry_px,
                  trail_arm=5.0, trail_drop=3.0, sl=-3.5, force_hms=131500):
    high_since = entry_px
    armed = False
    for hms, op, hi, lo, cl, _, _ in bar_list_after_entry:
        if hi > high_since:
            high_since = hi
        sl_px = entry_px * (1 + sl / 100)
        if lo <= sl_px:
            return sl, hms, "sl"
        high_ret = (high_since / entry_px - 1) * 100
        if not armed and high_ret >= trail_arm:
            armed = True
        if armed:
            trail_px = high_since * (1 - trail_drop / 100)
            if lo <= trail_px:
                return (trail_px / entry_px - 1) * 100, hms, "trail"
        if hms >= force_hms:
            return (cl / entry_px - 1) * 100, hms, "force"
    if bar_list_after_entry:
        last = bar_list_after_entry[-1]
        return (last[4] / entry_px - 1) * 100, last[0], "eod"
    return 0.0, 0, "noop"


def run_followup(date, vol_mult=3.0, breakout_lookback=30, surge_window=5,
                 max_positions=5, entry_start=90500, entry_end=110000,
                 require_dayrise_pos=True):
    """One day simulation. Returns list of (sym, entry_hms, entry_px, pnl, reason)."""
    # build per-minute snapshots: at each minute t, scan all syms
    # for each sym: compute surge ratio, breakout check, dayrise check
    # to be efficient, iterate per sym with cumulative state
    #
    # We'll iterate per minute (sorted set of all minutes) and per sym track state.
    # But simpler: per sym, scan its bars chronologically and emit entry signal.

    candidates = []  # (entry_hms, sym, entry_px, surge_ratio)
    for sym, dmap in bars.items():
        b = dmap.get(date)
        if not b or len(b) < breakout_lookback + surge_window + 5:
            continue
        # compute open price (first bar's open)
        open_px = b[0][1]
        if open_px <= 0:
            continue
        # iterate
        for i in range(breakout_lookback + surge_window, len(b)):
            hms = b[i][0]
            if hms < entry_start:
                continue
            if hms >= entry_end:
                break
            cur_close = b[i][4]
            # dayrise check
            if require_dayrise_pos and cur_close <= open_px:
                continue
            # breakout: current close > max(high) in lookback window
            lookback_bars = b[i - breakout_lookback:i]
            max_high = max(x[2] for x in lookback_bars)
            if cur_close <= max_high:
                continue
            # surge: last surge_window tr_pbmn vs prior breakout_lookback-surge_window avg
            recent_tr = sum(x[6] for x in b[i - surge_window:i])
            prior_window = b[i - breakout_lookback:i - surge_window]
            if not prior_window:
                continue
            prior_avg_tr = sum(x[6] for x in prior_window) / len(prior_window)
            if prior_avg_tr <= 0:
                continue
            surge_ratio = (recent_tr / surge_window) / prior_avg_tr
            if surge_ratio < vol_mult:
                continue
            # entry at next bar open
            if i + 1 >= len(b):
                continue
            entry_bar = b[i + 1]
            entry_px = entry_bar[1]
            candidates.append((hms, sym, entry_px, surge_ratio, i + 1))
            break  # one entry per sym per day
    # sort by signal time, take first max_positions
    candidates.sort(key=lambda x: (x[0], -x[3]))
    picked = candidates[:max_positions]
    results = []
    for sig_hms, sym, entry_px, ratio, entry_idx in picked:
        b_after = bars[sym][date][entry_idx + 1:]
        pnl, exit_hms, reason = simulate_exit(b_after, entry_px)
        results.append((sym, sig_hms, entry_px, pnl, reason, ratio))
    return results


def aggregate(label, **kwargs):
    all_pnls = []
    days_total = []
    for d in all_dates:
        rs = run_followup(d, **kwargs)
        if rs:
            day = sum(r[3] for r in rs)
            days_total.append((d, day, len(rs)))
            for r in rs:
                all_pnls.append(r[3])
    if not all_pnls:
        print(f"{label:50s} | no trades")
        return None
    wins = sum(1 for p in all_pnls if p > 0)
    avg = sum(all_pnls) / len(all_pnls)
    total = sum(all_pnls)
    pos_days = sum(1 for _, t, _ in days_total if t > 0)
    print(f"{label:50s} | n={len(all_pnls):3d} win={wins*100//len(all_pnls):2d}% "
          f"avg={avg:+5.2f}% total={total:+7.2f}% days+={pos_days}/{len(days_total)}")
    return days_total


print(f"\n{'='*100}")
print("VOLUME SURGE FOLLOW-UP — exit: trail(5%/3%) + SL(-3.5%) + force 13:15")
print(f"{'='*100}\n")

aggregate("F1: vol×3, breakout-30, 9:05~11:00, dayrise+", vol_mult=3.0)
aggregate("F2: vol×5, breakout-30, 9:05~11:00, dayrise+", vol_mult=5.0)
aggregate("F3: vol×3, breakout-15, 9:05~11:00, dayrise+", vol_mult=3.0, breakout_lookback=15)
aggregate("F4: vol×3, breakout-30, 9:30~12:00, dayrise+", vol_mult=3.0, entry_start=93000, entry_end=120000)
aggregate("F5: vol×5, breakout-15, 9:05~11:00, dayrise+", vol_mult=5.0, breakout_lookback=15)
aggregate("F6: vol×4, breakout-20, 9:10~11:30", vol_mult=4.0, breakout_lookback=20, entry_start=91000, entry_end=113000)
aggregate("F7: vol×3 NO dayrise gate", vol_mult=3.0, require_dayrise_pos=False)

# detail for the best
print(f"\n{'='*100}")
print("DAILY DETAIL — F1 (vol×3, breakout-30, 9:05~11:00)")
print(f"{'='*100}")
print(f"{'date':10s} {'n':>3s} {'total':>9s} | per-sym")
for d in all_dates:
    rs = run_followup(d, vol_mult=3.0)
    if not rs:
        continue
    day = sum(r[3] for r in rs)
    detail = " ".join(f"{r[0]}({r[3]:+.1f})" for r in rs[:5])
    print(f"{d:10s} {len(rs):>3d} {day:>+8.2f}% | {detail}")
