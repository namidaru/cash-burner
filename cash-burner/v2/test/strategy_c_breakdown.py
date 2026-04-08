"""
Strategy C breakdown.

Questions to answer:
1. C +54% PnL이 어디서 나왔나? (trail / sl / force / eod 분포)
2. 어떤 종목이 자주 뽑혔고, 누가 큰 PnL을 만들었나?
3. 며칠 보유하면 더 좋아지나?
4. 24일 결과가 한두 outlier에 의존하나?
"""
import os
import csv
from collections import defaultdict, Counter

VP_DIR = "data/volume_profiles"

print("loading...", flush=True)
bars = defaultdict(lambda: defaultdict(list))
for fname in os.listdir(VP_DIR):
    if not fname.endswith(".csv"):
        continue
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

# day stats
day_stats = defaultdict(dict)
for sym in bars:
    sym_dates = sorted(bars[sym].keys())
    for i, d in enumerate(sym_dates):
        b = bars[sym][d]
        if not b:
            continue
        day_stats[d][sym] = {
            "open": b[0][1],
            "high": max(x[2] for x in b),
            "low": min(x[3] for x in b),
            "close": b[-1][4],
            "tr": sum(x[6] for x in b),
            "prev_close": bars[sym][sym_dates[i-1]][-1][4] if i > 0 else 0,
        }


def select_c(date, n=5):
    cands = []
    for sym in day_stats[date]:
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


def simulate_trade(b, trail_arm=5.0, trail_drop=3.0, sl=-3.5, force_hms=131500):
    if not b:
        return 0.0, 0, "noop"
    entry_px = b[0][1]
    high_since = entry_px
    armed = False
    for hms, op, hi, lo, cl, _, _ in b[1:]:
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
    last = b[-1]
    return (last[4] / entry_px - 1) * 100, last[0], "eod"


# ---------- core run ----------
all_trades = []  # (date, sym, pnl, reason)
for d in all_dates:
    picks = select_c(d)
    for sym in picks:
        b = bars[sym][d]
        if not b:
            continue
        pnl, exit_t, reason = simulate_trade(b)
        all_trades.append((d, sym, pnl, reason))

print(f"\n{'='*100}")
print(f"STRATEGY C BREAKDOWN — {len(all_trades)} trades over {len(all_dates)} dates")
print(f"{'='*100}\n")

# 1. exit reason distribution
print("1. 청산 사유 분포")
reason_count = Counter(t[3] for t in all_trades)
reason_pnl = defaultdict(list)
for _, _, pnl, reason in all_trades:
    reason_pnl[reason].append(pnl)
print(f"  {'reason':10s} {'count':>5s} {'pct':>6s} {'avg_pnl':>9s} {'sum_pnl':>9s}")
for reason in ["trail", "sl", "force", "eod"]:
    cnt = reason_count.get(reason, 0)
    if cnt == 0:
        continue
    pct = cnt / len(all_trades) * 100
    avg = sum(reason_pnl[reason]) / cnt
    tot = sum(reason_pnl[reason])
    print(f"  {reason:10s} {cnt:>5d} {pct:>5.1f}% {avg:>+8.2f}% {tot:>+8.2f}%")
print()

# 2. per-symbol contribution
print("2. 종목별 contribution (top 15)")
sym_pnl = defaultdict(list)
for _, sym, pnl, _ in all_trades:
    sym_pnl[sym].append(pnl)
sym_summary = [(s, sum(p), len(p), sum(p)/len(p)) for s, p in sym_pnl.items()]
sym_summary.sort(key=lambda x: -x[1])
print(f"  {'sym':8s} {'n':>3s} {'sum':>9s} {'avg':>7s}")
for s, tot, n, avg in sym_summary[:15]:
    print(f"  {s:8s} {n:>3d} {tot:>+8.2f}% {avg:>+6.2f}%")
print(f"  ... ({len(sym_summary)} unique syms)")
print()

# 3. 픽업 빈도
print("3. 종목 픽업 빈도 (top 10)")
pick_count = Counter()
for d in all_dates:
    for s in select_c(d):
        pick_count[s] += 1
for s, c in pick_count.most_common(10):
    tot = sum(sym_pnl.get(s, [0]))
    print(f"  {s:8s} 픽업 {c:>2d}회 누적 {tot:+.2f}%")
print()

# 4. outlier 의존도
print("4. Outlier 의존도 — top 5 trade 빼면?")
sorted_pnls = sorted([t[2] for t in all_trades], reverse=True)
total = sum(sorted_pnls)
top5 = sum(sorted_pnls[:5])
top10 = sum(sorted_pnls[:10])
bot5 = sum(sorted_pnls[-5:])
print(f"  전체 합: {total:+.2f}%  ({len(sorted_pnls)} trades)")
print(f"  top 5 합: {top5:+.2f}% ({top5/total*100:.0f}% 기여)")
print(f"  top 10 합: {top10:+.2f}% ({top10/total*100:.0f}% 기여)")
print(f"  bot 5 합: {bot5:+.2f}%")
print(f"  top 5 빼면: {total - top5:+.2f}%")
print(f"  top 5 + bot 5 빼면: {total - top5 - bot5:+.2f}%")
print()

# 5. 청산 시각 분포
print("5. trail 청산이 발동한 시각 분포")
trail_times = []
for d, sym, pnl, reason in all_trades:
    if reason != "trail":
        continue
    b = bars[sym][d]
    _, exit_hms, _ = simulate_trade(b)
    trail_times.append(exit_hms)
if trail_times:
    bins = Counter()
    for t in trail_times:
        h = t // 10000
        bins[h] += 1
    for h in sorted(bins):
        print(f"  {h:02d}:00~ {bins[h]:>3d}건")
else:
    print("  trail 청산 0건")
print()

# 6. 다음날 보유 시뮬 — force exit 종목들이 다음날까지 들고 있으면?
print("6. 다음날 보유 시뮬레이션 — force/eod로 끝난 종목 다음날 9:00 시가 청산")
multi_day_pnls = []
multi_day_diff = []
for d, sym, pnl, reason in all_trades:
    if reason not in ("force", "eod"):
        multi_day_pnls.append(pnl)  # 그대로
        continue
    sym_dates = sorted(bars[sym].keys())
    if d not in sym_dates:
        multi_day_pnls.append(pnl)
        continue
    idx = sym_dates.index(d)
    if idx + 1 >= len(sym_dates):
        multi_day_pnls.append(pnl)  # 다음날 데이터 없음
        continue
    next_date = sym_dates[idx + 1]
    next_open = day_stats[next_date].get(sym, {}).get("open", 0)
    today_entry = bars[sym][d][0][1]
    if next_open <= 0 or today_entry <= 0:
        multi_day_pnls.append(pnl)
        continue
    new_pnl = (next_open / today_entry - 1) * 100
    # apply SL clamp at -3.5%
    if new_pnl < -3.5:
        new_pnl = -3.5
    multi_day_pnls.append(new_pnl)
    multi_day_diff.append((d, sym, pnl, new_pnl, new_pnl - pnl))

print(f"  당일 청산 총합: {sum(t[2] for t in all_trades):+.2f}%")
print(f"  다음날 시가 청산 총합: {sum(multi_day_pnls):+.2f}%")
print(f"  차이: {sum(multi_day_pnls) - sum(t[2] for t in all_trades):+.2f}%p")
better = sum(1 for d in multi_day_diff if d[4] > 0)
worse = sum(1 for d in multi_day_diff if d[4] < 0)
print(f"  다음날까지 보유가 더 나은 거래: {better}/{len(multi_day_diff)}")
print(f"  더 나쁜 거래: {worse}/{len(multi_day_diff)}")
print()

# 7. C 전략의 최대 trail이 얼마였나
print("7. 종목별 entry 시점 → 당일 최대 high% (potential 상한)")
max_pots = []
for d, sym, _, _ in all_trades:
    b = bars[sym][d]
    if not b:
        continue
    entry_px = b[0][1]
    max_h = max(x[2] for x in b)
    max_pot = (max_h / entry_px - 1) * 100
    max_pots.append(max_pot)
max_pots.sort(reverse=True)
print(f"  평균 max%: {sum(max_pots)/len(max_pots):.2f}%")
print(f"  중간값: {sorted(max_pots)[len(max_pots)//2]:.2f}%")
print(f"  >5% 도달: {sum(1 for x in max_pots if x>=5):>3d}/{len(max_pots)} ({sum(1 for x in max_pots if x>=5)*100//len(max_pots)}%)")
print(f"  >3% 도달: {sum(1 for x in max_pots if x>=3):>3d}/{len(max_pots)} ({sum(1 for x in max_pots if x>=3)*100//len(max_pots)}%)")
print(f"  >2% 도달: {sum(1 for x in max_pots if x>=2):>3d}/{len(max_pots)} ({sum(1 for x in max_pots if x>=2)*100//len(max_pots)}%)")
print(f"  >1% 도달: {sum(1 for x in max_pots if x>=1):>3d}/{len(max_pots)} ({sum(1 for x in max_pots if x>=1)*100//len(max_pots)}%)")
