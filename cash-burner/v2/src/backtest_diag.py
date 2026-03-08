"""backtest_diag.py — signal_diag.log + ledger_real.csv 연계 분석 스크립트.

사용법:
    python backtest_diag.py [--date YYYYMMDD]

date 미지정 시 오늘 날짜 기준 파일 탐색. 파일명에 날짜 없으면 기본 경로 사용.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


DEFAULT_SIGNAL_DIAG = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag.log"))
DEFAULT_LEDGER = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
OUTPUT_TRADE_SUMMARY = os.path.join("data", "trade_summary.csv")
OUTPUT_PERF_BY_REASON = os.path.join("data", "perf_by_reason.csv")


def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


# ---------- signal_diag 파싱 ----------

def parse_signal_diag(path: str) -> Tuple[List[dict], List[dict]]:
    """BUY / SELL 이벤트 파싱. (ts, sym, score/metrics, reason/pnl_pct/hold_sec)"""
    buys: List[dict] = []
    sells: List[dict] = []
    if not os.path.exists(path):
        return buys, sells
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",", 3)
            if len(parts) < 4:
                continue
            ts_str, sym, status, detail = parts[0], parts[1], parts[2], parts[3]
            ts = _f(ts_str)
            if status == "BUY":
                rec: dict = {"ts": ts, "sym": sym, "status": "BUY"}
                for tok in detail.split():
                    if "=" in tok:
                        k, _, v = tok.partition("=")
                        rec[k] = v
                buys.append(rec)
            elif status == "SELL":
                rec = {"ts": ts, "sym": sym, "status": "SELL"}
                for tok in detail.split():
                    if "=" in tok:
                        k, _, v = tok.partition("=")
                        rec[k] = v
                sells.append(rec)
    return buys, sells


# ---------- ledger 파싱 ----------

def parse_ledger(path: str) -> Tuple[List[dict], List[dict]]:
    ledger_buys: List[dict] = []
    ledger_sells: List[dict] = []
    if not os.path.exists(path):
        return ledger_buys, ledger_sells
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            action = (row.get("action") or "").upper()
            if action == "BUY":
                ledger_buys.append(row)
            elif action == "SELL":
                ledger_sells.append(row)
    return ledger_buys, ledger_sells


# ---------- BUY-SELL 매칭 ----------

def match_trades(diag_buys: List[dict], diag_sells: List[dict],
                 ledger_buys: List[dict], ledger_sells: List[dict]) -> List[dict]:
    """BUY-SELL 쌍 매칭 → 개별 트레이드 구성."""
    trades: List[dict] = []

    # ledger BUY/SELL을 심볼별로 큐 구성
    lb_by_sym: Dict[str, list] = defaultdict(list)
    for row in ledger_buys:
        lb_by_sym[row.get("symbol", "")].append(row)
    ls_by_sym: Dict[str, list] = defaultdict(list)
    for row in ledger_sells:
        ls_by_sym[row.get("symbol", "")].append(row)

    # diag BUY를 심볼별로 큐
    db_by_sym: Dict[str, list] = defaultdict(list)
    for rec in diag_buys:
        db_by_sym[rec["sym"]].append(rec)
    ds_by_sym: Dict[str, list] = defaultdict(list)
    for rec in diag_sells:
        ds_by_sym[rec["sym"]].append(rec)

    all_syms = set(db_by_sym) | set(lb_by_sym)
    for sym in sorted(all_syms):
        dbuys = sorted(db_by_sym.get(sym, []), key=lambda r: r["ts"])
        dsells = sorted(ds_by_sym.get(sym, []), key=lambda r: r["ts"])
        lbuys = sorted(lb_by_sym.get(sym, []), key=lambda r: _f(r.get("ts")))
        lsells = sorted(ls_by_sym.get(sym, []), key=lambda r: _f(r.get("ts")))

        bi = si = 0
        used_lb: set[int] = set()
        used_ls: set[int] = set()
        while bi < len(dbuys) and si < len(dsells):
            b = dbuys[bi]
            s = dsells[si]
            if s["ts"] <= b["ts"]:
                si += 1
                continue
            # 미사용 ledger 레코드 중 timestamp 가장 가까운 것 매칭
            lb_cands = [(i, r) for i, r in enumerate(lbuys) if i not in used_lb]
            if lb_cands:
                lb_idx, lb = min(lb_cands, key=lambda ir: abs(_f(ir[1].get("ts")) - b["ts"]))
                used_lb.add(lb_idx)
            else:
                lb = {}
            ls_cands = [(i, r) for i, r in enumerate(lsells) if i not in used_ls]
            if ls_cands:
                ls_idx, ls = min(ls_cands, key=lambda ir: abs(_f(ir[1].get("ts")) - s["ts"]))
                used_ls.add(ls_idx)
            else:
                ls = {}

            buy_price = _f(lb.get("price")) or _f(b.get("price"))
            sell_price = _f(ls.get("price")) or _f(s.get("price"))
            qty = _f(lb.get("qty")) or 1.0

            pnl_pct_diag = _f(s.get("pnl", "").rstrip("%"))
            pnl_pct_ledger = ((sell_price / buy_price - 1.0) * 100.0) if buy_price > 0 and sell_price > 0 else None
            hold_sec = _f(s.get("hold", "").rstrip("s"))
            reason = s.get("reason", "unknown")
            score = _f(b.get("score"))
            buy_hhmm = int(time.strftime("%H%M", time.localtime(b["ts"])))

            trades.append({
                "sym": sym,
                "buy_ts": b["ts"],
                "sell_ts": s["ts"],
                "buy_price": buy_price,
                "sell_price": sell_price,
                "qty": qty,
                "score": score,
                "reason": reason,
                "pnl_pct_diag": pnl_pct_diag,
                "pnl_pct_ledger": pnl_pct_ledger,
                "hold_sec": hold_sec,
                "buy_hhmm": buy_hhmm,
            })
            bi += 1
            si += 1

    return trades


# ---------- 분석 ----------

def analyze(trades: List[dict]) -> None:
    if not trades:
        print("거래 내역 없음.")
        return

    total = len(trades)
    pnls = [t["pnl_pct_ledger"] for t in trades if t["pnl_pct_ledger"] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    print(f"\n=== 전체 통계 ===")
    print(f"거래 수: {total}  (pnl 확인가능: {len(pnls)})")
    if pnls:
        win_rate = len(wins) / len(pnls) * 100
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        print(f"승률: {win_rate:.1f}%  평균이익: {avg_win:+.2f}%  평균손실: {avg_loss:+.2f}%")
        print(f"기대값: {sum(pnls)/len(pnls):+.3f}%")

    # 청산 이유별
    print(f"\n=== 청산 이유별 통계 ===")
    by_reason: Dict[str, list] = defaultdict(list)
    for t in trades:
        by_reason[t["reason"]].append(t.get("pnl_pct_ledger"))
    for reason, vals in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        valid = [v for v in vals if v is not None]
        avg = sum(valid) / len(valid) if valid else float("nan")
        print(f"  {reason}: 건수={len(vals)} 평균pnl={avg:+.2f}%")

    # 시간대별 승률
    print(f"\n=== 시간대별 승률 ===")
    hour_buckets = {9: [], 10: [], 11: [], 13: [], 14: []}
    for t in trades:
        h = t["buy_hhmm"] // 100
        if h in hour_buckets and t["pnl_pct_ledger"] is not None:
            hour_buckets[h].append(t["pnl_pct_ledger"])
    for h, vals in sorted(hour_buckets.items()):
        if not vals:
            continue
        wr = len([v for v in vals if v > 0]) / len(vals) * 100
        print(f"  {h}시대: 건수={len(vals)} 승률={wr:.1f}% 평균={sum(vals)/len(vals):+.2f}%")

    # score 구간별 승률
    print(f"\n=== 점수 구간별 승률 ===")
    score_buckets = {"85~100": [], "100~120": [], "120+": []}
    for t in trades:
        s = t["score"]
        v = t["pnl_pct_ledger"]
        if v is None:
            continue
        if s < 100:
            score_buckets["85~100"].append(v)
        elif s < 120:
            score_buckets["100~120"].append(v)
        else:
            score_buckets["120+"].append(v)
    for band, vals in score_buckets.items():
        if not vals:
            continue
        wr = len([v for v in vals if v > 0]) / len(vals) * 100
        print(f"  score {band}: 건수={len(vals)} 승률={wr:.1f}% 평균={sum(vals)/len(vals):+.2f}%")

    # Top 5 수익 / 손실
    by_sym_pnl: Dict[str, float] = defaultdict(float)
    for t in trades:
        if t["pnl_pct_ledger"] is not None:
            by_sym_pnl[t["sym"]] += t["pnl_pct_ledger"]
    ranked = sorted(by_sym_pnl.items(), key=lambda kv: kv[1])
    print(f"\n=== 손실 Top 5 ===")
    for sym, pnl in ranked[:5]:
        print(f"  {sym}: {pnl:+.2f}%")
    print(f"\n=== 수익 Top 5 ===")
    for sym, pnl in ranked[-5:][::-1]:
        print(f"  {sym}: {pnl:+.2f}%")


# ---------- CSV 저장 ----------

def save_csvs(trades: List[dict]) -> None:
    os.makedirs("data", exist_ok=True)

    with open(OUTPUT_TRADE_SUMMARY, "w", encoding="utf-8", newline="") as f:
        fields = ["sym", "buy_ts", "sell_ts", "buy_price", "sell_price", "qty",
                  "score", "reason", "pnl_pct_diag", "pnl_pct_ledger", "hold_sec", "buy_hhmm"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow({k: (f"{t[k]:.4f}" if isinstance(t[k], float) else t[k]) for k in fields})
    print(f"\n저장: {OUTPUT_TRADE_SUMMARY}")

    by_reason: Dict[str, list] = defaultdict(list)
    for t in trades:
        by_reason[t["reason"]].append(t.get("pnl_pct_ledger"))
    with open(OUTPUT_PERF_BY_REASON, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["reason", "count", "avg_pnl", "win_rate"])
        writer.writeheader()
        for reason, vals in sorted(by_reason.items()):
            valid = [v for v in vals if v is not None]
            avg = sum(valid) / len(valid) if valid else float("nan")
            wr = len([v for v in valid if v > 0]) / len(valid) * 100 if valid else float("nan")
            writer.writerow({"reason": reason, "count": len(vals),
                             "avg_pnl": f"{avg:.4f}", "win_rate": f"{wr:.1f}"})
    print(f"저장: {OUTPUT_PERF_BY_REASON}")


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="signal_diag + ledger 분석")
    parser.add_argument("--date", default="", help="날짜 YYYYMMDD (기본: 오늘)")
    args = parser.parse_args()

    date_str = args.date or time.strftime("%Y%m%d")

    def _resolve(default_path: str) -> str:
        base, ext = os.path.splitext(default_path)
        dated = f"{base}_{date_str}{ext}"
        if os.path.exists(dated):
            return dated
        return default_path

    diag_path = _resolve(DEFAULT_SIGNAL_DIAG)
    ledger_path = _resolve(DEFAULT_LEDGER)

    print(f"signal_diag: {diag_path}")
    print(f"ledger:      {ledger_path}")

    diag_buys, diag_sells = parse_signal_diag(diag_path)
    ledger_buys, ledger_sells = parse_ledger(ledger_path)

    print(f"diag BUY={len(diag_buys)} SELL={len(diag_sells)}")
    print(f"ledger BUY={len(ledger_buys)} SELL={len(ledger_sells)}")

    trades = match_trades(diag_buys, diag_sells, ledger_buys, ledger_sells)
    analyze(trades)
    save_csvs(trades)


if __name__ == "__main__":
    main()
