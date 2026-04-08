"""
Intraday Information Coefficient — 분봉 시점 T의 feature가 T+5/T+15/T+30분 수익률 예측?

데이터: data/volume_profiles/{sym}.csv  (1분봉, ~29일 × ~592종목)
샘플: 일별로 수십만 (sym, minute) 관측치

Features (시점 T 종가까지 관측 가능):
  - mom5: 5분 모멘텀
  - mom10: 10분 모멘텀
  - mom30: 30분 모멘텀
  - vol_spike: 직전 1분 거래량 / 20분 평균
  - tr_spike: 거래대금 비율
  - vwap_dist: 시점 T 가격이 vwap에서 몇% 떨어졌나
  - breakout_high: 시점 T 종가가 당일 직전 고가 돌파했나 (0/1)
  - range_pos: 직전 1분의 close 위치 (low~high 정규화)
  - accel: mom5 - mom5_prev
  - mom5_x_vol: mom5 × log(vol_spike)

Targets:
  - fwd_5: T → T+5분 종가 수익률
  - fwd_15: T → T+15
  - fwd_30: T → T+30
"""
import os, json, math, csv
from collections import defaultdict
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

DIR = "data/volume_profiles"
syms = sorted([f[:-4] for f in os.listdir(DIR) if f.endswith(".csv")])
print(f"syms: {len(syms)}")


def load_sym(sym):
    """return list of dicts sorted by (date,time)"""
    rows = []
    with open(f"{DIR}/{sym}.csv", "r") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                rows.append({
                    "date": r["date"],
                    "time": r["time"],
                    "o": int(r["open"]),
                    "h": int(r["high"]),
                    "l": int(r["low"]),
                    "c": int(r["close"]),
                    "v": int(r["volume"]),
                    "tr": int(r["tr_pbmn"]),
                })
            except Exception:
                continue
    return rows


# group by date
def by_date(rows):
    out = defaultdict(list)
    for r in rows:
        out[r["date"]].append(r)
    for d in out:
        out[d].sort(key=lambda x: x["time"])
    return out


def spearman(xs, ys):
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


# Build dataset: for each (date, time) → for each sym → features + fwd returns
print("loading all syms...")
all_data = {}  # sym → date → list of bars
for i, sym in enumerate(syms):
    rows = load_sym(sym)
    if not rows:
        continue
    all_data[sym] = by_date(rows)

# collect dates
all_dates = sorted(set(d for v in all_data.values() for d in v))
print(f"dates: {len(all_dates)}  ({all_dates[0]}~{all_dates[-1]})")

# features at index i (need i >= 30 for history, i+30 < n for forward)
def compute_features_for_day(date):
    """For each sym present on date, build [(time, features, fwd_targets), ...]"""
    out = {}  # sym → list of (t_idx, features dict, target dict)
    for sym, dd in all_data.items():
        bars = dd.get(date)
        if not bars or len(bars) < 60:
            continue
        n = len(bars)
        rows = []
        # cumulative for vwap
        cum_pv = 0
        cum_v = 0
        running_high = 0
        for i, b in enumerate(bars):
            cum_pv += b["c"] * b["v"]
            cum_v += b["v"]
            if b["h"] > running_high:
                running_high = b["h"]
            if i < 30 or i + 30 >= n:
                continue
            c = b["c"]
            if c <= 0:
                continue
            c5 = bars[i-5]["c"]
            c10 = bars[i-10]["c"]
            c30 = bars[i-30]["c"]
            if c5 <= 0 or c10 <= 0 or c30 <= 0:
                continue
            mom5 = (c/c5 - 1)*100
            mom10 = (c/c10 - 1)*100
            mom30 = (c/c30 - 1)*100
            mom5_prev = (c5/bars[i-10]["c"] - 1)*100 if bars[i-10]["c"] > 0 else 0
            accel = mom5 - mom5_prev
            avg_v_20 = mean(bars[i-j]["v"] for j in range(1, 21))
            avg_tr_20 = mean(bars[i-j]["tr"] for j in range(1, 21))
            vol_spike = b["v"] / avg_v_20 if avg_v_20 > 0 else 1
            tr_spike = b["tr"] / avg_tr_20 if avg_tr_20 > 0 else 1
            vwap = cum_pv / cum_v if cum_v > 0 else c
            vwap_dist = (c/vwap - 1)*100 if vwap > 0 else 0
            # breakout: c > max(high[0..i-1])
            prev_high = max(bars[j]["h"] for j in range(0, i))
            breakout = 1.0 if c > prev_high else 0.0
            range_pos = (b["c"] - b["l"]) / (b["h"] - b["l"]) if b["h"] > b["l"] else 0.5
            mom5_x_vol = mom5 * math.log1p(max(vol_spike, 0))
            # forward
            c_f5 = bars[i+5]["c"]
            c_f15 = bars[i+15]["c"]
            c_f30 = bars[i+30]["c"]
            if c_f5 <= 0 or c_f15 <= 0 or c_f30 <= 0:
                continue
            fwd5 = (c_f5/c - 1)*100
            fwd15 = (c_f15/c - 1)*100
            fwd30 = (c_f30/c - 1)*100
            rows.append({
                "i": i,
                "feat": {
                    "mom5": mom5,
                    "mom10": mom10,
                    "mom30": mom30,
                    "accel": accel,
                    "vol_spike": vol_spike,
                    "tr_spike": tr_spike,
                    "vwap_dist": vwap_dist,
                    "breakout": breakout,
                    "range_pos": range_pos,
                    "mom5_x_vol": mom5_x_vol,
                },
                "fwd5": fwd5,
                "fwd15": fwd15,
                "fwd30": fwd30,
            })
        if rows:
            out[sym] = rows
    return out


FEATURES = ["mom5","mom10","mom30","accel","vol_spike","tr_spike",
            "vwap_dist","breakout","range_pos","mom5_x_vol"]
TARGETS = ["fwd5","fwd15","fwd30"]

# group by (date, time_index) → list of (sym, features, fwds)
# compute IC per (date, time_index)
ic_acc = {f: {t: [] for t in TARGETS} for f in FEATURES}
qspread_acc = {f: {t: [] for t in TARGETS} for f in FEATURES}

print("computing IC per minute...")
n_obs_total = 0
for di, date in enumerate(all_dates):
    day_data = compute_features_for_day(date)
    if not day_data:
        continue
    # group by time index i
    by_time = defaultdict(list)
    for sym, rows in day_data.items():
        for r in rows:
            by_time[r["i"]].append((sym, r))
    for ti, items in by_time.items():
        if len(items) < 30:
            continue
        n_obs_total += len(items)
        for f in FEATURES:
            xs = [r[1]["feat"][f] for r in items]
            for t in TARGETS:
                ys = [r[1][t] for r in items]
                ic = spearman(xs, ys)
                ic_acc[f][t].append(ic)
                # quintile
                n = len(xs)
                q = n // 5
                if q >= 5:
                    sorted_idx = sorted(range(n), key=lambda i: xs[i])
                    bot_y = mean(ys[i] for i in sorted_idx[:q])
                    top_y = mean(ys[i] for i in sorted_idx[-q:])
                    qspread_acc[f][t].append(top_y - bot_y)
    if (di+1) % 5 == 0:
        print(f"  {di+1}/{len(all_dates)}  obs={n_obs_total:,}")

print(f"\ntotal cross-sectional observations: {n_obs_total:,}")
print(f"daily IC samples: {len(ic_acc[FEATURES[0]][TARGETS[0]])}")

print(f"\n{'='*100}")
print(f"INTRADAY IC — 분봉 cross-sectional 다음 N분 예측력")
print(f"{'='*100}")
print(f"  {'feature':<15s} {'target':<6s} {'IC mean':>10s} {'IC std':>9s} {'t-stat':>8s}  {'verdict'}")
for f in FEATURES:
    for t in TARGETS:
        ics = ic_acc[f][t]
        if not ics:
            continue
        m = mean(ics)
        var = sum((x-m)**2 for x in ics)/max(len(ics)-1,1)
        sd = math.sqrt(var)
        ts = m/(sd/math.sqrt(len(ics))) if sd > 0 else 0
        v = "노이즈" if abs(m) < 0.02 else ("약함" if abs(m) < 0.05 else ("★ 진짜" if abs(m) < 0.10 else "★★ 강함"))
        print(f"  {f:<15s} {t:<6s} {m:>+9.4f}  {sd:>8.4f}  {ts:>+7.2f}  {v}")

print(f"\n{'='*100}")
print(f"QUINTILE SPREAD — 매분 상위20% - 하위20% 평균 다음수익률")
print(f"{'='*100}")
print(f"  {'feature':<15s} {'target':<6s} {'spread%':>10s} {'t-stat':>8s}  {'verdict'}")
for f in FEATURES:
    for t in TARGETS:
        ss = qspread_acc[f][t]
        if not ss:
            continue
        m = mean(ss)
        var = sum((x-m)**2 for x in ss)/max(len(ss)-1,1)
        sd = math.sqrt(var)
        ts = m/(sd/math.sqrt(len(ss))) if sd > 0 else 0
        v = "0" if abs(m) < 0.05 else ("약" if abs(m) < 0.10 else ("★" if abs(m) < 0.20 else "★★"))
        print(f"  {f:<15s} {t:<6s} {m:>+8.3f}%  {ts:>+7.2f}  {v}")

print(f"\n{'='*100}")
print("READING")
print(f"{'='*100}")
print("""
intraday IC > 0.05이면 분봉 단위 진짜 alpha 존재
quintile spread > 0.2%이면 비용 0.15% 차감 후에도 수익 가능
모두 < 0.03이면 'real-time 감지 가능' 가설 통계적으로 기각
""")
