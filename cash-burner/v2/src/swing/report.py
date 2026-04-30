"""백테스트 결과 분석/리포트.

compute_metrics(equity) -> dict
yearly_breakdown(equity) -> DataFrame
print_report(result) -> stdout
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_metrics(equity: pd.Series) -> dict:
    if len(equity) < 2:
        return {}
    eq = equity.copy()
    eq.index = pd.to_datetime(eq.index)
    rets = eq.pct_change().dropna()
    n_days = len(eq)
    n_years = n_days / 252.0
    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1 / max(n_years, 1e-6)) - 1)
    vol = float(rets.std() * np.sqrt(252))
    sharpe = float(rets.mean() / (rets.std() + 1e-12) * np.sqrt(252))
    cummax = eq.cummax()
    dd = eq / cummax - 1
    mdd = float(dd.min())
    win = float((rets > 0).mean())
    return {
        "n_days": n_days,
        "n_years": round(n_years, 2),
        "total_return": total_ret,
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "mdd": mdd,
        "win_rate": win,
        "final_equity": float(eq.iloc[-1]),
    }


def yearly_breakdown(equity: pd.Series) -> pd.DataFrame:
    if len(equity) < 2:
        return pd.DataFrame()
    eq = equity.copy()
    eq.index = pd.to_datetime(eq.index)
    rets = eq.pct_change().dropna()
    by_y = rets.groupby(rets.index.year)
    rows = []
    for y, r in by_y:
        eq_y = eq[eq.index.year == y]
        if len(eq_y) < 2:
            continue
        ret = float(eq_y.iloc[-1] / eq_y.iloc[0] - 1)
        sharpe = float(r.mean() / (r.std() + 1e-12) * np.sqrt(252)) if len(r) > 5 else float("nan")
        cummax = eq_y.cummax()
        mdd = float((eq_y / cummax - 1).min())
        rows.append({"year": y, "return": ret, "sharpe": sharpe, "mdd": mdd, "n_days": len(eq_y)})
    return pd.DataFrame(rows).set_index("year")


def turnover_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"total_trades": 0, "buy_value": 0, "sell_value": 0, "total_cost": 0}
    buys = trades[trades["side"] == "BUY"]
    sells = trades[trades["side"] == "SELL"]
    return {
        "total_trades": len(trades),
        "n_buys": len(buys),
        "n_sells": len(sells),
        "buy_value": float(buys["value"].sum()),
        "sell_value": float(sells["value"].sum()),
        "total_cost": float(trades["cost"].sum()),
    }


def print_report(result, label: str = "") -> None:
    eq = result.equity
    m = compute_metrics(eq)
    yr = yearly_breakdown(eq)
    tov = turnover_stats(result.trades)

    print(f"\n=== Backtest Report {label} ===")
    print(f"기간: {eq.index[0].date()} ~ {eq.index[-1].date()} ({m['n_days']}일, {m['n_years']}년)")
    print(f"초기자본: {result.initial_cash:,.0f}원  →  최종: {m['final_equity']:,.0f}원")
    print(f"총수익률: {m['total_return']*100:>7.2f}%   CAGR: {m['cagr']*100:>6.2f}%")
    print(f"변동성:   {m['vol']*100:>7.2f}%   Sharpe: {m['sharpe']:>5.2f}")
    print(f"MDD:      {m['mdd']*100:>7.2f}%   일승률: {m['win_rate']*100:>5.1f}%")
    print(f"리밸런싱: {len(result.rebalance_dates)}회")
    print(f"거래:     {tov['n_buys']} BUY / {tov['n_sells']} SELL, "
          f"매수금액 {tov['buy_value']/1e8:.1f}억, 비용 {tov['total_cost']/1e6:.1f}백만")
    if not yr.empty:
        print("\n연도별:")
        for y, row in yr.iterrows():
            print(f"  {y}: {row['return']*100:>+7.2f}%  sharpe {row['sharpe']:>+5.2f}  "
                  f"mdd {row['mdd']*100:>+7.2f}%  ({int(row['n_days'])}일)")
    print()


def save_report(result, out_dir: str, label: str = "run") -> None:
    """equity/trades CSV + metrics JSON 저장."""
    import json
    from pathlib import Path
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    result.equity.to_csv(p / f"{label}_equity.csv", header=["equity"])
    result.trades.to_csv(p / f"{label}_trades.csv", index=False)
    metrics = {
        **compute_metrics(result.equity),
        **turnover_stats(result.trades),
        "yearly": yearly_breakdown(result.equity).reset_index().to_dict(orient="records"),
    }
    (p / f"{label}_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"  저장: {p}/{label}_*")
