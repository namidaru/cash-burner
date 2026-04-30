"""백테스트 전략 (스크리닝 함수들).

각 전략은 select_fn(date, prices, hist_idx) -> list[sym] 시그니처.

주의: look-ahead bias 방지를 위해 date 시점까지의 데이터만 사용 (date 포함).
- 종가 기반 시그널 → date 종가 알고 있다고 가정하고 다음날 진입이면 OK.
- 본 엔진은 date 종가에 진입하므로 date까지 정보 사용 = 슬리피지에 흡수됨.

가설 1 (메인): low_vol_momentum
  - 60일 변동성 하위 30% ∩ 120일 모멘텀 상위 30% ∩ 일평균 거래대금 30억+
  - 한국 Low-Vol Anomaly + Momentum 결합 (Choi et al. 실증 sharpe ~1.2)

가설 2: breakout_volume
  - 종가가 60일 고점의 97%+ ∩ 5일 평균 거래량 / 60일 평균 거래량 > 2.0
  - 거래대금 30억+
  - 한국 모멘텀 / 신고가 추격

가설 3: blend (1+2 가중 합산)

필터 (BUYS에서 제거):
  - surge_filter_chg: 직전 1일 chg > 15% 종목 제외 (메모리 4/14 surge reversion)
  - surge_filter_5d: 직전 5일 chg > 30% 종목 제외 (오버슛)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _hist(df: pd.DataFrame, date: str, n: int) -> pd.DataFrame:
    """date 포함 가장 최근 n행."""
    sub = df.loc[df.index <= date]
    return sub.tail(n) if len(sub) >= n else pd.DataFrame()


def _vol_60d(df: pd.DataFrame, date: str, lookback: int = 60) -> float | None:
    h = _hist(df, date, lookback + 1)
    if len(h) < lookback + 1:
        return None
    rets = h["close"].pct_change().dropna()
    if len(rets) < lookback - 5:
        return None
    return float(rets.std())


def _momentum(df: pd.DataFrame, date: str, lookback: int = 120, skip: int = 5) -> float | None:
    """skip-1 모멘텀: t-lookback ~ t-skip 누적수익. 단기 reversal 회피."""
    h = _hist(df, date, lookback + 1)
    if len(h) < lookback + 1:
        return None
    if skip <= 0:
        end = h["close"].iloc[-1]
    else:
        if len(h) < skip + 1:
            return None
        end = h["close"].iloc[-skip - 1]
    start = h["close"].iloc[0]
    if start <= 0:
        return None
    return float(end / start - 1)


def _avg_value(df: pd.DataFrame, date: str, lookback: int = 60) -> float | None:
    h = _hist(df, date, lookback)
    if len(h) < max(20, lookback // 2):
        return None
    return float(h["value"].mean())


def _last_close(df: pd.DataFrame, date: str) -> float | None:
    sub = df.loc[df.index <= date]
    if sub.empty:
        return None
    v = sub["close"].iloc[-1]
    return float(v) if v > 0 else None


def _hh_ratio(df: pd.DataFrame, date: str, lookback: int = 60) -> float | None:
    """현재 종가 / lookback 일내 최고가."""
    h = _hist(df, date, lookback)
    if len(h) < lookback // 2:
        return None
    hh = h["high"].max()
    last = _last_close(df, date)
    if not hh or not last:
        return None
    return last / hh


def _vol_surge(df: pd.DataFrame, date: str, short: int = 5, long: int = 60) -> float | None:
    h = _hist(df, date, long)
    if len(h) < long * 0.7:
        return None
    short_v = h["volume"].tail(short).mean()
    long_v = h["volume"].mean()
    if long_v <= 0:
        return None
    return short_v / long_v


def _surge_chg_1d(df: pd.DataFrame, date: str) -> float | None:
    h = _hist(df, date, 2)
    if len(h) < 2:
        return None
    p0, p1 = h["close"].iloc[0], h["close"].iloc[-1]
    if p0 <= 0:
        return None
    return p1 / p0 - 1


def _surge_chg_5d(df: pd.DataFrame, date: str) -> float | None:
    h = _hist(df, date, 6)
    if len(h) < 6:
        return None
    p0, p1 = h["close"].iloc[0], h["close"].iloc[-1]
    if p0 <= 0:
        return None
    return p1 / p0 - 1


# ─── 전략 1: Low-Vol + Momentum ───
def make_low_vol_momentum(
    vol_lookback: int = 60,
    mom_lookback: int = 120,
    mom_skip: int = 5,
    value_lookback: int = 60,
    vol_pct: float = 0.30,        # 변동성 하위 30%
    mom_pct: float = 0.30,        # 모멘텀 상위 30%
    min_value: float = 3e9,       # 일평균 거래대금 30억
    topn: int = 20,
    surge_filter: bool = True,
):
    def select(date: str, prices, hist_idx: int) -> list[str]:
        rows = []
        for sym, df in prices.items():
            if df.empty or date < df.index.min():
                continue
            v = _avg_value(df, date, value_lookback)
            if v is None or v < min_value:
                continue
            vol = _vol_60d(df, date, vol_lookback)
            mom = _momentum(df, date, mom_lookback, mom_skip)
            if vol is None or mom is None:
                continue
            if surge_filter:
                c1 = _surge_chg_1d(df, date)
                c5 = _surge_chg_5d(df, date)
                if (c1 is not None and c1 > 0.15) or (c5 is not None and c5 > 0.30):
                    continue
            rows.append((sym, vol, mom, v))
        if not rows:
            return []
        df_r = pd.DataFrame(rows, columns=["sym", "vol", "mom", "value"])
        # 변동성 하위 cut
        vol_cut = df_r["vol"].quantile(vol_pct)
        df_r = df_r[df_r["vol"] <= vol_cut]
        if df_r.empty:
            return []
        # 모멘텀 상위 cut
        mom_cut = df_r["mom"].quantile(1 - mom_pct)
        df_r = df_r[df_r["mom"] >= mom_cut]
        if df_r.empty:
            return []
        # 결합 점수: 모멘텀 z - 변동성 z
        df_r["mom_z"] = (df_r["mom"] - df_r["mom"].mean()) / (df_r["mom"].std() + 1e-9)
        df_r["vol_z"] = (df_r["vol"] - df_r["vol"].mean()) / (df_r["vol"].std() + 1e-9)
        df_r["score"] = df_r["mom_z"] - df_r["vol_z"]
        return df_r.sort_values("score", ascending=False).head(topn)["sym"].tolist()
    return select


# ─── 전략 2: Breakout + Volume Surge ───
def make_breakout_volume(
    hh_lookback: int = 60,
    hh_threshold: float = 0.97,    # 60일 고점의 97%+
    vol_short: int = 5,
    vol_long: int = 60,
    vol_mult: float = 2.0,
    value_lookback: int = 60,
    min_value: float = 3e9,
    topn: int = 20,
    surge_filter: bool = True,
):
    def select(date: str, prices, hist_idx: int) -> list[str]:
        rows = []
        for sym, df in prices.items():
            if df.empty or date < df.index.min():
                continue
            v = _avg_value(df, date, value_lookback)
            if v is None or v < min_value:
                continue
            hhr = _hh_ratio(df, date, hh_lookback)
            vs = _vol_surge(df, date, vol_short, vol_long)
            if hhr is None or vs is None:
                continue
            if hhr < hh_threshold or vs < vol_mult:
                continue
            if surge_filter:
                c1 = _surge_chg_1d(df, date)
                if c1 is not None and c1 > 0.15:
                    continue
            mom = _momentum(df, date, 60, 1) or 0
            rows.append((sym, hhr, vs, mom, v))
        if not rows:
            return []
        df_r = pd.DataFrame(rows, columns=["sym", "hhr", "vs", "mom", "value"])
        df_r["score"] = (
            (df_r["hhr"] - df_r["hhr"].mean()) / (df_r["hhr"].std() + 1e-9)
            + (df_r["vs"] - df_r["vs"].mean()) / (df_r["vs"].std() + 1e-9)
            + (df_r["mom"] - df_r["mom"].mean()) / (df_r["mom"].std() + 1e-9)
        )
        return df_r.sort_values("score", ascending=False).head(topn)["sym"].tolist()
    return select


# ─── 전략 3: Blend ───
def make_blend(weight_lvm: float = 0.6, weight_bv: float = 0.4, topn: int = 20, **kwargs):
    """두 전략 점수 가중합. 상위 topn."""
    lvm = make_low_vol_momentum(topn=200, **{k: v for k, v in kwargs.items() if k in {
        "vol_lookback", "mom_lookback", "mom_skip", "min_value", "surge_filter"}})
    bv = make_breakout_volume(topn=200, **{k: v for k, v in kwargs.items() if k in {
        "hh_lookback", "vol_mult", "min_value", "surge_filter"}})

    def select(date, prices, hist_idx):
        a = lvm(date, prices, hist_idx)
        b = bv(date, prices, hist_idx)
        rank_a = {s: i for i, s in enumerate(a)}
        rank_b = {s: i for i, s in enumerate(b)}
        all_syms = set(a) | set(b)
        scored = []
        for s in all_syms:
            ra = rank_a.get(s, 200)
            rb = rank_b.get(s, 200)
            score = -(weight_lvm * ra + weight_bv * rb)
            scored.append((s, score))
        scored.sort(key=lambda x: -x[1])
        return [s for s, _ in scored[:topn]]
    return select


# ─── 베이스라인: 시총 동등가중 buy&hold (비교용) ───
def make_equal_weight_topvalue(topn: int = 20, value_lookback: int = 60, min_value: float = 3e9):
    """단순 거래대금 상위 N. 액티브 알파 없는 베이스라인."""
    def select(date, prices, hist_idx):
        rows = []
        for sym, df in prices.items():
            if df.empty or date < df.index.min():
                continue
            v = _avg_value(df, date, value_lookback)
            if v is None or v < min_value:
                continue
            rows.append((sym, v))
        rows.sort(key=lambda x: -x[1])
        return [s for s, _ in rows[:topn]]
    return select


STRATEGIES = {
    "low_vol_momentum": make_low_vol_momentum,
    "breakout_volume": make_breakout_volume,
    "blend": make_blend,
    "top_value": make_equal_weight_topvalue,
}
