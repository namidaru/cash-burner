"""
4/8 dip-buy backtest

Goal: validate "watch market then buy on dip" strategy on 4/8 unbiased.
- Pool: theme 45 stocks
- Parse H0STCNT0 ticks per symbol from ws_dump
- Simulate dip-buy: track running high, buy when price drops X% from high then recovers Y%
- Exit: trail(5% arm, 3% drop) + SL -3% + force close 14:00
- Sweep parameters
"""
import os, sys, json, time
from collections import defaultdict

sys.path.insert(0, "src")
from parser import extract_raw, parse_ws_raw_multi

DATE = os.environ.get("BT_DATE", "20260408")
_compact = f"data/logs/{DATE}/ws_dump_compact.log"
WS_PATH = _compact if os.path.exists(_compact) else f"data/logs/{DATE}/ws_dump.log"
THEME_DBG = f"data/logs/{DATE}/theme_debug.log"
PREVCLOSE = "data/prev_close.json"

# ---------- load pool ----------
pool = []
with open(THEME_DBG, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        if "BUILD SELECTED" in line:
            try:
                arr = line.split("BUILD SELECTED:")[1].strip()
                pool = eval(arr)
            except Exception:
                pass
print(f"theme pool: {len(pool)} stocks")

with open(PREVCLOSE, "r", encoding="utf-8") as f:
    prev_close = json.load(f)

# ---------- parse ticks ----------
# ticks[sym] = [(hhmmss_int, price), ...] from H0STCNT0 only
ticks = defaultdict(list)
pool_set = set(pool)

t0 = time.time()
n_lines = 0
n_ticks = 0
with open(WS_PATH, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        n_lines += 1
        raw = extract_raw(line)
        if not raw:
            continue
        for tr_id, row in parse_ws_raw_multi(raw):
            if tr_id != "H0STCNT0":
                continue
            sym = row.get("MKSC_SHRN_ISCD", "")
            if sym not in pool_set:
                continue
            try:
                price = float(row.get("STCK_PRPR", "0"))
                hms = int(row.get("STCK_CNTG_HOUR", "0"))
            except Exception:
                continue
            if price <= 0 or hms < 90000:
                continue
            ticks[sym].append((hms, price))
            n_ticks += 1

print(f"parsed {n_lines} lines, {n_ticks} ticks across {len(ticks)} syms in {time.time()-t0:.1f}s")

# sort
for sym in ticks:
    ticks[sym].sort()

# ---------- backtest ----------
def simulate(symbol_ticks, dip_pct, recov_pct, entry_start, entry_end,
             trail_arm=5.0, trail_drop=3.0, sl=-3.0, force_exit=140000):
    """One symbol simulation. Returns (entry_hms, entry_px, exit_hms, exit_px, pnl_pct, reason) or None."""
    entered = False
    entry_px = 0
    entry_hms = 0
    high_since_entry = 0
    armed = False
    running_high = 0
    rh_hms = 0

    for hms, px in symbol_ticks:
        if not entered:
            if hms < entry_start or hms >= entry_end:
                continue
            if running_high == 0 or px > running_high:
                running_high = px
                rh_hms = hms
                continue
            # dip from high?
            drop = (px / running_high - 1) * 100
            if drop > dip_pct:
                continue  # not dipped enough (drop is negative; dip_pct is e.g. -1.5)
            # in dip, watch for recovery
            # we treat each tick: if currently below the running_high by <= dip_pct,
            # and price has risen recov_pct from local min — buy
            # simplify: track local min after running_high
            # quick approach: if price is below high by >= |dip_pct|, mark dip_low; then buy when price >= dip_low * (1+recov_pct/100)
            # we'll do this with a state var
        else:
            # exit logic
            if px > high_since_entry:
                high_since_entry = px
            ret = (px / entry_px - 1) * 100
            high_ret = (high_since_entry / entry_px - 1) * 100
            if not armed and high_ret >= trail_arm:
                armed = True
            if armed:
                # trail: drop from high_since_entry by trail_drop%
                drop_from_peak = (px / high_since_entry - 1) * 100
                if drop_from_peak <= -trail_drop:
                    return (entry_hms, entry_px, hms, px, ret, "trail")
            if ret <= sl:
                return (entry_hms, entry_px, hms, px, ret, "sl")
            if hms >= force_exit:
                return (entry_hms, entry_px, hms, px, ret, "force")

    # if entered but never exited
    if entered and symbol_ticks:
        last_hms, last_px = symbol_ticks[-1]
        ret = (last_px / entry_px - 1) * 100
        return (entry_hms, entry_px, last_hms, last_px, ret, "eod")
    return None


def simulate_v2(symbol_ticks, dip_pct, recov_pct, entry_start, entry_end,
                trail_arm=5.0, trail_drop=3.0, sl=-3.0, force_exit=140000):
    """Cleaner state machine."""
    state = "scan"  # scan -> in_dip -> entered
    running_high = 0
    dip_low = 0
    entry_px = 0
    entry_hms = 0
    high_since_entry = 0
    armed = False

    for hms, px in symbol_ticks:
        if state == "scan":
            if hms < entry_start:
                continue
            if hms >= entry_end:
                return None
            if px > running_high:
                running_high = px
                continue
            if running_high == 0:
                running_high = px
                continue
            drop = (px / running_high - 1) * 100
            if drop <= dip_pct:  # dipped enough (dip_pct negative)
                state = "in_dip"
                dip_low = px
        elif state == "in_dip":
            if hms >= entry_end:
                return None
            if px < dip_low:
                dip_low = px
                continue
            recov = (px / dip_low - 1) * 100
            if recov >= recov_pct:
                # BUY
                state = "entered"
                entry_px = px
                entry_hms = hms
                high_since_entry = px
            # if price exceeds running_high again, reset to scan from new high
            if px > running_high:
                running_high = px
                state = "scan"
        elif state == "entered":
            if px > high_since_entry:
                high_since_entry = px
            ret = (px / entry_px - 1) * 100
            high_ret = (high_since_entry / entry_px - 1) * 100
            if not armed and high_ret >= trail_arm:
                armed = True
            if armed:
                drop_from_peak = (px / high_since_entry - 1) * 100
                if drop_from_peak <= -trail_drop:
                    return (entry_hms, entry_px, hms, px, ret, "trail")
            if ret <= sl:
                return (entry_hms, entry_px, hms, px, ret, "sl")
            if hms >= force_exit:
                return (entry_hms, entry_px, hms, px, ret, "force")

    if state == "entered" and symbol_ticks:
        last_hms, last_px = symbol_ticks[-1]
        ret = (last_px / entry_px - 1) * 100
        return (entry_hms, entry_px, last_hms, last_px, ret, "eod")
    return None


# ---------- sweep ----------
configs = [
    # (dip%, recov%, start, end, label)
    (-1.0, 0.2, 90000, 110000, "dip1.0_r0.2_9-11"),
    (-1.5, 0.3, 90000, 110000, "dip1.5_r0.3_9-11"),
    (-2.0, 0.3, 90000, 110000, "dip2.0_r0.3_9-11"),
    (-1.5, 0.3, 93000, 110000, "dip1.5_r0.3_930-11"),
    (-1.5, 0.3, 93000, 120000, "dip1.5_r0.3_930-12"),
    (-2.0, 0.5, 90000, 120000, "dip2.0_r0.5_9-12"),
    (-1.0, 0.3, 93000, 113000, "dip1.0_r0.3_930-1130"),
    (-1.5, 0.5, 93000, 113000, "dip1.5_r0.5_930-1130"),
]

print(f"\n{'='*100}")
print(f"DIP BUY BACKTEST — 4/8 theme pool ({len(pool)} stocks)")
print(f"{'='*100}\n")

for dip_pct, recov_pct, t_start, t_end, label in configs:
    results = []
    for sym in pool:
        st = ticks.get(sym)
        if not st or len(st) < 10:
            continue
        r = simulate_v2(st, dip_pct, recov_pct, t_start, t_end)
        if r:
            results.append((sym, *r))
    if not results:
        print(f"{label:30s} | no trades")
        continue
    pnls = [r[5] for r in results]
    wins = sum(1 for p in pnls if p > 0)
    avg = sum(pnls) / len(pnls)
    total = sum(pnls)
    best = max(pnls)
    worst = min(pnls)
    print(f"{label:30s} | trades={len(pnls):2d} win={wins:2d}/{len(pnls):2d} ({wins*100//len(pnls):2d}%) "
          f"avg={avg:+5.2f}% total={total:+6.2f}% best={best:+5.2f}% worst={worst:+5.2f}%")

# ---------- detail for best config ----------
print(f"\n{'='*100}")
print(f"DETAIL — dip1.5_r0.3_930-1130 (눌림 -1.5% 회복 +0.3%, 9:30~11:30)")
print(f"{'='*100}")
detail_results = []
for sym in pool:
    st = ticks.get(sym)
    if not st or len(st) < 10:
        continue
    r = simulate_v2(st, -1.5, 0.3, 93000, 113000)
    if r:
        detail_results.append((sym, *r))

detail_results.sort(key=lambda x: -x[5])
print(f"{'sym':8s} {'entry_t':>8s} {'entry_px':>10s} {'exit_t':>8s} {'exit_px':>10s} {'pnl%':>7s} {'reason':10s}")
for sym, eh, ep, xh, xp, pnl, reason in detail_results:
    print(f"{sym:8s} {eh:8d} {ep:10.0f} {xh:8d} {xp:10.0f} {pnl:+7.2f} {reason:10s}")

# also: stocks that had no entry in this config
non_entered = [s for s in pool if s not in {r[0] for r in detail_results} and ticks.get(s)]
print(f"\nno entry ({len(non_entered)} stocks): {non_entered[:20]}")

# ---------- max gain reference ----------
print(f"\n{'='*100}")
print(f"REFERENCE — pool 종목별 9:00-13:00 max gain from open")
print(f"{'='*100}")
ref = []
for sym in pool:
    st = ticks.get(sym)
    if not st:
        continue
    morn = [(h, p) for h, p in st if 90000 <= h < 130000]
    if len(morn) < 5:
        continue
    open_px = morn[0][1]
    high_px = max(p for _, p in morn)
    low_px = min(p for _, p in morn)
    last_px = morn[-1][1]
    ref.append((sym, open_px, high_px, low_px, last_px,
                (high_px/open_px-1)*100, (last_px/open_px-1)*100))
ref.sort(key=lambda x: -x[5])
print(f"{'sym':8s} {'open':>8s} {'high':>8s} {'low':>8s} {'close':>8s} {'max%':>7s} {'cls%':>7s}")
for s, o, h, l, c, mp, cp in ref[:20]:
    print(f"{s:8s} {o:8.0f} {h:8.0f} {l:8.0f} {c:8.0f} {mp:+7.2f} {cp:+7.2f}")
