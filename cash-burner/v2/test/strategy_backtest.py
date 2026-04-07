"""진입 전략 백테스트 — 전 날짜 ws_dump 기반.

전략:
  A. 시가 즉시매수 (09:00 첫 체결)
  B. 1분 확인 (09:01까지 시가 이상 유지 시 매수)
  C. 2분 확인 (09:02까지 시가 이상 유지 시 매수)
  D. 시가+1% 돌파 시 매수 (09:00~09:10)
  E. 눌림목 매수 (고점 -1% 후 +0.5% 회복)

공통 청산:
  - trail: arm +5%, drop 3%
  - SL: -3.5%
  - 장마감: 15:19 강제청산
"""
import sys, os, time, re, json
from collections import defaultdict
from datetime import datetime

LOG_BASE = "data/logs"
DATES = []
for d in sorted(os.listdir(LOG_BASE)):
    if len(d) == 8 and d.isdigit():
        ws_c = os.path.join(LOG_BASE, d, "ws_dump_compact.log")
        ws_r = os.path.join(LOG_BASE, d, "ws_dump.log")
        sig = os.path.join(LOG_BASE, d, "signal_diag.log")
        if (os.path.exists(ws_c) or os.path.exists(ws_r)) and os.path.exists(sig):
            DATES.append(d)

print(f"백테스트 대상: {len(DATES)}일 — {DATES}")
print()

# ── 날짜별 whitelist + watchlist 파싱 ──
def get_stocks_for_date(date_str):
    """signal_diag에서 whitelist 파싱. 없으면 watchlist_debug에서 워치리스트."""
    sig = os.path.join(LOG_BASE, date_str, "signal_diag.log")
    stocks = []
    # PREOPEN_RANK에서 whitelist
    with open(sig, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "PREOPEN_RANK,whitelist" in line:
                # whitelist20=[046970(80) | 033790(51) ...] 형태
                m = re.search(r"whitelist\d+=\[(.+?)\]", line)
                if m:
                    for item in m.group(1).split("|"):
                        sm = re.match(r"\s*(\d{6})", item.strip())
                        if sm:
                            stocks.append(sm.group(1))
                break

    # whitelist 없으면 watchlist_debug에서 SCAN wrote 또는 INTERSECT
    if not stocks:
        wl_dbg = os.path.join(LOG_BASE, date_str, "watchlist_debug.log")
        if os.path.exists(wl_dbg):
            with open(wl_dbg, "r", encoding="utf-8") as f:
                for line in f:
                    if "INTERSECT" in line and "syms=" in line:
                        m = re.search(r"syms=\[(.+?)\]", line)
                        if m:
                            stocks = [s.strip().strip("'") for s in m.group(1).split(",")]
                        break
                    if "SCAN wrote" in line and not stocks:
                        pass  # fallback

    # 테마 풀도 수집 (비교용)
    theme_pool = []
    theme_dbg = os.path.join(LOG_BASE, date_str, "theme_debug.log")
    if os.path.exists(theme_dbg):
        with open(theme_dbg, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "BUILD SELECTED:" in line:
                    m = re.search(r"BUILD SELECTED:\s*\[(.+?)\]", line)
                    if m:
                        theme_pool = [s.strip().strip("'") for s in m.group(1).split(",")]
                    break

    return stocks, theme_pool


def parse_prices(date_str, syms_set):
    """ws_dump compact에서 H0STCNT0 체결 데이터 파싱."""
    ws_c = os.path.join(LOG_BASE, date_str, "ws_dump_compact.log")
    ws_r = os.path.join(LOG_BASE, date_str, "ws_dump.log")
    ws_path = ws_c if os.path.exists(ws_c) else ws_r

    y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:])
    BASE = datetime(y, m, d).timestamp()

    price_data = defaultdict(list)
    with open(ws_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "H0STCNT0" not in line:
                continue
            tab = line.find("\t")
            if tab < 0:
                continue
            ts_str = line[:tab]
            try:
                hh, mm, ss = int(ts_str[11:13]), int(ts_str[14:16]), int(ts_str[17:19])
                epoch = BASE + hh * 3600 + mm * 60 + ss
            except:
                continue
            body = line[tab + 1:]
            pipes = body.split("|", 3)
            if len(pipes) < 4 or pipes[1] != "H0STCNT0":
                continue
            fields = pipes[3].split("^")
            i = 0
            while i < len(fields) - 2:
                f0 = fields[i]
                if f0 in syms_set:
                    try:
                        px = float(fields[i + 2])
                        if px > 0:
                            price_data[f0].append((epoch, px))
                    except:
                        pass
                    j = i + 3
                    while j < len(fields) and fields[j] != "":
                        j += 1
                    i = j + 1
                else:
                    i += 1

    # 중복 제거 + 정렬
    result = {}
    for sym in price_data:
        d = {}
        for ep, px in price_data[sym]:
            d[ep] = px
        result[sym] = sorted(d.items())
    return result


def simulate_exit(prices, entry_idx, entry_px, base_ts):
    """trail(5/3) + SL(3.5%) + 장마감(15:19) 청산 시뮬."""
    SL_PCT = 3.5
    TRAIL_ARM_PCT = 5.0
    TRAIL_DROP_PCT = 3.0
    FORCE_EXIT = base_ts + 15 * 3600 + 19 * 60

    peak = entry_px
    armed = False
    arm_px = entry_px * (1 + TRAIL_ARM_PCT / 100)
    sl_px = entry_px * (1 - SL_PCT / 100)

    for i in range(entry_idx + 1, len(prices)):
        ts, px = prices[i]
        if ts > FORCE_EXIT:
            return px, ts, "force_exit", (px / entry_px - 1) * 100

        # SL
        if px <= sl_px:
            return px, ts, f"SL_{SL_PCT}", (px / entry_px - 1) * 100

        # trail
        if px > peak:
            peak = px
        if not armed and px >= arm_px:
            armed = True
        if armed:
            drop_from_peak = (peak - px) / peak * 100
            if drop_from_peak >= TRAIL_DROP_PCT:
                return px, ts, "trail", (px / entry_px - 1) * 100

    # 데이터 끝
    last_px = prices[-1][1]
    return last_px, prices[-1][0], "eod", (last_px / entry_px - 1) * 100


def strategy_A_open(prices, base_ts):
    """시가 즉시매수 (09:00 첫 체결)."""
    market_open = base_ts + 9 * 3600
    for i, (ts, px) in enumerate(prices):
        if ts >= market_open:
            return px, i, ts
    return None, None, None


def strategy_B_wait1m(prices, base_ts):
    """1분 확인: 09:01에 시가 이상이면 매수."""
    market_open = base_ts + 9 * 3600
    t_check = market_open + 60

    open_px = None
    for ts, px in prices:
        if ts >= market_open:
            open_px = px
            break
    if open_px is None:
        return None, None, None

    for i, (ts, px) in enumerate(prices):
        if ts >= t_check:
            if px >= open_px:
                return px, i, ts
            else:
                return None, None, None  # 시가 이하 → 패스
    return None, None, None


def strategy_C_wait2m(prices, base_ts):
    """2분 확인: 09:02에 시가 이상이면 매수."""
    market_open = base_ts + 9 * 3600
    t_check = market_open + 120

    open_px = None
    for ts, px in prices:
        if ts >= market_open:
            open_px = px
            break
    if open_px is None:
        return None, None, None

    for i, (ts, px) in enumerate(prices):
        if ts >= t_check:
            if px >= open_px:
                return px, i, ts
            else:
                return None, None, None
    return None, None, None


def strategy_D_breakout(prices, base_ts):
    """시가+1% 돌파 시 매수 (09:00~09:10)."""
    market_open = base_ts + 9 * 3600
    deadline = market_open + 10 * 60

    open_px = None
    for ts, px in prices:
        if ts >= market_open:
            open_px = px
            break
    if open_px is None:
        return None, None, None

    target = open_px * 1.01
    for i, (ts, px) in enumerate(prices):
        if ts < market_open:
            continue
        if ts > deadline:
            return None, None, None
        if px >= target:
            return px, i, ts
    return None, None, None


def strategy_E_dip(prices, base_ts):
    """눌림목: 시가 후 고점 -1% 하락 → +0.5% 회복 시 매수 (09:00~09:30)."""
    market_open = base_ts + 9 * 3600
    deadline = market_open + 30 * 60

    high_px = 0
    in_dip = False
    dip_low = 0

    for i, (ts, px) in enumerate(prices):
        if ts < market_open:
            continue
        if ts > deadline:
            return None, None, None

        if px > high_px:
            high_px = px
            if in_dip and dip_low > 0:
                recovery = (px / dip_low - 1) * 100
                if recovery >= 0.5:
                    return px, i, ts
        if high_px > 0:
            drop = (px / high_px - 1) * 100
            if drop <= -1.0:
                if not in_dip:
                    in_dip = True
                    dip_low = px
                elif px < dip_low:
                    dip_low = px
    return None, None, None


# ── 메인 백테스트 ──
strategies = {
    "A_시가즉시": strategy_A_open,
    "B_1분확인": strategy_B_wait1m,
    "C_2분확인": strategy_C_wait2m,
    "D_1%돌파": strategy_D_breakout,
    "E_눌림목": strategy_E_dip,
}

results = {name: [] for name in strategies}
per_date = {name: {} for name in strategies}

for date_str in DATES:
    print(f"--- {date_str} ---")
    stocks, theme_pool = get_stocks_for_date(date_str)
    if not stocks:
        print(f"  종목 없음, 스킵")
        continue

    # 최대 10종목만 (너무 많으면 파싱 느림)
    test_syms = stocks[:10]
    all_syms = set(test_syms)
    if theme_pool:
        # 테마풀 상위 20도 추가 (비교용)
        all_syms |= set(theme_pool[:20])

    print(f"  whitelist={test_syms[:5]}  파싱 중...")
    price_data = parse_prices(date_str, all_syms)
    print(f"  체결 데이터: {len(price_data)}종목")

    y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:])
    base_ts = datetime(y, m, d).timestamp()

    for strat_name, strat_fn in strategies.items():
        day_pnls = []
        day_trades = []
        for sym in test_syms:
            if sym not in price_data or len(price_data[sym]) < 10:
                continue
            prices = price_data[sym]

            entry_px, entry_idx, entry_ts = strat_fn(prices, base_ts)
            if entry_px is None:
                continue

            exit_px, exit_ts, exit_reason, pnl_pct = simulate_exit(
                prices, entry_idx, entry_px, base_ts
            )

            # 최대수익
            max_px = max(px for _, px in prices[entry_idx:])
            max_pct = (max_px / entry_px - 1) * 100

            day_pnls.append(pnl_pct)
            day_trades.append({
                "sym": sym, "entry": entry_px, "exit": exit_px,
                "pnl": pnl_pct, "max": max_pct, "reason": exit_reason,
                "entry_t": time.strftime("%H:%M:%S", time.localtime(entry_ts)),
                "exit_t": time.strftime("%H:%M:%S", time.localtime(exit_ts)),
            })
            results[strat_name].append(pnl_pct)

        per_date[strat_name][date_str] = day_trades
        if day_trades:
            avg = sum(day_pnls) / len(day_pnls)
            wins = sum(1 for p in day_pnls if p > 0)
            print(f"  {strat_name}: {len(day_trades)}건  avg={avg:+.2f}%  "
                  f"승률={wins}/{len(day_trades)}")

    print()

# ══════════════════════════════════════════════════════════════
# 종합 결과
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("전략별 종합 성과")
print("=" * 80)
print()
print(f"  {'전략':<12s}  {'건수':>4s}  {'평균PnL':>8s}  {'승률':>8s}  {'총PnL':>8s}  {'최대승':>7s}  {'최대패':>7s}  {'SL비율':>6s}")
print("  " + "-" * 70)

for name in strategies:
    pnls = results[name]
    if not pnls:
        print(f"  {name:<12s}  데이터 없음")
        continue
    n = len(pnls)
    avg = sum(pnls) / n
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    mx = max(pnls)
    mn = min(pnls)
    sl_count = 0
    for date_str in per_date[name]:
        for t in per_date[name][date_str]:
            if "SL" in t["reason"]:
                sl_count += 1
    print(f"  {name:<12s}  {n:>4d}  {avg:>+7.2f}%  {wins}/{n:>2d} {wins/n*100:4.0f}%  {total:>+7.1f}%  {mx:>+6.1f}%  {mn:>+6.1f}%  {sl_count}/{n}")

print()

# ── 날짜별 상세 (전략 A vs B) ──
print("=" * 80)
print("날짜별 상세 — A(시가즉시) vs B(1분확인) vs D(1%돌파)")
print("=" * 80)
for date_str in DATES:
    trades_a = per_date["A_시가즉시"].get(date_str, [])
    trades_b = per_date["B_1분확인"].get(date_str, [])
    trades_d = per_date["D_1%돌파"].get(date_str, [])
    if not trades_a and not trades_b:
        continue
    print(f"\n  {date_str}:")
    all_syms_day = set()
    for t in trades_a + trades_b + trades_d:
        all_syms_day.add(t["sym"])
    for sym in sorted(all_syms_day):
        a = next((t for t in trades_a if t["sym"] == sym), None)
        b = next((t for t in trades_b if t["sym"] == sym), None)
        d_t = next((t for t in trades_d if t["sym"] == sym), None)
        a_str = f"A={a['pnl']:+.1f}%({a['reason'][:5]})" if a else "A=skip"
        b_str = f"B={b['pnl']:+.1f}%({b['reason'][:5]})" if b else "B=skip"
        d_str = f"D={d_t['pnl']:+.1f}%({d_t['reason'][:5]})" if d_t else "D=skip"
        max_str = f"max={a['max']:+.1f}%" if a else ""
        print(f"    {sym}  {a_str:<20s}  {b_str:<20s}  {d_str:<20s}  {max_str}")
