"""
Information Coefficient test — 'next day return을 예측할 수 있는가'.

15개 feature × 593일 × ~592 syms 모든 관측치에 대해:
  - feature(D)와 return(D→D+1)의 cross-sectional spearman 상관 측정
  - 매일의 IC를 평균내고 t-stat 계산
  - IC > 0.05이면 alpha, IC > 0.1이면 강함, |IC| < 0.03이면 노이즈

또한 quintile test:
  - 매일 feature 상위 20%, 하위 20% 종목의 다음날 평균 수익률
  - 상위 - 하위 spread = long/short 가치
"""
import os, json, math
from collections import defaultdict
from statistics import mean, median

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)

# Build per-sym sorted day list with prev_close cached
sym_days = {}
for sym, days in cache.items():
    if not days:
        continue
    sd = sorted(days.keys())
    sym_days[sym] = sd

all_dates = sorted(set(d for sd in sym_days.values() for d in sd))
print(f"days: {len(all_dates)}  syms: {len(sym_days)}")


def get(sym, d, key):
    return cache.get(sym, {}).get(d, {}).get(key, 0)


def compute_features_for_day(d):
    """For each sym alive on day d, compute features observable at end of d
    + the next-day return (target).
    Returns: {sym: {feature_name: value, 'fwd_ret': value}}
    """
    out = {}
    for sym in sym_days:
        sd = sym_days[sym]
        if d not in sd:
            continue
        i = sd.index(d)
        if i + 1 >= len(sd):
            continue
        # need history for some features
        if i < 21:
            continue
        c = get(sym, d, "close")
        if c <= 0:
            continue
        # next day
        d_next = sd[i+1]
        c_next = get(sym, d_next, "close")
        o_next = get(sym, d_next, "open")
        if c_next <= 0:
            continue
        fwd_ret = (c_next/c - 1) * 100
        fwd_ret_co = (c_next/c - 1) * 100  # close to close
        fwd_ret_overnight = (o_next/c - 1) * 100  # close to next open

        # features
        h = get(sym, d, "high"); l = get(sym, d, "low"); o = get(sym, d, "open")
        v = get(sym, d, "vol"); tr = get(sym, d, "tr")
        prev_c = get(sym, sd[i-1], "close") if i > 0 else 0
        if prev_c <= 0 or h <= l or v <= 0:
            continue

        # ----- features -----
        ret_1d = (c/prev_c - 1) * 100
        ret_2d = (c/get(sym, sd[i-2], "close") - 1) * 100 if i >= 2 and get(sym, sd[i-2], "close") > 0 else 0
        ret_5d = (c/get(sym, sd[i-5], "close") - 1) * 100 if i >= 5 and get(sym, sd[i-5], "close") > 0 else 0
        ret_10d = (c/get(sym, sd[i-10], "close") - 1) * 100 if i >= 10 and get(sym, sd[i-10], "close") > 0 else 0
        ret_20d = (c/get(sym, sd[i-20], "close") - 1) * 100 if i >= 20 and get(sym, sd[i-20], "close") > 0 else 0

        # candle position (강세마감 척도)
        close_pos = (c - l) / (h - l)
        # gap from prev close
        gap = (o/prev_c - 1) * 100
        # range %
        rng_pct = (h - l) / prev_c * 100
        # volume features
        avg_vol_20 = mean(get(sym, sd[i-j], "vol") for j in range(1, 21))
        vol_ratio = v / avg_vol_20 if avg_vol_20 > 0 else 1
        avg_tr_20 = mean(get(sym, sd[i-j], "tr") for j in range(1, 21))
        tr_ratio = tr / avg_tr_20 if avg_tr_20 > 0 else 1
        # acceleration
        ret_5d_prev = (get(sym, sd[i-5], "close")/get(sym, sd[i-10], "close") - 1)*100 if i >= 10 and get(sym, sd[i-5], "close") > 0 and get(sym, sd[i-10], "close") > 0 else 0
        accel = ret_5d - ret_5d_prev
        # 6m momentum (about 120 days)
        ret_6m = (c/get(sym, sd[i-120], "close") - 1)*100 if i >= 120 and get(sym, sd[i-120], "close") > 0 else None
        ret_12m = (c/get(sym, sd[i-240], "close") - 1)*100 if i >= 240 and get(sym, sd[i-240], "close") > 0 else None

        out[sym] = {
            "fwd_ret": fwd_ret,
            "fwd_ret_overnight": fwd_ret_overnight,
            # features
            "ret_1d": ret_1d,
            "ret_2d": ret_2d,
            "ret_5d": ret_5d,
            "ret_10d": ret_10d,
            "ret_20d": ret_20d,
            "ret_6m": ret_6m if ret_6m is not None else 0,
            "ret_12m": ret_12m if ret_12m is not None else 0,
            "close_pos": close_pos,
            "gap": gap,
            "rng_pct": rng_pct,
            "vol_ratio": vol_ratio,
            "tr_ratio": tr_ratio,
            "accel": accel,
            # combined
            "strong_high_tr": close_pos * math.log1p(max(tr_ratio, 0)),
            "ret_1d_x_vol": ret_1d * math.log1p(max(vol_ratio, 0)),
        }
    return out


def spearman(xs, ys):
    """rank correlation."""
    n = len(xs)
    if n < 5:
        return 0
    rank_x = sorted(range(n), key=lambda i: xs[i])
    rank_y = sorted(range(n), key=lambda i: ys[i])
    rx = [0]*n; ry = [0]*n
    for r, i in enumerate(rank_x): rx[i] = r
    for r, i in enumerate(rank_y): ry[i] = r
    mx = (n-1)/2
    num = sum((rx[i]-mx)*(ry[i]-mx) for i in range(n))
    sx = math.sqrt(sum((rx[i]-mx)**2 for i in range(n)))
    sy = math.sqrt(sum((ry[i]-mx)**2 for i in range(n)))
    if sx == 0 or sy == 0:
        return 0
    return num / (sx*sy)


# ----- Loop over all days, collect daily IC for each feature -----
FEATURES = ["ret_1d","ret_2d","ret_5d","ret_10d","ret_20d","ret_6m","ret_12m",
            "close_pos","gap","rng_pct","vol_ratio","tr_ratio","accel",
            "strong_high_tr","ret_1d_x_vol"]

daily_ic = {f: [] for f in FEATURES}
quintile_data = {f: defaultdict(list) for f in FEATURES}  # f → {q: [fwd_rets]}

print("computing daily features + IC...")
for di, d in enumerate(all_dates):
    if di < 250:  # need 12m history
        continue
    rows = compute_features_for_day(d)
    if len(rows) < 30:
        continue
    fwd = [rows[s]["fwd_ret"] for s in rows]
    syms = list(rows.keys())
    for f in FEATURES:
        feat = [rows[s][f] for s in syms]
        ic = spearman(feat, fwd)
        daily_ic[f].append(ic)
        # quintile
        n = len(feat)
        sorted_idx = sorted(range(n), key=lambda i: feat[i])
        q = n // 5
        if q < 3:
            continue
        # bottom Q1, top Q5
        bot = [fwd[i] for i in sorted_idx[:q]]
        top = [fwd[i] for i in sorted_idx[-q:]]
        quintile_data[f]["Q1"].append(mean(bot))
        quintile_data[f]["Q5"].append(mean(top))

# ----- Report -----
print(f"\n{'='*100}")
print(f"INFORMATION COEFFICIENT — 다음날(D+1) 수익률 예측력")
print(f"{'='*100}")
print(f"  관측 일수: {len(daily_ic[FEATURES[0]])}")
print(f"\n  {'feature':<20s} {'IC mean':>10s} {'IC stdev':>10s} {'t-stat':>8s} {'IC>0 days':>12s}  {'verdict'}")

results = []
for f in FEATURES:
    ics = daily_ic[f]
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
    results.append((f, m, sd, t, pos, v))
    print(f"  {f:<20s} {m:>+9.4f}  {sd:>9.4f}  {t:>+7.2f}  {pos:>5d}/{len(ics):<5d}  {v}")

# Best feature
results.sort(key=lambda x: -abs(x[1]))
print(f"\n  >> 절댓값 IC 가장 큰 5개:")
for r in results[:5]:
    print(f"     {r[0]:<20s} IC={r[1]:+.4f}  t={r[3]:+.2f}  {r[5]}")

# ----- Quintile test (long top - short bottom) -----
print(f"\n{'='*100}")
print(f"QUINTILE TEST — 매일 상위 20% (Q5) - 하위 20% (Q1) 다음날 수익률")
print(f"{'='*100}")
print(f"  {'feature':<20s} {'Q1 avg':>10s} {'Q5 avg':>10s} {'Q5-Q1':>10s} {'t-stat':>8s} {'verdict'}")
for f in FEATURES:
    q1 = quintile_data[f]["Q1"]
    q5 = quintile_data[f]["Q5"]
    if not q1 or not q5:
        continue
    m1 = mean(q1); m5 = mean(q5)
    spread = [q5[i] - q1[i] for i in range(min(len(q1), len(q5)))]
    msp = mean(spread)
    var = sum((x-msp)**2 for x in spread)/max(len(spread)-1,1)
    sd = math.sqrt(var)
    t = msp/(sd/math.sqrt(len(spread))) if sd > 0 else 0
    if abs(msp) < 0.05:
        v = "0"
    elif abs(msp) < 0.10:
        v = "약"
    elif abs(msp) < 0.20:
        v = "★"
    else:
        v = "★★"
    print(f"  {f:<20s} {m1:>+8.3f}%  {m5:>+8.3f}%  {msp:>+8.3f}%  {t:>+7.2f}  {v}")

# ----- 결론 -----
print(f"\n{'='*100}")
print("READING")
print(f"{'='*100}")
print("""
IC 해석:
  |IC| < 0.02 → 완전 노이즈, 예측력 없음
  |IC| 0.02~0.05 → 약한 시그널 (비용 차감 후 무의미)
  |IC| 0.05~0.10 → 진짜 alpha (실전 가능)
  |IC| > 0.10 → 강한 alpha (헤지펀드 수준)

|t-stat| > 2이면 통계적으로 유의 (random ≠ 진짜)

Q5-Q1 spread:
  실제 long/short 포트폴리오의 일평균 수익률
  비용 0.3%/일 차감하면 실전 수익. 0.3%보다 작으면 거래시 손해.
""")
