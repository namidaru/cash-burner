#!/usr/bin/env python3
"""스윙 백테스트 CLI.

사용법:
  # 1) 데이터 수집 (최초 1회 + 주기적 업데이트)
  python run_swing_bt.py --update --start 20180101 --syms-from cached
  python run_swing_bt.py --update --syms 005930 000660 035420

  # 2) 백테스트 실행
  python run_swing_bt.py --strategy low_vol_momentum --start 20200101 --end 20260101
  python run_swing_bt.py --strategy breakout_volume --start 20200101 --end 20260101 --rebalance W
  python run_swing_bt.py --strategy blend --topn 15 --report-out data/swing_results

  # 3) 여러 전략 한 번에 비교 (top_value는 베이스라인)
  python run_swing_bt.py --strategy all --start 20200101 --end 20260101

옵션:
  --strategy {low_vol_momentum, breakout_volume, blend, top_value, all}
  --start / --end          YYYYMMDD
  --rebalance {M, W}       월말 / 주말 리밸
  --topn N                 보유 종목 수 (기본 20)
  --min-value 30           일평균 거래대금 최소 (억원, 기본 30)
  --slippage 0.003         양방향 슬리피지 (기본 0.3%)
  --syms-from {cached,watchlist,universe}
  --report-out DIR         리포트 저장 디렉토리
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "src"))

from swing.data_loader import (
    update_universe, load_universe, list_cached_symbols,
    load_calendar, build_calendar,
)
from swing.backtester import run_backtest
from swing.strategies import STRATEGIES
from swing.report import print_report, save_report


def _read_syms(args) -> list[str]:
    if args.syms:
        return args.syms
    src = args.syms_from
    if src == "cached":
        return list_cached_symbols()
    if src == "watchlist":
        wl = BASE_DIR / "data" / "watchlist.txt"
        if wl.exists():
            return [s.strip() for s in wl.read_text(encoding="utf-8").splitlines()
                    if s.strip() and not s.startswith("#")]
        return []
    if src == "universe":
        u = BASE_DIR / "data" / "volume_profiles" / "_symbols.json"
        if u.exists():
            import json
            return json.loads(u.read_text(encoding="utf-8"))
        return []
    return []


def cmd_update(args) -> None:
    syms = _read_syms(args)
    if not syms:
        print("종목 없음. --syms 또는 --syms-from 지정.")
        sys.exit(1)
    update_universe(syms, end_date=args.end, default_start=args.start)


def cmd_backtest(args) -> None:
    syms = _read_syms(args) or list_cached_symbols()
    if not syms:
        print("캐시된 일봉 없음. 먼저 --update 실행.")
        sys.exit(1)
    print(f"유니버스: {len(syms)}종목 (캐시 로드 중...)")

    min_value = args.min_value * 1e8  # 억원 → 원
    prices = load_universe(syms, min_value=min_value)
    print(f"  거래대금 {args.min_value}억+ 필터: {len(prices)}종목")
    if not prices:
        print("필터 후 종목 없음.")
        sys.exit(1)

    cal = load_calendar()
    if not cal:
        cal = build_calendar(end_date=args.end, default_start=args.start)
    print(f"  캘린더: {len(cal)}거래일 ({cal[0]}~{cal[-1]})")

    strategies = []
    if args.strategy == "all":
        strategies = ["top_value", "low_vol_momentum", "breakout_volume", "blend"]
    else:
        strategies = [args.strategy]

    results = {}
    for strat_name in strategies:
        if strat_name not in STRATEGIES:
            print(f"unknown strategy: {strat_name}")
            continue
        factory = STRATEGIES[strat_name]
        # 공통 인자: topn, min_value
        kwargs = {"topn": args.topn, "min_value": min_value}
        select_fn = factory(**{k: v for k, v in kwargs.items()
                                if k in factory.__code__.co_varnames})

        print(f"\n--- 실행: {strat_name} ---")
        result = run_backtest(
            prices=prices,
            calendar=cal,
            select_fn=select_fn,
            start=args.start,
            end=args.end,
            rebalance=args.rebalance,
            initial_cash=args.cash,
            slippage_pct=args.slippage,
            fee_pct=args.fee,
            sell_tax_pct=args.sell_tax,
        )
        print_report(result, label=strat_name)
        results[strat_name] = result

        if args.report_out:
            save_report(result, args.report_out, label=strat_name)

    if len(results) > 1:
        print("\n=== 비교표 ===")
        from swing.report import compute_metrics
        rows = []
        for name, r in results.items():
            m = compute_metrics(r.equity)
            rows.append((name, m.get("cagr", 0), m.get("sharpe", 0),
                         m.get("mdd", 0), m.get("vol", 0)))
        print(f"  {'strategy':<22} {'CAGR':>8} {'Sharpe':>8} {'MDD':>8} {'Vol':>8}")
        for n, c, s, d, v in rows:
            print(f"  {n:<22} {c*100:>7.2f}% {s:>8.2f} {d*100:>7.2f}% {v*100:>7.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="일봉 데이터 수집/업데이트")
    ap.add_argument("--strategy", default="low_vol_momentum",
                    choices=list(STRATEGIES.keys()) + ["all"])
    ap.add_argument("--start", default="20200101")
    ap.add_argument("--end", default="20260401")
    ap.add_argument("--rebalance", default="M", choices=["M", "W"])
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--min-value", type=float, default=30.0, help="일평균 거래대금 최소 (억원)")
    ap.add_argument("--cash", type=float, default=1e8, help="초기자본 (원)")
    ap.add_argument("--slippage", type=float, default=0.003)
    ap.add_argument("--fee", type=float, default=0.00015)
    ap.add_argument("--sell-tax", type=float, default=0.0023)
    ap.add_argument("--syms", nargs="+", default=None)
    ap.add_argument("--syms-from", default="cached", choices=["cached", "watchlist", "universe"])
    ap.add_argument("--report-out", default=None)
    args = ap.parse_args()

    if args.update:
        cmd_update(args)
    else:
        cmd_backtest(args)


if __name__ == "__main__":
    main()
