"""
Vol-targeted leverage hold.
122630 buy&hold = +622% MDD -41%.
이걸 20일 변동성 측정해서 목표변동성(연15%)에 맞춰 포지션 사이즈 조절.
변동성 높을 때 → 비중 줄임, 낮을 때 → 비중 늘림(최대 1.0).

비교:
  - 122630 buy&hold (raw)
  - vol target 10/15/20% (cap 1.0)
  - vol target + cash buffer (over는 cash)
"""
import os, json, math
from statistics import mean

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

with open("data/daily_ohlc_cache.json", "r", encoding="utf-8") as f:
    cache = json.load(f)

LEV = "122630"
days = sorted(cache[LEV].keys())
closes = [cache[LEV][d]["close"] for d in days]

# daily return %
rets = [0.0]
for i in range(1, len(closes)):
    rets.append((closes[i]/closes[i-1] - 1) * 100)

WIN = 20  # 20일 표준편차
ANN = math.sqrt(252)

def rolling_vol_ann(i):
    """i 시점에서 직전 WIN일 변동성 (연환산 %)"""
    if i < WIN:
        return None
    window = rets[i-WIN+1:i+1]
    m = mean(window)
    var = sum((r-m)**2 for r in window) / (WIN - 1)
    return math.sqrt(var) * ANN  # daily% × √252 = annual%

def equity_curve(daily_rets):
    eq = 100.0
    peak = 100.0
    max_dd = 0.0
    curve = []
    for r in daily_rets:
        eq *= (1 + r/100)
        if eq > peak:
            peak = eq
        dd = (eq/peak - 1) * 100
        if dd < max_dd:
            max_dd = dd
        curve.append((eq, dd))
    return eq, max_dd, curve

def sharpe(daily_rets):
    if not daily_rets:
        return 0
    m = mean(daily_rets)
    var = sum((r-m)**2 for r in daily_rets) / max(len(daily_rets)-1, 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0
    return m / sd * math.sqrt(252)

def cagr(eq, n_days):
    if eq <= 0 or n_days <= 0:
        return 0
    yrs = n_days / 252
    return (eq/100) ** (1/yrs) * 100 - 100

# ----- 1. raw buy&hold -----
print(f"data: {len(rets)} days  ({days[0]} ~ {days[-1]})")
print(f"\n{'='*100}")
print(f"{'strategy':<30s} {'final':>10s} {'cagr':>8s} {'mdd':>9s} {'sharpe':>8s} {'avg_pos':>9s} {'turnover':>10s}")
print(f"{'='*100}")

eq, mdd, _ = equity_curve(rets[1:])
print(f"{'122630 raw buy&hold':<30s} {eq:>9.1f}  {cagr(eq, len(rets)-1):>+6.1f}%  {mdd:>+7.2f}%  {sharpe(rets[1:]):>+7.2f}  {1.0:>8.2f}  {0.0:>9.1f}")

# ----- 2. vol targeted -----
def vol_target_strategy(target_vol, max_pos=1.0, cost_pct=0.0):
    """매일 직전 20일 변동성으로 비중 결정 → 다음날 적용."""
    pos_yesterday = 0.0
    daily = []
    positions = []
    turnover = 0.0
    for i in range(len(rets)):
        v = rolling_vol_ann(i)
        if v is None or v == 0:
            pos = 0
        else:
            pos = min(target_vol / v, max_pos)
        # i 시점에서 결정한 pos를 다음날(i+1) return에 적용
        if i + 1 >= len(rets):
            break
        r_next = rets[i+1]
        # 거래비용: 비중 변경분만큼 차감
        turn = abs(pos - pos_yesterday)
        cost = turn * cost_pct
        daily.append(pos * r_next - cost)
        positions.append(pos)
        turnover += turn
        pos_yesterday = pos
    return daily, positions, turnover

for tv in [10, 12, 15, 18, 20, 25]:
    drets, poses, turn = vol_target_strategy(tv)
    eq, mdd, _ = equity_curve(drets)
    label = f"vol_target {tv}%"
    avg_p = mean(poses) if poses else 0
    print(f"{label:<30s} {eq:>9.1f}  {cagr(eq, len(drets)):>+6.1f}%  {mdd:>+7.2f}%  {sharpe(drets):>+7.2f}  {avg_p:>8.2f}  {turn:>9.1f}")

print()
print("[비용 차감 — 비중변화 1단위당 0.15% (편도)]")
for tv in [10, 12, 15, 18, 20]:
    drets, poses, turn = vol_target_strategy(tv, cost_pct=0.15)
    eq, mdd, _ = equity_curve(drets)
    label = f"vt {tv}% +cost"
    avg_p = mean(poses) if poses else 0
    print(f"{label:<30s} {eq:>9.1f}  {cagr(eq, len(drets)):>+6.1f}%  {mdd:>+7.2f}%  {sharpe(drets):>+7.2f}  {avg_p:>8.2f}  {turn:>9.1f}")

# ----- 3. vol target + leverage cap higher (비중 1.5까지 허용) -----
print()
print("[max_pos=1.5  — 저변동성 구간에 더 실음]")
for tv in [12, 15, 18]:
    drets, poses, turn = vol_target_strategy(tv, max_pos=1.5, cost_pct=0.15)
    eq, mdd, _ = equity_curve(drets)
    label = f"vt {tv}% cap1.5"
    avg_p = mean(poses) if poses else 0
    print(f"{label:<30s} {eq:>9.1f}  {cagr(eq, len(drets)):>+6.1f}%  {mdd:>+7.2f}%  {sharpe(drets):>+7.2f}  {avg_p:>8.2f}  {turn:>9.1f}")

# ----- 4. vol target + MA60 trend filter (위에서만 long) -----
print()
print("[vol target + MA60 trend filter]")
def vt_with_trend(target_vol, ma_n=60, max_pos=1.0, cost_pct=0.15):
    pos_yest = 0
    daily = []
    poses = []
    turn = 0
    for i in range(len(rets)):
        if i < max(WIN, ma_n):
            continue
        v = rolling_vol_ann(i)
        ma = mean(closes[i-ma_n+1:i+1])
        cur = closes[i]
        in_trend = cur > ma
        pos = 0
        if v and v > 0 and in_trend:
            pos = min(target_vol/v, max_pos)
        if i+1 >= len(rets):
            break
        r_next = rets[i+1]
        t = abs(pos - pos_yest)
        cost = t * cost_pct
        daily.append(pos * r_next - cost)
        poses.append(pos)
        turn += t
        pos_yest = pos
    return daily, poses, turn

for tv in [10, 15, 20]:
    drets, poses, turn = vt_with_trend(tv)
    eq, mdd, _ = equity_curve(drets)
    label = f"vt{tv}% +MA60"
    avg_p = mean(poses) if poses else 0
    print(f"{label:<30s} {eq:>9.1f}  {cagr(eq, len(drets)):>+6.1f}%  {mdd:>+7.2f}%  {sharpe(drets):>+7.2f}  {avg_p:>8.2f}  {turn:>9.1f}")

# ----- 5. 최악의 해 분석 -----
print()
print(f"{'='*100}")
print("연도별 비교 (raw vs vt15% +MA60)")
print(f"{'='*100}")
drets_vt, poses_vt, _ = vt_with_trend(15)
# vt_with_trend skips early days; align by using last len(drets_vt) days
offset = len(rets) - 1 - len(drets_vt)
year_raw = {}
year_vt = {}
for i, r in enumerate(rets[1:]):
    y = days[i+1][:4]
    year_raw.setdefault(y, []).append(r)
for i, r in enumerate(drets_vt):
    y = days[offset + 1 + i][:4]
    year_vt.setdefault(y, []).append(r)

print(f"  {'year':<6s} {'raw_ret':>10s} {'raw_mdd':>10s}  {'vt_ret':>10s} {'vt_mdd':>10s}")
for y in sorted(year_raw.keys()):
    r1 = year_raw[y]
    e1, d1, _ = equity_curve(r1)
    r2 = year_vt.get(y, [])
    e2, d2, _ = equity_curve(r2) if r2 else (100, 0, [])
    print(f"  {y:<6s} {e1-100:>+8.1f}%  {d1:>+8.2f}%  {e2-100:>+8.1f}%  {d2:>+8.2f}%")
