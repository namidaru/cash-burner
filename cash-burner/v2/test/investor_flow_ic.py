"""
수급(외국인/기관) 시그널의 다음날 수익률 예측력 IC 테스트.

데이터: data/investor_cache.json (종목 × 30일)
       data/daily_ohlc_cache.json (종가)

Features (D 기준):
  - frgn_1d: D일 외인 순매수 금액 (절대)
  - frgn_qty_1d: D일 외인 순매수 수량 / 평균거래량 (정규화)
  - frgn_3d: 직전 3일 누적 외인 순매수
  - frgn_5d: 직전 5일 누적 외인 순매수
  - orgn_1d / 3d / 5d: 기관 동일
  - both_1d: 외인+기관 합계 1일
  - both_5d: 외인+기관 합계 5일
  - frgn_streak: 외인 연속 순매수 일수 (양수만)
  - orgn_streak: 기관 동일

Targets: D+1 close 수익률 (D 종가 → D+1 종가)
"""
import os, json, math
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/investor_cache.json", "r", encoding="utf-8") as f:
    inv = json.load(f)
with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    ohlc = json.load(f)

print(f"investor cache: {len(inv)} syms")

# 공통 종목
syms = sorted(set(inv.keys()) & set(ohlc.keys()))
print(f"intersect with ohlc: {len(syms)}")


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return 0
    rx_idx = sorted(range(n), key=lambda i: xs[i])
    ry_idx = sorted(range(n), key=lambda i: ys[i])
    rx = [0]*n; ry = [0]*n
    for r, i in enumerate(rx_idx): rx[i] = r
    for r, i in enumerate(ry_idx): ry[i] = r
    mx = (n-1)/2
    num = sum((rx[i]-mx)*(ry[i]-mx) for i in range(n))
    sx = math.sqrt(sum((rx[i]-mx)**2 for i in range(n)))
    sy = math.sqrt(sum((ry[i]-mx)**2 for i in range(n)))
    return num/(sx*sy) if sx > 0 and sy > 0 else 0


# 모든 날짜 수집
all_dates = set()
for sym in syms:
    all_dates.update(inv[sym].keys())
all_dates = sorted(all_dates)
print(f"dates: {len(all_dates)}  ({all_dates[0]}~{all_dates[-1]})")


def get_close(sym, d):
    return ohlc.get(sym, {}).get(d, {}).get("close", 0)


def avg_vol(sym, d, n=20):
    """직전 n일 평균 거래량."""
    days = sorted(ohlc.get(sym, {}).keys())
    if d not in days:
        return 0
    i = days.index(d)
    if i < n:
        return 0
    vols = [ohlc[sym][days[j]].get("vol", 0) for j in range(i-n, i)]
    return mean(vols) if vols else 0


def features_for_day(d, prev_dates):
    """d 종가 시점에서 관측 가능한 feature, target은 d+1 수익률."""
    out = {}
    di = all_dates.index(d) if d in all_dates else -1
    if di < 5 or di + 1 >= len(all_dates):
        return out
    d_next = all_dates[di+1]
    for sym in syms:
        sd = inv[sym]
        if d not in sd:
            continue
        c = get_close(sym, d)
        c_next = get_close(sym, d_next)
        if c <= 0 or c_next <= 0:
            continue
        # 외인/기관 데이터
        history = []
        for k in range(5):
            dk = all_dates[di-k]
            if dk in sd:
                history.append(sd[dk])
            else:
                break
        if len(history) < 5:
            continue
        # 정규화용 평균거래량
        avgv = avg_vol(sym, d, 20)
        if avgv <= 0:
            continue

        frgn_1d = history[0]["frgn_qty"]
        orgn_1d = history[0]["orgn_qty"]
        frgn_3d = sum(h["frgn_qty"] for h in history[:3])
        frgn_5d = sum(h["frgn_qty"] for h in history[:5])
        orgn_3d = sum(h["orgn_qty"] for h in history[:3])
        orgn_5d = sum(h["orgn_qty"] for h in history[:5])

        # streak
        f_streak = 0
        for h in history:
            if h["frgn_qty"] > 0: f_streak += 1
            else: break
        o_streak = 0
        for h in history:
            if h["orgn_qty"] > 0: o_streak += 1
            else: break

        out[sym] = {
            "fwd": (c_next/c - 1)*100,
            "frgn_1d_norm": frgn_1d / avgv,
            "frgn_3d_norm": frgn_3d / avgv,
            "frgn_5d_norm": frgn_5d / avgv,
            "orgn_1d_norm": orgn_1d / avgv,
            "orgn_3d_norm": orgn_3d / avgv,
            "orgn_5d_norm": orgn_5d / avgv,
            "both_1d_norm": (frgn_1d + orgn_1d) / avgv,
            "both_5d_norm": (frgn_5d + orgn_5d) / avgv,
            "frgn_streak": f_streak,
            "orgn_streak": o_streak,
            # 거래대금
            "frgn_tr_1d": history[0]["frgn_tr"],
            "orgn_tr_1d": history[0]["orgn_tr"],
            "frgn_tr_5d": sum(h["frgn_tr"] for h in history[:5]),
        }
    return out


FEATURES = ["frgn_1d_norm","frgn_3d_norm","frgn_5d_norm",
            "orgn_1d_norm","orgn_3d_norm","orgn_5d_norm",
            "both_1d_norm","both_5d_norm",
            "frgn_streak","orgn_streak",
            "frgn_tr_1d","orgn_tr_1d","frgn_tr_5d"]

ic_acc = {f: [] for f in FEATURES}
qspread_acc = {f: [] for f in FEATURES}

n_total = 0
n_days = 0
for d in all_dates:
    rows = features_for_day(d, all_dates)
    if len(rows) < 20:
        continue
    n_days += 1
    n_total += len(rows)
    syms_d = list(rows.keys())
    fwd = [rows[s]["fwd"] for s in syms_d]
    for f in FEATURES:
        xs = [rows[s][f] for s in syms_d]
        ic = spearman(xs, fwd)
        ic_acc[f].append(ic)
        # quintile
        n = len(xs)
        q = n // 5
        if q >= 4:
            sidx = sorted(range(n), key=lambda i: xs[i])
            bot = mean(fwd[i] for i in sidx[:q])
            top = mean(fwd[i] for i in sidx[-q:])
            qspread_acc[f].append(top - bot)

print(f"\n관측 일수: {n_days}, 총 (sym,day) 관측: {n_total}")

print(f"\n{'='*100}")
print(f"INVESTOR FLOW IC — 수급 시그널 → 다음날 수익률 예측력")
print(f"{'='*100}")
print(f"  {'feature':<18s} {'IC mean':>10s} {'IC std':>9s} {'t-stat':>8s} {'IC>0':>7s}  {'verdict'}")
results = []
for f in FEATURES:
    ics = ic_acc[f]
    if not ics:
        continue
    m = mean(ics)
    var = sum((x-m)**2 for x in ics)/max(len(ics)-1,1)
    sd = math.sqrt(var)
    t = m/(sd/math.sqrt(len(ics))) if sd > 0 else 0
    pos = sum(1 for x in ics if x > 0)
    if abs(m) < 0.02:
        v = "노이즈"
    elif abs(m) < 0.05:
        v = "약함"
    elif abs(m) < 0.10:
        v = "★ 진짜"
    else:
        v = "★★ 강함"
    results.append((f, m, t, v))
    print(f"  {f:<18s} {m:>+9.4f}  {sd:>8.4f}  {t:>+7.2f}  {pos:>3d}/{len(ics):<3d}  {v}")

print(f"\n{'='*100}")
print(f"QUINTILE SPREAD — 매일 상위20% - 하위20% 다음날 수익률")
print(f"{'='*100}")
print(f"  {'feature':<18s} {'spread%':>10s} {'t-stat':>8s}  {'verdict'}")
for f in FEATURES:
    ss = qspread_acc[f]
    if not ss:
        continue
    m = mean(ss)
    var = sum((x-m)**2 for x in ss)/max(len(ss)-1,1)
    sd = math.sqrt(var)
    t = m/(sd/math.sqrt(len(ss))) if sd > 0 else 0
    if abs(m) < 0.05:
        v = "0"
    elif abs(m) < 0.15:
        v = "약"
    elif abs(m) < 0.30:
        v = "★"
    else:
        v = "★★"
    print(f"  {f:<18s} {m:>+8.3f}%  {t:>+7.2f}  {v}")

# 절댓값 큰 순
print(f"\n>> 절댓값 IC 가장 큰 5개:")
results.sort(key=lambda x: -abs(x[1]))
for r in results[:5]:
    print(f"     {r[0]:<18s}  IC={r[1]:+.4f}  t={r[2]:+.2f}  {r[3]}")
