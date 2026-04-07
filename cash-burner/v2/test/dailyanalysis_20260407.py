"""4/7(화) 일일 분석 — v4 교집합 첫 실전."""
import sys, os, json, time, re
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "src")

DATE = "20260407"
LOG_DIR = f"data/logs/{DATE}"
LEDGER = "data/ledger_real.csv"
PREV_CLOSE_PATH = "data/prev_close.json"
SIGNAL_DIAG = f"{LOG_DIR}/signal_diag.log"
FASTPATH_DEBUG = f"{LOG_DIR}/fastpath_debug.log"
THEME_DEBUG = f"{LOG_DIR}/theme_debug.log"
WATCHLIST_DEBUG = f"{LOG_DIR}/watchlist_debug.log"
WS_PATH = f"{LOG_DIR}/ws_dump_compact.log"
if not os.path.exists(WS_PATH):
    WS_PATH = f"{LOG_DIR}/ws_dump.log"

ENTRY_MODE = "theme"

# prev_close
with open(PREV_CLOSE_PATH, "r", encoding="utf-8") as f:
    pcr = json.load(f)
prev_close = {}
for sym, v in pcr.items():
    prev_close[sym] = v.get("price", 0) if isinstance(v, dict) else float(v)

print(f"=== {DATE[:4]}-{DATE[4:6]}-{DATE[6:]} (화) 일일 분석 ===\n")

# ══════════════════════════════════════════════════════════════
# STEP 1: 데이터 수집
# ══════════════════════════════════════════════════════════════

# 1. ledger
buys = []
sells = []
if os.path.exists(LEDGER):
    with open(LEDGER, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",", 6)
            if len(parts) < 5: continue
            try: ts = float(parts[0])
            except: continue
            dt = time.strftime("%Y%m%d", time.localtime(ts))
            if dt != DATE: continue
            action = parts[1]
            sym = parts[2]
            qty = int(parts[3])
            price = float(parts[4])
            rest = parts[5] if len(parts) > 5 else ""
            if action == "BUY":
                buys.append({"ts": ts, "sym": sym, "qty": qty, "price": price, "detail": rest})
            elif action in ("SELL", "PARTIAL_SELL"):
                sells.append({"ts": ts, "sym": sym, "qty": qty, "price": price, "detail": rest})

# 2. 테마 풀 파싱
theme_pool = []
theme_details = {}  # sym -> {name, score, freq, themes, chg}
if os.path.exists(THEME_DEBUG):
    with open(THEME_DEBUG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "BUILD SELECTED:" in line:
                m = re.search(r"BUILD SELECTED:\s*\[(.+?)\]", line)
                if m:
                    theme_pool = [s.strip().strip("'") for s in m.group(1).split(",")]
            m = re.match(r".*RANK\s+(\d{6})\s+(\S+)\s+score=([\d.]+)\s+freq=(\d+)\s+themes=\[(.+?)\]\s+price=(\d+)\s+chg=([\d.+-]+)%", line)
            if m:
                sym = m.group(1)
                theme_details[sym] = {
                    "name": m.group(2), "score": float(m.group(3)),
                    "freq": int(m.group(4)), "themes": m.group(5),
                    "chg": float(m.group(7)),
                }

# 3. signal_diag — PREOPEN_DROP
preopen_drops = []
preopen_rank_detail = []
if os.path.exists(SIGNAL_DIAG):
    with open(SIGNAL_DIAG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "PREOPEN_DROP" in line:
                # parse: 375500 ba=2.1<5 | 023160 ba=2.7<5 ...
                m = re.search(r"PREOPEN_DROP.*?\|(.*)", line)
                if m:
                    parts = m.group(1).strip().split("|")
                    for p in parts:
                        p = p.strip()
                        sm = re.match(r"(\d{6})\s+(.*)", p)
                        if sm:
                            preopen_drops.append({"sym": sm.group(1), "reason": sm.group(2).strip()})
            if "PREOPEN_RANK_DETAIL" in line:
                preopen_rank_detail.append(line.strip())
            if "PREOPEN_RANK," in line and "whitelist" in line:
                preopen_rank_detail.append(line.strip())

# 4. watchlist_debug — INTERSECT + MIDDAY
intersect_syms = []
midday_new_syms = []
if os.path.exists(WATCHLIST_DEBUG):
    with open(WATCHLIST_DEBUG, "r", encoding="utf-8") as f:
        for line in f:
            if "INTERSECT" in line and "overlap=" in line and not intersect_syms:
                m = re.search(r"syms=\[(.+?)\]", line)
                if m:
                    intersect_syms = [s.strip().strip("'") for s in m.group(1).split(",")]
            if "MIDDAY_EXPAND" in line and "new=" in line and not midday_new_syms:
                m = re.search(r"new=\[(.+?)\]", line)
                if m:
                    midday_new_syms = [s.strip().strip("'") for s in m.group(1).split(",")]

# 5. ws_dump compact 파싱 — 장중 가격
all_syms_to_track = list(dict.fromkeys(intersect_syms + midday_new_syms + theme_pool[:45]))
watch_set = set(all_syms_to_track)

BASE = datetime(2026, 4, 7).timestamp()
price_data = defaultdict(list)

print("[데이터 로딩] ws_dump compact 파싱 중...")
cnt = 0
with open(WS_PATH, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        if "H0STCNT0" not in line:
            continue
        tab = line.find("\t")
        if tab < 0:
            continue
        ts_str = line[:tab]
        try:
            h, m_t, s = int(ts_str[11:13]), int(ts_str[14:16]), int(ts_str[17:19])
            epoch = BASE + h * 3600 + m_t * 60 + s
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
            if f0 in watch_set:
                try:
                    px = float(fields[i + 2])
                    if px > 0:
                        price_data[f0].append((epoch, px))
                        cnt += 1
                except:
                    pass
                j = i + 3
                while j < len(fields) and fields[j] != "":
                    j += 1
                i = j + 1
            else:
                i += 1

# 중복 제거 + 정렬
for sym in list(price_data.keys()):
    d = {}
    for ep, px in price_data[sym]:
        d[ep] = px
    price_data[sym] = sorted(d.items())

print(f"  체결 {cnt:,}건 / {len(price_data)}종목\n")

# ══════════════════════════════════════════════════════════════
# STEP 2: 종목선택 검증
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("[종목선택 검증 — 교집합 + PREOPEN]")
print("=" * 80)
print()

print(f"  테마 스캐너 풀: {len(theme_pool)}종목")
print(f"  교집합 (테마 ∩ 랭크): {len(intersect_syms)}종목")
print(f"    {intersect_syms}")
print()

# PREOPEN_DROP 분석
drop_reasons = defaultdict(list)
for d in preopen_drops:
    # 간단 분류
    reason = d["reason"]
    if "ba=" in reason:
        drop_reasons["ba<5"].append(d["sym"])
    elif "gap=" in reason:
        drop_reasons["gap<0.3"].append(d["sym"])
    elif "tr=" in reason:
        drop_reasons["tr<30"].append(d["sym"])
    else:
        drop_reasons["other"].append(d["sym"])

print(f"  PREOPEN 통과: 0종목 / 탈락: {len(preopen_drops)}종목")
print(f"  탈락 사유 분포:")
for reason, syms in sorted(drop_reasons.items()):
    print(f"    {reason}: {len(syms)}종목 — {syms}")
print()

# 탈락 종목 상세 + 장중 성과
print(f"  {'종목':>8s} {'이름':>10s}  {'탈락사유':>16s}  {'전종':>8s}  {'시가':>8s}  {'고가':>8s}  {'종가':>8s}  {'시→고':>7s}  {'시→종':>7s}")
print("  " + "-" * 100)

sym_perf = {}  # sym -> {open, high, low, close, oh%, oc%}
for sym in intersect_syms:
    prices = price_data.get(sym, [])
    pc = prev_close.get(sym, 0)
    td = theme_details.get(sym, {})
    name = td.get("name", "?")[:10]
    drop_reason = "?"
    for d in preopen_drops:
        if d["sym"] == sym:
            drop_reason = d["reason"]
            break

    if prices:
        o = prices[0][1]
        h = max(p for _, p in prices)
        l = min(p for _, p in prices)
        c = prices[-1][1]
        oh = (h / o - 1) * 100 if o > 0 else 0
        oc = (c / o - 1) * 100 if o > 0 else 0
        sym_perf[sym] = {"open": o, "high": h, "low": l, "close": c, "oh": oh, "oc": oc}
        print(f"  {sym:>8s} {name:>10s}  {drop_reason:>16s}  {pc:>8,.0f}  {o:>8,.0f}  {h:>8,.0f}  {c:>8,.0f}  {oh:>+6.1f}%  {oc:>+6.1f}%")
    else:
        print(f"  {sym:>8s} {name:>10s}  {drop_reason:>16s}  {pc:>8,.0f}  데이터 없음")

# 교집합 종목 시뮬: 시가 매수 → 어떻게 됐을까
print()
if sym_perf:
    avg_oh = sum(v["oh"] for v in sym_perf.values()) / len(sym_perf)
    avg_oc = sum(v["oc"] for v in sym_perf.values()) / len(sym_perf)
    winners = sum(1 for v in sym_perf.values() if v["oh"] > 1.5)
    losers = sum(1 for v in sym_perf.values() if v["oc"] < -1.0)
    print(f"  [교집합 시뮬 — 시가 매수 가정]")
    print(f"    종목 수: {len(sym_perf)}")
    print(f"    평균 시가→고가: {avg_oh:+.1f}%")
    print(f"    평균 시가→종가: {avg_oc:+.1f}%")
    print(f"    시→고 1.5%+: {winners}/{len(sym_perf)}")
    print(f"    시→종 -1.0%↓: {losers}/{len(sym_perf)}")
    print()
    # 상위 5 (시→고)
    top5 = sorted(sym_perf.items(), key=lambda x: x[1]["oh"], reverse=True)[:5]
    print(f"    시가→고가 TOP5:")
    for sym, v in top5:
        name = theme_details.get(sym, {}).get("name", "?")
        print(f"      {sym} {name}: 시가={v['open']:,.0f} 고가={v['high']:,.0f} → {v['oh']:+.1f}%")

print()

# ══════════════════════════════════════════════════════════════
# STEP 2B: 테마 풀 전체 비교
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("[종목선택 검증 — 테마 풀 전체 비교]")
print("=" * 80)
print()

theme_perf = {}
for sym in theme_pool:
    prices = price_data.get(sym, [])
    if not prices:
        continue
    o = prices[0][1]
    h = max(p for _, p in prices)
    l = min(p for _, p in prices)
    c = prices[-1][1]
    oh = (h / o - 1) * 100 if o > 0 else 0
    oc = (c / o - 1) * 100 if o > 0 else 0
    theme_perf[sym] = {"open": o, "high": h, "low": l, "close": c, "oh": oh, "oc": oc}

if theme_perf:
    intersect_set = set(intersect_syms)
    sel = {k: v for k, v in theme_perf.items() if k in intersect_set}
    not_sel = {k: v for k, v in theme_perf.items() if k not in intersect_set}

    sel_avg = sum(v["oh"] for v in sel.values()) / max(len(sel), 1)
    not_avg = sum(v["oh"] for v in not_sel.values()) / max(len(not_sel), 1)
    print(f"  교집합 종목({len(sel)}개) 평균 시→고: {sel_avg:+.1f}%")
    print(f"  비교집합 종목({len(not_sel)}개) 평균 시→고: {not_avg:+.1f}%")
    print(f"  교집합 우위: {sel_avg - not_avg:+.1f}%p")
    print()

    # 전체 TOP10 (시→고)
    all_sorted = sorted(theme_perf.items(), key=lambda x: x[1]["oh"], reverse=True)
    top10 = all_sorted[:10]
    top5_set = set(s for s, _ in all_sorted[:5])
    top10_set = set(s for s, _ in all_sorted[:10])
    print(f"  테마 풀 TOP10 (시→고):")
    for i, (sym, v) in enumerate(top10):
        td = theme_details.get(sym, {})
        name = td.get("name", "?")[:10]
        in_ix = "★" if sym in intersect_set else " "
        print(f"    {i+1:>2}. {sym} {name:>10s} {in_ix}  시가={v['open']:>8,.0f}  고가={v['high']:>8,.0f}  {v['oh']:>+6.1f}%")

    cap5 = len(intersect_set & top5_set)
    cap10 = len(intersect_set & top10_set)
    print(f"\n  Top5 Capture: {cap5}/5  Top10 Capture: {cap10}/10")

    # 놓친 최대 기회 (교집합 밖에서 가장 좋았던 것)
    missed = [(s, v) for s, v in all_sorted if s not in intersect_set]
    if missed:
        best = missed[0]
        td = theme_details.get(best[0], {})
        print(f"  놓친 최대 기회: {best[0]} {td.get('name','?')} (+{best[1]['oh']:.1f}%, 랭크 미포함)")
print()

# ══════════════════════════════════════════════════════════════
# STEP 3: 진입 실행
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("[진입 실행]")
print("=" * 80)
print()
print(f"  총 진입: {len(buys)}/11종목 (교집합)")
if buys:
    for b in buys:
        t = time.strftime("%H:%M:%S", time.localtime(b["ts"]))
        print(f"    {b['sym']}: {t} price={b['price']:,.0f} qty={b['qty']} {b['detail']}")
else:
    print(f"  *** PREOPEN이 교집합 11종목 전부 탈락 → whitelist=0 → 매수 없음 ***")
    print()
    print(f"  원인: 동시호가(H0STANC0) ba_ratio 기준 ba>=5 미달")
    print(f"    ba<5 탈락: {len(drop_reasons.get('ba<5',[]))}종목")
    print(f"    gap<0.3 탈락: {len(drop_reasons.get('gap<0.3',[]))}종목")
    print(f"    tr<30 탈락: {len(drop_reasons.get('tr<30',[]))}종목")
print()

# ══════════════════════════════════════════════════════════════
# STEP 4: 매도 성과
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("[매도 성과]")
print("=" * 80)
print()
if sells:
    for s in sells:
        t = time.strftime("%H:%M:%S", time.localtime(s["ts"]))
        print(f"  {s['sym']}: {t} price={s['price']:,.0f} qty={s['qty']} {s['detail']}")
else:
    print("  거래 없음")
print()

# ══════════════════════════════════════════════════════════════
# STEP 5: 눌림목 패턴 — 교집합 11종목
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("[눌림목 패턴 — 교집합 11종목]")
print("=" * 80)
print()

SIM_START = BASE + 9 * 3600
SIM_END = BASE + 15 * 3600 + 19 * 60
dip_summary = []

for sym in intersect_syms:
    prices = price_data.get(sym, [])
    if not prices:
        print(f"  {sym}: 데이터 없음")
        continue

    high_px = 0
    dips = []
    in_dip = False
    dip_start_high = 0
    dip_low = 0
    dip_low_ts = 0

    for ts, px in prices:
        if ts < SIM_START or ts > SIM_END:
            continue
        if px > high_px:
            high_px = px
            if in_dip:
                recovery = (px / dip_low - 1) * 100 if dip_low > 0 else 0
                drop_pct = (dip_low / dip_start_high - 1) * 100
                dips.append({
                    "high": dip_start_high, "low": dip_low,
                    "drop": drop_pct, "recovery": recovery,
                    "low_ts": dip_low_ts,
                })
                in_dip = False

        if high_px > 0:
            drop = (px / high_px - 1) * 100
            if drop <= -1.0:
                if not in_dip:
                    in_dip = True
                    dip_start_high = high_px
                    dip_low = px
                    dip_low_ts = ts
                elif px < dip_low:
                    dip_low = px
                    dip_low_ts = ts

    td = theme_details.get(sym, {})
    name = td.get("name", "?")[:10]
    if dips:
        print(f"  {sym} {name}: 눌림 {len(dips)}회")
        for i, d in enumerate(dips[:3]):
            lt = time.strftime("%H:%M", time.localtime(d["low_ts"]))
            print(f"    #{i+1}: 고점={d['high']:,.0f} → 저점={d['low']:,.0f} ({d['drop']:+.1f}%) "
                  f"→ 회복 {d['recovery']:+.1f}%  @{lt}")
        dip_summary.append((sym, len(dips)))
    else:
        print(f"  {sym} {name}: 눌림 0회")

if dip_summary:
    total_dips = sum(n for _, n in dip_summary)
    print(f"\n  요약: {len(dip_summary)}종목에서 총 {total_dips}회 눌림")
else:
    print("\n  눌림 0회")
print()

# ══════════════════════════════════════════════════════════════
# STEP 6: 현금 효율
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("[현금 효율]")
print("=" * 80)
print()
print("  총 자본: 1,005,993원")
print("  거래 없음 — 유휴 현금 100%")
print()

# ══════════════════════════════════════════════════════════════
# STEP 7: 종합 + PREOPEN 기준 문제 분석
# ══════════════════════════════════════════════════════════════
print("=" * 80)
print("[종합]")
print("=" * 80)
print()
print(f"  4/7 결과: 0건 거래, 손익 0원")
print()
print(f"  v4 교집합 첫 실전 — 스캐너 정상 동작, 교집합 11종목 발견")
print(f"  *** 문제: PREOPEN ba_ratio 게이트가 전부 차단 ***")
print()
print(f"  교집합 11종목 중:")

# 분석: ba 기준이 너무 높은 건지?
ba_drops = [(d["sym"], d["reason"]) for d in preopen_drops if "ba=" in d["reason"]]
if ba_drops:
    ba_vals = []
    for sym, reason in ba_drops:
        m = re.search(r"ba=([\d.]+)", reason)
        if m:
            ba_vals.append(float(m.group(1)))
    if ba_vals:
        print(f"    ba<5 탈락: {len(ba_vals)}종목 (ba 평균={sum(ba_vals)/len(ba_vals):.1f}, 최대={max(ba_vals):.1f})")

gap_drops = [(d["sym"], d["reason"]) for d in preopen_drops if "gap=" in d["reason"]]
if gap_drops:
    print(f"    gap<0.3% 탈락: {len(gap_drops)}종목")

tr_drops = [(d["sym"], d["reason"]) for d in preopen_drops if "tr=" in d["reason"]]
if tr_drops:
    print(f"    tr<30 탈락: {len(tr_drops)}종목")

print()
print("  시가 매수 가정 시뮬 (교집합):")
if sym_perf:
    for sym, v in sorted(sym_perf.items(), key=lambda x: x[1]["oh"], reverse=True):
        td = theme_details.get(sym, {})
        name = td.get("name", "?")[:10]
        verdict = "○" if v["oh"] > 1.5 else "✗"
        print(f"    {verdict} {sym} {name}: 시→고={v['oh']:+.1f}%  시→종={v['oc']:+.1f}%")

print()
print("  → ba_ratio 기준 ba>=5가 장전에 너무 엄격하여 교집합 전체 차단.")
print("  → v4는 교집합 종목을 무조건 매수 목적 → PREOPEN 게이트 완화 검토 필요.")
