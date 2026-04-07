from __future__ import annotations

import json
import math
import os
import time
import threading
import concurrent.futures
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, Tuple

from kis_orders import buyable_cash, sellable_qty, order_cash, account_buying_power, account_cash_snapshot, inquire_holdings
from notifier import DiscordNotifier
from quote_basic import load_cache, ensure_prev_close, save_cache
try:
    from scanner_company_rank import register_stoploss_blacklist as _register_sl_blacklist
except Exception:
    def _register_sl_blacklist(sym: str):  # type: ignore
        pass


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d


def _fmt_won(v: Any) -> str:
    if v is None:
        return "-"
    try:
        n = float(v)
    except Exception:
        return "-"
    if not math.isfinite(n):
        return "-"
    return f"{int(round(n)):,}원"


@dataclass
class Position:
    qty: int
    entry_price: float
    entry_ts: float
    max_price: float
    max_pnl_pct: float = 0.0
    min_pnl_pct: float = 0.0
    score: float = 0.0
    reasons: list[str] | None = None
    sell_fail_count: int = 0
    atr_pct: float = 0.0       # PROMPT 1: 진입 시 30초 변동성
    partial_taken: bool = False  # PROMPT 1: 부분익실 완료 여부
    split_a_sold: bool = False   # 50/50 분할청산: A물량(trail) 청산 완료 여부
    last_price: float = 0.0    # PROMPT 7: 시장상태 추적용
    last_price_ts: float = 0.0  # BUG-014: 마지막 틱 timestamp
    fill_confirmed: bool = True
    sellable_zero_count: int = 0
    upper_limit: float = 0.0       # 당일 상한가 (KIS 기준가 * 1.30)
    manual: bool = False  # 수동 매수로 SYNC_ADD된 포지션 — max_hold/손절 적용 제외


CASH_REFRESH_INTERVAL = float(os.getenv("CASH_REFRESH_INTERVAL_SEC", "30"))


def _roll_file(path: str, max_lines: int, keep_lines: int):
    """파일이 max_lines를 초과하면 마지막 keep_lines줄만 남김."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= max_lines:
            return
        lines = lines[-keep_lines:]
        tmp = path + ".rolltmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, path)
    except Exception:
        pass


class EngineSimple:
    """실전용 단순 모멘텀 엔진: 단일 매수 경로 / 단일 청산 경로."""

    def __init__(self):
        # files
        self.ledger_file = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
        self.state_file = os.getenv("POSITION_STATE_FILE", os.path.join("data", "positions_simple.json"))
        self.watchlist_file = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
        self.signal_diag_file = os.getenv("SIGNAL_DIAG_FILE", os.path.join("data", "signal_diag.log"))
        self.runtime_status_file = os.getenv("RUNTIME_STATUS_FILE", os.path.join("data", "runtime_status.json"))

        # buy / score
        self.position_pct = float(os.getenv("POSITION_PCT", "0.25"))  # FIX-20260309: 0.30→0.25 현금고갈 방지
        self.max_positions = max(1, int(os.getenv("MAX_POSITIONS", "10")))  # 포지션 수 제한 완화: 실제 제한은 현금 잔고 기반
        self.entry_score_threshold = float(os.getenv("ENTRY_SCORE_THRESHOLD", "155"))
        self.entry_score_strong = float(os.getenv("ENTRY_SCORE_STRONG", "220"))        # 165→220: strong 기준도 비례 상향
        self.entry_block_dayrise_pct = float(os.getenv("ENTRY_BLOCK_DAYRISE_PCT", "12.0"))  # 7.0→12.0: 급등주 타겟(+3~15%) 맞게 상향
        self.entry_hard_dayrise_block_pct = float(os.getenv("ENTRY_HARD_DAYRISE_BLOCK_PCT", "20.0"))  # 18.0→20.0

        self.buy_ret10_min = float(os.getenv("BUY_RET10_MIN", "0.20"))  # 0.30→0.20: 저가 종목 gate_ret10 완화
        self.buy_ret5_min = float(os.getenv("BUY_RET5_MIN", "0.15"))
        self.buy_trv10_min = float(os.getenv("BUY_TRV10_MIN", "10000000"))  # 20M→10M: ret10 제거로 진입 빨라짐, 초기 거래대금 낮을 수 있음
        self.buy_ofi_min = float(os.getenv("BUY_OFI_MIN", "1.4"))
        self.buy_imb_min = float(os.getenv("BUY_IMB_MIN", "0.60"))
        self.buy_spread_max_bps = float(os.getenv("BUY_SPREAD_MAX_BPS", "35"))
        self.pullback_pct = float(os.getenv("PULLBACK_PCT", "1.2"))        # 0.65→1.2: 급등주 눌림은 1~2% 현실적
        self.pullback_rebound_pct = float(os.getenv("PULLBACK_REBOUND_PCT", "0.30"))  # 0.18→0.30: 반등 확인 강화
        self.vi_guard_pct = float(os.getenv("VI_GUARD_PCT", "0.25"))

        # sell (4 rules only)
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "3.5"))  # 2.0→3.5 급등주 노이즈 흡수 (기존 53% SL률)
        self.take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "20.0"))  # B물량 TP (SPLIT_B_TP_PCT와 동기화)
        self.trail_arm_pct = float(os.getenv("TRAIL_ARM_PCT", "5.0"))  # 3.0→5.0 조기 trail 방지 (475150 +0.44% 매도 사건)
        self.trail_drop_pct = float(os.getenv("TRAIL_DROP_PCT", "3.0"))  # 2.0→3.0 되돌림 여유 확대
        self.max_hold_sec = float(os.getenv("MAX_HOLD_SEC", "99999"))  # 사실상 무제한
        self.exit_grace_sec = float(os.getenv("EXIT_GRACE_SEC", "30.0"))  # 10→30초, 시장가 체결 후 스프레드 노이즈 흡수
        self.take_profit_grace_sec = float(os.getenv("TAKE_PROFIT_GRACE_SEC", "30.0"))  # exit_grace_sec와 동기화
        self.stop_loss_early_grace_sec = float(os.getenv("STOP_LOSS_EARLY_GRACE_SEC", "15.0"))  # 8→15초
        self.stop_loss_early_relax_mult = float(os.getenv("STOP_LOSS_EARLY_RELAX_MULT", "1.6"))
        self.stop_loss_emergency_pct = float(os.getenv("STOP_LOSS_EMERGENCY_PCT", "5.0"))  # 3.0→5.0
        self.cooldown_sec = float(os.getenv("COOLDOWN_SEC", "60"))  # FIX-20260309: 90→60
        # PROMPT 3: 청산 이유별 쿨다운
        self.cooldown_stop_sec = float(os.getenv("COOLDOWN_STOP_SEC", "120"))  # FIX-20260309: 180→120
        self.cooldown_panic_sec = float(os.getenv("COOLDOWN_PANIC_SEC", "180"))  # FIX-20260309: 300→180
        self.cooldown_take_sec = float(os.getenv("COOLDOWN_TAKE_SEC", "60"))  # FIX-20260309: 30→15→60 익절 후 재진입 방지
        self.cooldown_trail_sec = float(os.getenv("COOLDOWN_TRAIL_SEC", "30"))  # FIX-20260309: 60→30
        self.cooldown_maxhold_sec = float(os.getenv("COOLDOWN_MAXHOLD_SEC", "15"))  # FIX-20260309: 30→15
        self.loss_streak_block_enabled = os.getenv("LOSS_STREAK_BLOCK", "1") == "1"
        # PROMPT 1: 부분익실
        self.partial_take_enabled = os.getenv("PARTIAL_TAKE_ENABLED", "0") == "1"  # 1→0: 분할매도 비활성화 (트레일로 전량 청산)
        # 50/50 분할청산: A물량=trail, B물량=SL/TP/장마감
        self._split_exit_enabled = os.getenv("SPLIT_EXIT_ENABLED", "1") == "1"
        self._split_b_tp_pct = float(os.getenv("SPLIT_B_TP_PCT", "20.0"))
        self._limit_up_tp_pct = float(os.getenv("LIMIT_UP_TP_PCT", "27.0"))  # 상한가 근접 전량 TP
        # PROMPT 7: 장초반 거래대금 완화
        self.morning_trv_relax = os.getenv("MORNING_TRV_RELAX", "1") == "1"
        self.daily_loss_limit_pct = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.5"))  # surge 구조: 최대 10거래 가능 → 3.5% 한도 활성화
        self._daily_loss_base_cash: float | None = None
        self._daily_realized_pnl: float = 0.0
        self._trading_halted: bool = False
        self._reconcile_done: bool = False
        self._last_trading_day: str = ""

        self.entry_chase_penalty_dayrise_pct = float(os.getenv("ENTRY_CHASE_PENALTY_DAYRISE_PCT", "15.0"))  # entry_block(12%)보다 높아야 함

        self.health_check_sec = float(os.getenv("HEALTH_CHECK_SEC", "1800"))
        self.health_cash_symbol = os.getenv("HEALTH_CASH_SYMBOL", "005930").strip() or "005930"

        # runtime state
        self.ticks: Dict[str, Deque[Tuple[float, float, float]]] = defaultdict(lambda: deque(maxlen=4096))
        self.book: Dict[str, Dict[str, str]] = {}
        self.book_ts: Dict[str, float] = {}
        self.pos: Dict[str, Position] = {}
        self.cooldown_until: Dict[str, float] = {}
        self.watch: set[str] = set()
        self._last_watch_reload_ts = 0.0
        self._last_candidate_log_ts: Dict[str, float] = {}
        self._last_health_ts = 0.0
        self._score_eval_total = 0
        self._score_pass_total = 0
        self._gate_block_counts: Dict[str, int] = defaultdict(int)
        self._last_buy_time = 0.0
        self._last_sell_time = 0.0
        self._last_buy_symbol = ""
        self._last_sell_symbol = ""
        self._recent_events: Deque[Dict[str, Any]] = deque(maxlen=10)
        self._last_runtime_snapshot_ts = 0.0
        self._state_lock = threading.Lock()
        self._last_orderable_cash: float | None = None
        self._last_orderable_cash_ts = 0.0
        # PROMPT 3: 연속손절 추적
        self._loss_streak: Dict[str, int] = defaultdict(int)
        self._loss_streak_blocked: set[str] = set()
        # PROMPT 2: VI 해제 추적
        self._vi_clear_ts: Dict[str, float] = {}
        self._vi_prev_std: Dict[str, float] = {}
        # BUG-023: 체결 미확인 종목 재진입 차단 (fill_unknown 이후 동일 종목 중복 주문 방지)
        self._pending_fill: Dict[str, float] = {}
        self._buy_fail_count: Dict[str, int] = {}  # BUY_FAIL 연속 카운터
        self._session_blacklist: set = set()  # 3회 실패 → 당일 세션 블랙리스트
        self.pending_fill_block_sec = float(os.getenv("PENDING_FILL_BLOCK_SEC", "3.0"))  # FIX-20260309: 하드코딩 10s → 환경변수화
        self.manual_halt_file = os.getenv("MANUAL_HALT_FILE", os.path.join("data", "pause.txt"))  # FIX-20260309: 긴급정지 스위치
        self.entry_top_k = int(os.getenv("ENTRY_TOP_K", "2"))  # 현재 미사용 — on_timer 배치 선택으로 대체
        self._watchlist_scores: Dict[str, float] = {}  # sym → 최근 score 캐시
        self._watchlist_score_ts: Dict[str, float] = {}  # sym → score 계산 시각
        # REFACTOR: 틱에서 게이트 통과한 후보를 모아두고 on_timer에서 최고점 1개 선택
        self._buy_candidates: Dict[str, Dict] = {}  # sym → {score, price, reasons, metrics, ts}
        self._imb_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=300))
        self._ba_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=300))  # (ts, ba_ratio)
        self._prev_ofi: Dict[str, float] = {}    # 2틱 게이트용 직전 ofi
        self._prev_burst: Dict[str, float] = {}  # 2틱 게이트용 직전 burst_ratio
        self._prev_imb: Dict[str, float] = {}    # 2틱 게이트용 직전 imb
        self.entry_orderflow_min = float(os.getenv("ENTRY_ORDERFLOW_MIN", "0.0"))  # orderflow 하한
        self.entry_imb_min_gate = float(os.getenv("ENTRY_IMB_MIN", "0.52"))  # imb 하한 게이트
        # NEW-001: on_timer 최초 1회 실계좌 수량 검증 플래그
        self._state_validated: bool = False
        self._init_ts: float = time.time()  # 기동 시각 (검증 대기 기준)
        # PROMPT 7: 시장 하락추세 감지
        self._market_declining: bool = False
        self._market_declining_until: float = 0.0

        # ── 동시호가 (H0STANC0) 사전 데이터 ──────────────────────────
        # sym → {ba_ratio, expected_gap_pct, ts}
        # PRE_SUB에서 참조해 매수/매도 잔량 비율이 높은 종목을 우선 구독
        self._preopen_data: Dict[str, Dict] = {}
        self._preopen_history: Dict[str, list] = {}  # sym → [(ts, expected_gap_pct)] 기울기 계산용
        self._preopen_whitelist: set[str] = set()  # 동시호가 퀄리티 상위 N개 (9:00 직전 확정)
        self._preopen_whitelist_done: bool = False
        self._preopen_budgets: Dict[str, float] = {}  # sym → 사전 배분 예산 (원)

        # ── VWAP ────────────────────────────────────────────────────
        self._vwap_pv: Dict[str, float] = defaultdict(float)
        self._vwap_v:  Dict[str, float] = defaultdict(float)
        self._vwap_reset_date: str = ""
        # ── True Book OFI (L2 델타) ──────────────────────────────────
        self._prev_bid1: Dict[str, Tuple[float, float]] = {}
        self._prev_ask1: Dict[str, Tuple[float, float]] = {}
        self._book_ofi_buf: Dict[str, Deque[Tuple[float, float]]] = defaultdict(lambda: deque(maxlen=30))

        self.diag_roll_maxlines = int(os.getenv("DIAG_ROLL_MAXLINES", "0"))  # 0=롤링 비활성화 (날짜별 파일로 보존)
        self.diag_roll_keeplines = int(os.getenv("DIAG_ROLL_KEEPLINES", "0"))
        self._diag_write_count = 0
        self._console_msgs: list[str] = []   # runner_live가 drain해서 표시
        self.ticker_snap_file = os.getenv("TICKER_SNAP_FILE", os.path.join("data", "ticker_snap.csv"))
        self.ticker_snap_interval = float(os.getenv("TICKER_SNAP_INTERVAL", "1.5"))
        self._last_ticker_snap_ts: Dict[str, float] = {}
        self.ticker_snap_maxlines = int(os.getenv("TICKER_SNAP_MAXLINES", "3000"))
        self._ticker_snap_write_count = 0

        self.prev_close_cache = load_cache()
        self._prev_close_cache_ok = len(self.prev_close_cache) >= 10
        if not self._prev_close_cache_ok:
            self._console_msgs.append(f"[WARN] prev_close_cache 비어있음(n={len(self.prev_close_cache)}) "
                  f"— watchlist 로드 후 API에서 자동 보충됩니다")
        self._prev_close_fetch_pending: bool = False  # BUG-010: async guard
        self._fill_confirm_pending: bool = False       # BUY_FILL_ASSUMED 체결가 교정 guard
        self._fill_confirm_ts: float = 0.0            # 마지막 교정 시도 시각
        from morning_fastpath import MorningFastPath
        self._fast_path = MorningFastPath(self, rvol_loader=None)
        self.notifier = DiscordNotifier()
        self._init_files()
        self._load_state()
        self._prime_cash_status()

    # ---------- infra ----------
    def _init_files(self):
        for p in (self.ledger_file, self.signal_diag_file, self.state_file, self.runtime_status_file, self.ticker_snap_file):
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.ledger_file):
            with open(self.ledger_file, "w", encoding="utf-8") as f:
                f.write("ts,action,symbol,qty,price,reason,rt_cd,msg\n")
        if not os.path.exists(self.ticker_snap_file):
            with open(self.ticker_snap_file, "w", encoding="utf-8") as f:
                f.write("ts,symbol,price,score,ret10,ret5,ofi,imb,spread_bps,dayrise,pass,no_book,"
                        "cntg_vol,acml_vol,acml_tr,buy_cnt,sell_cnt,ntby_cnt,cttr,pvol_rate,open_px,high_px,low_px,"
                        "liq_raw,ofi_raw,ba_ratio,ba_trend_30s\n")

    def _log_ledger(self, ts_epoch: float, action: str, sym: str, qty: int, price: float, reason: str, rt_cd: str, msg: str):
        safe = (msg or "").replace('"', "")[:240]
        with open(self.ledger_file, "a", encoding="utf-8") as f:
            f.write(f"{ts_epoch:.3f},{action},{sym},{qty},{price:.4f},\"{reason}\",{rt_cd},\"{safe}\"\n")

    def _log_diag(self, ts_epoch: float, sym: str, status: str, detail: str):
        with open(self.signal_diag_file, "a", encoding="utf-8") as f:
            f.write(f"{ts_epoch:.3f},{sym},{status},{detail}\n")
        if status in ("BUY", "SELL", "EVICT", "SYNC_EVICT", "SYNC_ADD",
                      "TRAIL_FIRE", "HALT", "BUY_FAIL", "SELL_FAIL", "PARTIAL_SELL"):
            ts_str = time.strftime("%H:%M:%S", time.localtime(ts_epoch))
            # 핵심 지표만 파싱해서 앞에 표시 (price, qty, score, pnl, reason)
            _short = detail
            _kv: dict = {}
            for _tok in detail.replace(" ", ",").split(","):
                if "=" in _tok:
                    _k, _, _v = _tok.partition("=")
                    _kv[_k.strip()] = _v.strip()
            _R = "\033[91m"; _B = "\033[94m"; _G = "\033[92m"; _W = "\033[93m"; _D = "\033[2m"; _0 = "\033[0m"
            if status == "BUY":
                _price   = _kv.get("fill_price") or _kv.get("price") or ""
                _qty     = _kv.get("qty") or ""
                _score   = _kv.get("score") or ""
                _short   = f"score={_G}{_score}{_0}  price={_price}  qty={_qty}"
            elif status in ("SELL", "EVICT"):
                _pnl_s   = _kv.get("pnl") or "0"
                _pnl_f   = float(_pnl_s.rstrip("%")) if _pnl_s.replace("-","").replace(".","").replace("%","").isdigit() else 0.0
                _pnl_col = _R if _pnl_f > 0 else (_B if _pnl_f < 0 else _0)
                _reason  = _kv.get("reason") or ""
                _hold    = _kv.get("hold") or ""
                _price   = _kv.get("price") or ""
                _short   = f"pnl={_pnl_col}{_pnl_s}{_0}  reason={_reason}  hold={_hold}  price={_price}"
            elif status in ("HALT", "BUY_FAIL", "SELL_FAIL"):
                _short   = f"{_W}{detail[:100]}{_0}"
            else:
                _short = detail[:100]
            # 고정폭: [HH:MM:SS] [STATUS  ] SYM  ...
            _status_padded = f"{status:<10}"
            _sym_padded    = f"{sym:<8}"
            _msg = f"[{ts_str}] [{_status_padded}] {_sym_padded} {_short}"
            self._console_msgs.append(_msg)
        self._diag_write_count += 1
        if self._diag_write_count % 500 == 0:
            _roll_file(self.signal_diag_file, self.diag_roll_maxlines, self.diag_roll_keeplines)

    def _save_state(self):
        rows = {
            "positions": {sym: asdict(p) for sym, p in self.pos.items()},
            "cooldown_until": self.cooldown_until,
            "loss_streak": dict(self._loss_streak),
            "loss_streak_blocked": list(self._loss_streak_blocked),
            # NEW-002: 재시작 후에도 halt/손익 상태 유지
            "trading_halted": self._trading_halted,
            "daily_realized_pnl": self._daily_realized_pnl,
            "last_trading_day": self._last_trading_day,
        }
        # BUG-012: 원자적 쓰기로 크래시 시 부분 쓰기 방지
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    def _load_state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            return
        try:
            self.cooldown_until = {str(k): float(v) for k, v in (j.get("cooldown_until") or {}).items()}
        except Exception:
            self.cooldown_until = {}
        try:
            self._loss_streak = defaultdict(int, {k: int(v) for k, v in (j.get("loss_streak") or {}).items()})
        except Exception:
            pass
        try:
            self._loss_streak_blocked = set(j.get("loss_streak_blocked") or [])
        except Exception:
            pass
        # NEW-002: 재시작 후 halt/손익/날짜 상태 복원
        try:
            self._trading_halted = bool(j.get("trading_halted", False))
            self._daily_realized_pnl = float(j.get("daily_realized_pnl", 0.0))
            self._last_trading_day = str(j.get("last_trading_day", ""))
        except Exception:
            pass
        # NEW-001: sellable_qty REST 호출을 _load_state에서 제거해 기동 블로킹 방지.
        # 실계좌 수량 검증은 on_timer 최초 1회에서 수행한다.
        for sym, item in (j.get("positions") or {}).items():
            try:
                qty_state = int(item.get("qty", 0))
                if qty_state <= 0:
                    continue
                self.pos[sym] = Position(
                    qty=qty_state,
                    entry_price=float(item.get("entry_price", 0.0)),
                    entry_ts=max(float(item.get("entry_ts", time.time())), time.time() - self.max_hold_sec),  # FIX-BUG2: 재시작 후 즉시 max_hold 청산 방지
                    max_price=float(item.get("max_price", item.get("entry_price", 0.0))),
                    max_pnl_pct=float(item.get("max_pnl_pct", 0.0)),
                    min_pnl_pct=float(item.get("min_pnl_pct", 0.0)),
                    score=float(item.get("score", 0.0)),
                    reasons=list(item.get("reasons") or []),
                    atr_pct=float(item.get("atr_pct", 0.0)),
                    partial_taken=bool(item.get("partial_taken", False)),
                    split_a_sold=bool(item.get("split_a_sold", False)),
                    last_price=float(item.get("last_price", 0.0)),
                    fill_confirmed=bool(item.get("fill_confirmed", True)),
                    upper_limit=float(item.get("upper_limit", 0.0)),
                    manual=bool(item.get("manual", False)),  # 재시작 복구 시 manual 여부 유지
                )
            except Exception:
                continue
        # BUG-A 수정: ledger SELL 기록으로 쿨다운 복원 (_sell_ts + _cd 기준)
        import csv as _csv
        try:
            _now = time.time()
            with open(self.ledger_file, "r", encoding="utf-8") as _lf:
                for _row in _csv.DictReader(_lf):
                    if _row.get("action", "").upper() != "SELL":
                        continue
                    if _row.get("rt_cd", "") != "0":
                        continue
                    _sym = _row.get("symbol", "")
                    if not _sym:
                        continue
                    _sell_ts = float(_row.get("ts") or 0)
                    if _sell_ts <= 0:
                        continue
                    _reason = _row.get("reason", "")
                    _cd = self._exit_cooldown(_reason)
                    _new_until = _sell_ts + _cd  # 매도 시각 기준
                    if _new_until <= _now:
                        continue  # 이미 만료된 쿨다운은 무시
                    if self.cooldown_until.get(_sym, 0) < _new_until:
                        self.cooldown_until[_sym] = _new_until
        except Exception:
            pass
        # FIX-20260309 BUG-4: KIS 실계좌로 self.pos 초기화
        try:
            now = time.time()
            holdings = inquire_holdings()
            kis_map: Dict[str, tuple] = {}
            for row in holdings:
                pdno = str(row.get("pdno") or row.get("PDNO") or "").strip()
                qty_k = int(_f(row.get("hldg_qty") or row.get("HLDG_QTY"), 0))
                avg_k = float(_f(row.get("pchs_avg_pric") or row.get("PCHS_AVG_PRIC"), 0.0))
                if pdno and qty_k > 0:
                    kis_map[pdno] = (qty_k, avg_k)
            # KIS에 있는데 self.pos 없음 → 추가
            for pdno, (qty_k, avg_k) in kis_map.items():
                if pdno not in self.pos:
                    self.pos[pdno] = Position(
                        qty=qty_k, entry_price=avg_k if avg_k > 0 else 1.0,
                        entry_ts=now, max_price=avg_k if avg_k > 0 else 1.0,
                        fill_confirmed=True,
                        manual=True,  # SYNC_ADD: 수동 매수 or state 유실 복구
                    )
                    self._log_diag(now, pdno, "SYNC_ADD", f"KIS qty={qty_k} avg={avg_k:.0f} manual=True")
            # self.pos 있는데 KIS qty==0 → cooldown 120초 후 pop
            for sym in list(self.pos.keys()):
                if sym not in kis_map:
                    # PATCH 2: KIS에 없는 유령 포지션 — 쿨다운 없이 조용히 제거
                    # 쿨다운을 걸면 다음 진입 기회를 막아버림. 유령이라 매도할 게 없으니 쿨다운 불필요.
                    self._log_diag(now, sym, "SYNC_EVICT",
                                   f"KIS qty=0 -> ghost pos cleared (no cooldown)")
                    self.pos.pop(sym, None)
                else:
                    kis_qty = kis_map[sym][0]
                    if self.pos[sym].qty != kis_qty:
                        self._log_diag(now, sym, "SYNC_QTY", f"state={self.pos[sym].qty} kis={kis_qty} overwrite")
                        self.pos[sym].qty = kis_qty
            self._state_validated = True  # on_timer 기존 검증 블록 비활성화
        except Exception as e:
            # PATCH 1: inquire_holdings 실패 시 JSON 포지션을 신뢰할 수 없음
            # 유령 포지션이 즉시 매도 루프를 만드는 것 방지 — 전부 제거하고 재검증 대기
            stale_syms = list(self.pos.keys())
            for sym in stale_syms:
                self.cooldown_until[sym] = time.time() + 300.0  # 5분 쿨다운
                self.pos.pop(sym, None)
            self._log_diag(
                time.time(), "ENGINE", "SYNC_FAIL_CLEAR",
                f"inquire_holdings failed: {type(e).__name__}: {e} "
                f"- ghost pos {len(stale_syms)} cleared: {stale_syms} cooldown=300s"
            )
            self._state_validated = False  # on_timer에서 재검증 시도

    def _reload_watchlist(self, ts_epoch: float):
        if (ts_epoch - self._last_watch_reload_ts) < 1.0:
            return
        self._last_watch_reload_ts = ts_epoch
        try:
            new_watch: set[str] = set()
            try:
                with open(self.watchlist_file, "r", encoding="utf-8") as f:
                    new_watch = {ln.strip() for ln in f if ln.strip()}
            except Exception:
                pass
            with self._state_lock:
                if new_watch != self.watch:
                    self.prev_close_cache = load_cache()
                self.watch = new_watch
                # BUG-020: 워치리스트에서 제거된 종목의 tick 버퍼 정리 (메모리 누수 방지)
                stale = set(self.ticks) - new_watch - set(self.pos)
                for s in stale:
                    del self.ticks[s]
            if new_watch:
                # 전일종가 누락 종목만 API 보충 — BUG-010: 틱 스레드 블로킹 방지로 비동기 처리
                    try:
                        missing = [s for s in new_watch if self._prev_close(s) <= 0]
                        if missing and not self._prev_close_fetch_pending:
                            self._prev_close_fetch_pending = True
                            def _fetch(syms, ts):
                                try:
                                    fresh = ensure_prev_close(syms)
                                    if fresh:
                                        with self._state_lock:
                                            self.prev_close_cache.update(fresh)
                                        recovered = [s for s in syms if fresh.get(s, 0) > 0]
                                        if recovered:
                                            self._prev_close_cache_ok = True
                                            self._log_diag(ts, "ENGINE", "PREV_CLOSE_LOADED",
                                                           f"recovered={recovered} total_cache={len(self.prev_close_cache)}")
                                except Exception as _e:
                                    self._console_msgs.append(
                                          f"{time.strftime('%Y-%m-%d %H:%M:%S')} [WARN] ensure_prev_close async: {_e}")
                                finally:
                                    self._prev_close_fetch_pending = False
                            _t = threading.Thread(target=_fetch, args=(missing, ts_epoch), daemon=True)
                            _t.start()
                    except Exception as e:
                        self._log_diag(ts_epoch, "ENGINE", "PREV_CLOSE_FAIL",
                                       f"{type(e).__name__}: {e}")
        except Exception:
            pass


    def _prev_close(self, sym: str) -> float:
        v = self.prev_close_cache.get(sym, 0.0)
        if not v:
            # 캐시 미스 시 파일에서 재로드 시도
            try:
                from quote_basic import load_cache as _lc
                fresh = _lc()
                if fresh:
                    self.prev_close_cache.update(fresh)
                    v = self.prev_close_cache.get(sym, 0.0)
            except Exception:
                pass
        return float(v or 0.0)

    def _day_rise_pct(self, sym: str, price: float) -> float:
        pc = self._prev_close(sym)
        if pc <= 0:
            return 0.0
        return (price / pc - 1.0) * 100.0

    def _window_stats(self, dq: Deque[Tuple[float, float, float]], now: float, sec: float) -> tuple[float, float, int]:
        st = now - sec
        base = 0.0
        last = 0.0
        trv = 0.0
        cnt = 0
        first_valid = True
        for t, px, vol, *_ in dq:
            if t < st:
                continue
            if first_valid:
                base = px
                first_valid = False
            last = px
            trv += px * vol
            cnt += 1
        if cnt < 2 or base <= 0:
            return 0.0, trv, cnt
        return ((last - base) / base * 100.0), trv, cnt

    def _compute_ofi_window(self, dq: Deque[Tuple[float, float, float]], now: float, sec: float = 10.0) -> float:
        st = now - sec
        buy = 0.0
        sell = 0.0
        prev = None
        for t, px, vol, *_ in dq:
            if t < st:
                if px > 0:
                    prev = px
                continue
            if px <= 0 or vol <= 0:
                continue
            if prev is None:
                prev = px
                continue
            if px > prev:
                buy += vol
            elif px < prev:
                sell += vol
            prev = px
        return buy / max(1.0, sell)

    def _imbalance(self, sym: str) -> float:
        ob = self.book.get(sym) or {}
        bid = _f(ob.get("TOTAL_BIDP_RSQN") or ob.get("total_bidp_rsqn"))
        ask = _f(ob.get("TOTAL_ASKP_RSQN") or ob.get("total_askp_rsqn"))
        den = bid + ask
        return (bid / den) if den > 0 else 0.5

    def _depth_ratio(self, sym: str) -> float:
        ob = self.book.get(sym) or {}
        bid = _f(ob.get("BIDP_RSQN1")) + _f(ob.get("BIDP_RSQN2")) + _f(ob.get("BIDP_RSQN3"))
        ask = _f(ob.get("ASKP_RSQN1")) + _f(ob.get("ASKP_RSQN2")) + _f(ob.get("ASKP_RSQN3"))
        if ask <= 0:
            return 1.0
        return bid / ask

    def _spread_bps(self, sym: str, price: float) -> float | None:
        ob = self.book.get(sym) or {}
        ask1 = _f(ob.get("ASKP1") or ob.get("askp1"))
        bid1 = _f(ob.get("BIDP1") or ob.get("bidp1"))
        if ask1 <= 0 or bid1 <= 0:
            return None
        mid = (ask1 + bid1) / 2.0
        if mid <= 0:
            return None
        return (ask1 - bid1) / mid * 10000.0

    def _pullback_rebound(self, dq: Deque[Tuple[float, float, float]], now: float, price: float) -> float:
        # 급등주는 고점이 더 오래 전에 형성됨 — 60초 고점 기준으로 확장
        high60 = max((px for t, px, *_ in dq if (now - t) <= 60.0), default=price)
        low10 = min((px for t, px, *_ in dq if (now - t) <= 10.0), default=price)
        pulled = low10 <= high60 * (1.0 - max(0.0, self.pullback_pct) / 100.0)
        rebound = price >= low10 * (1.0 + max(0.0, self.pullback_rebound_pct) / 100.0)
        recovery_floor = low10 + (high60 - low10) * 0.5
        return 1.0 if (pulled and rebound and price >= recovery_floor) else 0.0

    def _burst_ratio(self, dq: Deque[Tuple[float, float, float]], now: float) -> float:
        """최근 5초 거래대금 / 직전 5~10초 거래대금 비율."""
        trv5 = sum(px * vol for t, px, vol, *_ in dq if (now - t) <= 5.0)
        trv5_prev = sum(px * vol for t, px, vol, *_ in dq if 5.0 < (now - t) <= 10.0)
        if trv5_prev <= 0:
            return 1.0
        return trv5 / trv5_prev

    # ================================================================
    # [PATCH] 추가 지표 메서드: ATR / RSI / VWAP / Book OFI
    # ================================================================

    def _update_book_ofi(self, sym: str, row: Dict[str, str]):
        """on_orderbook 호출 시 L2 호가 델타 OFI 누적."""
        bid1_px = _f(row.get("BIDP1"))
        bid1_qt = _f(row.get("BIDP_RSQN1"))
        ask1_px = _f(row.get("ASKP1"))
        ask1_qt = _f(row.get("ASKP_RSQN1"))
        if bid1_px <= 0 or ask1_px <= 0:
            return
        prev_b = self._prev_bid1.get(sym, (0.0, 0.0))
        prev_a = self._prev_ask1.get(sym, (0.0, 0.0))
        # Bid 델타
        if prev_b[0] == 0.0:
            bid_delta = 0.0
        elif bid1_px > prev_b[0]:
            bid_delta = bid1_qt
        elif bid1_px == prev_b[0]:
            bid_delta = bid1_qt - prev_b[1]
        else:
            bid_delta = -prev_b[1]
        # Ask 델타
        if prev_a[0] == 0.0:
            ask_delta = 0.0
        elif ask1_px < prev_a[0]:
            ask_delta = ask1_qt
        elif ask1_px == prev_a[0]:
            ask_delta = ask1_qt - prev_a[1]
        else:
            ask_delta = -prev_a[1]
        self._book_ofi_buf[sym].append(bid_delta - ask_delta)
        self._prev_bid1[sym] = (bid1_px, bid1_qt)
        self._prev_ask1[sym] = (ask1_px, ask1_qt)

    def _update_vwap(self, sym: str, price: float, vol: float, ts_epoch: float):
        """on_trade 호출 시 세션 VWAP 누적."""
        today = time.strftime("%Y%m%d", time.localtime(ts_epoch))
        if today != self._vwap_reset_date:
            self._vwap_pv.clear()
            self._vwap_v.clear()
            self._vwap_reset_date = today
        if price > 0 and vol > 0:
            self._vwap_pv[sym] += price * vol
            self._vwap_v[sym]  += vol

    def _vwap(self, sym: str) -> float:
        """세션 VWAP 반환. 데이터 없으면 0."""
        v = self._vwap_v.get(sym, 0.0)
        return (self._vwap_pv[sym] / v) if v > 0 else 0.0

    def _book_ofi_score(self, sym: str) -> float:
        """최근 N회 book OFI 합계를 -1~+1로 정규화."""
        buf = self._book_ofi_buf.get(sym)
        if not buf or len(buf) < 3:
            return 0.0
        total = sum(buf)
        abs_total = sum(abs(v) for v in buf)
        return (total / abs_total) if abs_total > 0 else 0.0

    def _calc_atr(self, sym: str, ts_epoch: float,
                  candle_sec: float = None, n_candles: int = None) -> float:
        """
        틱 기반 True Range ATR.
        candle_sec: 의사캔들 크기(초), n_candles: 사용 캔들 수
        반환: ATR을 현재가 대비 % (atr_pct와 동일 단위)
        """
        if candle_sec is None:
            candle_sec = float(os.getenv("ATR_CANDLE_SEC", "10"))
        if n_candles is None:
            n_candles = int(os.getenv("ATR_N_CANDLES", "6"))
        dq = self.ticks.get(sym)
        if not dq:
            return 0.0
        candles: list = []
        for i in range(n_candles, 0, -1):
            t_end   = ts_epoch - (i - 1) * candle_sec
            t_start = t_end - candle_sec
            prices  = [px for t, px, *_ in dq if t_start <= t < t_end and px > 0]
            if len(prices) >= 2:
                candles.append((min(prices), max(prices), prices[-1]))
        if len(candles) < 2:
            return 0.0
        trs = []
        for i in range(1, len(candles)):
            lo, hi, _ = candles[i]
            prev_c = candles[i - 1][2]
            trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
        if not trs:
            return 0.0
        atr = sum(trs) / len(trs)
        mid = candles[-1][2]
        return (atr / mid * 100.0) if mid > 0 else 0.0

    def _calc_entry_atr(self, sym: str, ts_epoch: float) -> float:
        """enter_position 전용 ATR — 신규 방식 우선, 실패 시 기존 range fallback."""
        atr_new = self._calc_atr(sym, ts_epoch)
        if atr_new > 0:
            return atr_new
        prices_30s = [px for t, px, *_ in self.ticks[sym] if (ts_epoch - t) <= 30.0]
        if len(prices_30s) >= 2:
            mid = (max(prices_30s) + min(prices_30s)) / 2.0
            return (max(prices_30s) - min(prices_30s)) / max(1.0, mid) * 100.0
        return 0.0

    def _calc_rsi(self, sym: str, ts_epoch: float,
                  candle_sec: float = None, period: int = None) -> float:
        """
        틱 기반 RSI.
        candle_sec × period = 필요 히스토리 (기본 10s × 7 = 70초)
        데이터 부족 시 50(중립) 반환 → 게이트 미차단
        """
        if candle_sec is None:
            candle_sec = float(os.getenv("RSI_CANDLE_SEC", "10"))
        if period is None:
            period = int(os.getenv("RSI_PERIOD", "7"))
        dq = self.ticks.get(sym)
        if not dq:
            return 50.0
        closes: list = []
        for i in range(period + 1, 0, -1):
            t_end   = ts_epoch - (i - 1) * candle_sec
            t_start = t_end - candle_sec
            prices  = [px for t, px, *_ in dq if t_start <= t < t_end and px > 0]
            if prices:
                closes.append(prices[-1])
        if len(closes) < period + 1:
            return 50.0
        gains  = [max(0.0,  closes[i] - closes[i-1]) for i in range(1, len(closes))]
        losses = [max(0.0, -(closes[i] - closes[i-1])) for i in range(1, len(closes))]
        avg_gain = sum(gains)  / len(gains)
        avg_loss = sum(losses) / len(losses)
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _gate_rsi(self, sym: str, ts_epoch: float) -> str:
        """RSI 과매수 진입 차단 게이트."""
        if os.getenv("RSI_GATE_ENABLED", "1") != "1":
            return ""
        rsi_ob = float(os.getenv("RSI_OB", "72"))
        rsi = self._calc_rsi(sym, ts_epoch)
        if rsi >= rsi_ob:
            return f"gate_rsi_ob={rsi:.1f}>={rsi_ob:.0f}"
        return ""

    def _gate_vwap(self, sym: str, price: float, ts_epoch: float) -> str:
        """VWAP 아래 진입 차단 게이트."""
        if os.getenv("VWAP_GATE_ENABLED", "1") != "1":
            return ""
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if 900 <= hhmm < 905:
            return ""  # 장 초반 5분 면제
        vwap = self._vwap(sym)
        if vwap <= 0:
            return ""
        below_pct = float(os.getenv("VWAP_BELOW_PCT", "0.3"))
        dist = (price / vwap - 1.0) * 100.0
        if dist < -below_pct:
            return f"gate_vwap_below={dist:.2f}%<-{below_pct}%"
        return ""

    # ================================================================
    # [PATCH END]
    # ================================================================

    @staticmethod
    def _extract_fill_price(j: Dict[str, Any], fallback: float) -> float:
        """주문 응답에서 실체결가 추출. 없으면 fallback(tick price) 반환."""
        for _root in ("output", "output1", "output2"):
            _out = j.get(_root)
            _cands = [_out] if isinstance(_out, dict) else (_out if isinstance(_out, list) else [])
            for _c in _cands:
                if not isinstance(_c, dict):
                    continue
                for _k in ("avg_prvs", "AVG_PRVS", "ccld_unpr", "CCLD_UNPR"):
                    _v = _f(_c.get(_k), 0.0)
                    if _v > 0:
                        return _v
                else:
                    continue
            else:
                continue
        return fallback

    def _safe_order(self, side: str, sym: str, qty: int, ts_epoch: float, price: float, reason: str, ord_dvsn: str = "01", ord_unpr: str = "0") -> Dict[str, Any]:
        # BUG-005: ord_dvsn 기본값 "01"=시장가. 지정가 주문 시 호출부에서 "00"과 ord_unpr 명시 필요.
        try:
            j = order_cash(side, sym, qty, ord_dvsn=ord_dvsn, ord_unpr=ord_unpr)
            self._log_ledger(ts_epoch, side, sym, qty, price, reason, j.get("rt_cd", ""), j.get("msg1", ""))
            return j
        except Exception as e:
            self._log_ledger(ts_epoch, side, sym, qty, price, reason, "EX", f"{type(e).__name__}:{e}")
            return {"rt_cd": "EX", "msg1": str(e)}

    def _is_afterhours_window(self, ts_epoch: float) -> bool:
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        return not (900 <= hhmm < 1530)

    def _confirmed_fill_qty(self, j: Dict[str, Any]) -> int:
        cands = []
        if isinstance(j, dict):
            cands.append(j)
            for rk in ("output", "output1", "output2"):
                out = j.get(rk)
                if isinstance(out, dict):
                    cands.append(out)
                elif isinstance(out, list):
                    cands.extend([it for it in out if isinstance(it, dict)])
        for k in ("tot_ccld_qty", "TOT_CCLD_QTY", "ccld_qty", "CCLD_QTY", "exec_qty", "EXEC_QTY", "filled_qty", "FILLED_QTY"):
            for d in cands:
                v = _f(d.get(k), -1.0)
                if v > 0:
                    return int(v)
        return 0

    def _exit_cooldown(self, reason: str) -> float:
        """청산 이유별 쿨다운 시간 반환."""
        if "panic" in reason:
            return self.cooldown_panic_sec
        if "stop_loss" in reason:
            return self.cooldown_stop_sec
        if "take_profit" in reason or "partial_take" in reason:
            return self.cooldown_take_sec
        if "trail_stop" in reason:
            return self.cooldown_trail_sec
        if "max_hold" in reason:
            return self.cooldown_maxhold_sec
        return self.cooldown_sec

    def _eod_ts(self, ts_epoch: float) -> float:
        """당일 장마감(15:30 KST) timestamp 반환."""
        t = time.localtime(ts_epoch)
        eod = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 15, 30, 0, 0, 0, -1))
        if eod <= ts_epoch:
            # 이미 15:30 지남 — 충분히 큰 값(자정)으로 대체
            eod = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 23, 59, 59, 0, 0, -1))
        return eod

    def _update_market_state(self, ts_epoch: float):
        """보유 포지션 평균 pnl 기반 시장 하락추세 감지 (PROMPT 7)."""
        if not self.pos:
            if self._market_declining and ts_epoch > self._market_declining_until:
                self._market_declining = False
            return
        pnl_vals = [
            (p.last_price / p.entry_price - 1.0) * 100.0
            for p in self.pos.values()
            if p.last_price > 0 and p.entry_price > 0
        ]
        if not pnl_vals:
            return
        min_pos_for_market_signal = max(1, self.max_positions // 2)  # FIX-BUG5: max_positions=1이면 max(2,0)=2로 항상 early return되던 버그 수정
        if len(pnl_vals) < min_pos_for_market_signal:
            if ts_epoch > self._market_declining_until:
                self._market_declining = False
            return
        avg_pnl = sum(pnl_vals) / len(pnl_vals)
        if avg_pnl <= -1.0:
            self._market_declining = True
            self._market_declining_until = ts_epoch + 300.0
        elif ts_epoch > self._market_declining_until:
            self._market_declining = False

    def _prime_cash_status(self):
        now = time.time()
        self._refresh_orderable_cash_sync(now, use_fallback=False)
        self._refresh_orderable_cash_sync(now, use_fallback=True)
        if self._daily_loss_base_cash is None and self._last_orderable_cash is not None:
            self._daily_loss_base_cash = self._last_orderable_cash
            self._log_diag(
                time.time(), "ENGINE", "DAILY_BASE",
                f"base_cash={self._daily_loss_base_cash:.0f}"
            )

    def _refresh_orderable_cash(self, ts_epoch: float, use_fallback: bool = True) -> float | None:
        if (ts_epoch - self._last_orderable_cash_ts) < CASH_REFRESH_INTERVAL and self._last_orderable_cash is not None:
            return self._last_orderable_cash
        # 이미 백그라운드 갱신 중이면 캐시 반환 (메인 스레드 블로킹 방지)
        if getattr(self, '_cash_refresh_pending', False):
            return self._last_orderable_cash
        self._cash_refresh_pending = True
        _health_sym = self.health_cash_symbol

        def _do_refresh(ts, fb):
            try:
                orderable = None
                try:
                    snap = account_cash_snapshot()
                except Exception:
                    snap = {}
                if isinstance(snap, dict):
                    v = _f(snap.get("orderable"), -1.0)
                    if v > 0:
                        orderable = v
                if orderable is None and fb:
                    try:
                        bp = float(account_buying_power(symbol=_health_sym, ord_dvsn="01", price="0"))
                        if bp > 0:
                            orderable = bp
                    except Exception:
                        orderable = None
                # 초기 비정상 표기 방지: 매우 작은 fallback 값은 버리고 마지막 정상값 유지
                if orderable is not None and orderable >= 10000:
                    self._last_orderable_cash = orderable
                    self._last_orderable_cash_ts = ts
                elif orderable is not None and self._last_orderable_cash is None and not fb:
                    self._last_orderable_cash = orderable
                    self._last_orderable_cash_ts = ts
            except Exception:
                pass
            finally:
                self._cash_refresh_pending = False

        threading.Thread(target=_do_refresh, args=(ts_epoch, use_fallback), daemon=True).start()
        return self._last_orderable_cash

    def _refresh_orderable_cash_sync(self, ts_epoch: float, use_fallback: bool = True) -> float | None:
        """동기 버전: enter_position 등 즉시 최신 잔고가 필요한 경우."""
        orderable = None
        try:
            snap = account_cash_snapshot()
        except Exception:
            snap = {}

        if isinstance(snap, dict):
            v = _f(snap.get("orderable"), -1.0)
            if v > 0:
                orderable = v

        if orderable is None and use_fallback:
            try:
                bp = float(account_buying_power(symbol=self.health_cash_symbol, ord_dvsn="01", price="0"))
                if bp > 0:
                    orderable = bp
            except Exception:
                orderable = None

        if orderable is not None and orderable >= 10000:
            self._last_orderable_cash = orderable
            self._last_orderable_cash_ts = ts_epoch
        elif orderable is not None and self._last_orderable_cash is None and not use_fallback:
            self._last_orderable_cash = orderable
            self._last_orderable_cash_ts = ts_epoch

        return self._last_orderable_cash

    # ---------- core strategy ----------
    def score_symbol(self, sym: str, price: float, ts_epoch: float) -> tuple[float, list[str], Dict[str, float]]:
        dq = self.ticks[sym]
        ret10, trv10, _ = self._window_stats(dq, ts_epoch, 10.0)
        ret5, _, _ = self._window_stats(dq, ts_epoch, 5.0)
        _, trv30, _ = self._window_stats(dq, ts_epoch, 30.0)
        ofi = self._compute_ofi_window(dq, ts_epoch, 10.0)
        imb = self._imbalance(sym)
        depth = self._depth_ratio(sym)
        spread_bps_raw = self._spread_bps(sym, price)
        spread_bps = -1.0 if spread_bps_raw is None else spread_bps_raw
        dayrise = self._day_rise_pct(sym, price)
        accel = trv10 / max(1.0, trv30 / 3.0)
        pull_rebound = self._pullback_rebound(dq, ts_epoch, price)

        # B/A 비율 및 30초 추세
        ob_raw = self.book.get(sym) or {}
        _total_bid = _f(ob_raw.get("TOTAL_BIDP_RSQN") or ob_raw.get("total_bidp_rsqn"))
        _total_ask = _f(ob_raw.get("TOTAL_ASKP_RSQN") or ob_raw.get("total_askp_rsqn"))
        ba_ratio = _total_bid / max(1.0, _total_ask)
        _ba_dq = self._ba_history.get(sym)
        if _ba_dq:
            _ba_window = [(t, v) for t, v in _ba_dq if (ts_epoch - t) <= 30.0]
            ba_30s_ago = _ba_window[0][1] if len(_ba_window) >= 2 else ba_ratio
        else:
            ba_30s_ago = ba_ratio
        ba_trend_30s = ba_ratio / max(0.01, ba_30s_ago)

        vi_std = self._vi_prev_std.get(sym, 0.0)
        vi_gap = abs(price - vi_std) / vi_std * 100.0 if vi_std > 0 else 999.0

        # ---- positive groups ----
        ret10_for_score = max(ret10, 0.0) if pull_rebound > 0.0 else ret10

        # [SCORE-1] ofi < 2.0이면 momentum 할인 (flow 없는 가격 상승은 신뢰도 낮음)
        ofi_mom_mult = 1.0 if ofi >= 2.0 else max(0.4, ofi / 2.0)
        momentum_score = (math.sqrt(max(0.0, ret10_for_score)) * 32.0 + math.sqrt(max(0.0, ret5)) * 18.0) * ofi_mom_mult
        # cap 제거 — sqrt가 자연 수렴

        # [SCORE-LIQ] ofi 약한 순수 거래량 종목 할인 — 방향 없는 볼륨은 신뢰도 낮음
        ofi_liq_mult = 1.0 if ofi >= 1.0 else max(0.70, ofi)
        liquidity_score = (math.log1p(max(0.0, trv10)) * 1.8 + math.sqrt(max(0.0, accel - 1.0)) * 14.0) * ofi_liq_mult
        liq_raw = liquidity_score  # 참고용

        # [SCORE-2] spread 넓으면 orderflow 점수 할인 (spread 10bps=1.0, 20bps=0.7, 30bps=0.4, 최저 0.3)
        _sp = max(spread_bps_raw or 0.0, 0.0)
        spread_reliability = max(0.3, 1.0 - (_sp - 10.0) * 0.03) if _sp > 10.0 else 1.0

        # [SCORE-3] ofi 극단값 로그 스케일 완화 (ofi>10이면 로그로)
        ofi_eff = ofi if ofi <= 10.0 else 10.0 + math.log1p(ofi - 10.0) * 4.0
        ofi_boost = min(50.0, max(0.0, ofi_eff - 1.0) * 18.0)
        imbalance_boost = max(-22.0, min(22.0, (imb - 0.5) * 90.0))
        depth_boost = max(-16.0, min(16.0, (depth - 1.0) * 16.0))
        orderflow_score = (ofi_boost + imbalance_boost + depth_boost) * spread_reliability
        ofi_raw = orderflow_score  # 참고용
        # cap 제거 — ofi_eff 이미 log 스케일, 자연 수렴

        structure_score = pull_rebound * 10.0

        burst_ratio = self._burst_ratio(dq, ts_epoch)
        # [SCORE-BURST] burst 강한 종목 우대: burst>=5 → cap 35 (기존 20), 일반은 유지
        burst_cap = 35.0 if burst_ratio >= 5.0 else 20.0
        burst_score = min(burst_cap, max(0.0, burst_ratio - 2.0) * 10.0)

        vi_clear_boost = 0.0
        vi_clear_ts = self._vi_clear_ts.get(sym, 0.0)
        if vi_clear_ts > 0 and 3.0 <= (ts_epoch - vi_clear_ts) <= 15.0:
            vi_clear_boost = 15.0

        orderflow_early_bonus = 0.0
        if ofi >= 2.0 and burst_ratio >= 2.0 and ret10 >= 0.10:  # ret10 상한 제거 — 강한 상승도 보너스
            orderflow_early_bonus = min(15.0, (ofi - 2.0) * 5.0 + (burst_ratio - 2.0) * 3.0)

        # [SCORE-4] ofi_bonus: ofi 강하고 ret5 최소 확인 시 momentum 부족분 보완 (최대 +35)
        # ret10→ret5: 더 빠른 신호 반응 (10s 누적 대신 5s)
        ofi_bonus = 0.0
        if ofi_eff >= 3.0 and ret5 >= 0.15:
            ofi_bonus = min(35.0, (ofi_eff - 3.0) * 5.5 + 8.0)

        # [SCORE-BURST2] momentum_burst_bonus: burst 강하고 ret5 확인 시 초기 급등 포착 가산
        # (017860 유형: burst_ratio=7 + ret5=1% 같은 선행 신호를 score에 반영)
        momentum_burst_bonus = 0.0
        if burst_ratio >= 4.0 and ret5 >= 0.30:
            momentum_burst_bonus = min(20.0, (burst_ratio - 4.0) * 5.0 + (ret5 - 0.30) * 20.0)

        # 호가 미도착 상태: spread/imb/depth 전부 무의미 → orderflow 점수 강제 0
        no_book = (spread_bps_raw is None and imb == 0.5 and depth == 1.0)
        if no_book:
            orderflow_score = 0.0
            ofi_bonus = 0.0
            # ofi_boost도 믿을 수 없음 — ofi 자체를 1.0으로 클램프
            ofi = min(ofi, 1.0)
            ofi_eff = ofi
            ofi_mom_mult = max(0.4, ofi / 2.0)
            # FIX-BUG3: no_book 시 ofi_mom_mult 변경됐으므로 momentum_score 재계산
            momentum_score = (math.sqrt(max(0.0, ret10_for_score)) * 32.0 + math.sqrt(max(0.0, ret5)) * 18.0) * ofi_mom_mult

        positive_total = momentum_score + liquidity_score + orderflow_score + structure_score + burst_score + vi_clear_boost + orderflow_early_bonus + ofi_bonus
        score_pos = positive_total

        # ---- penalties ----
        penalty_dayrise = 0.0
        if dayrise > self.entry_block_dayrise_pct:
            raw_pen = (dayrise - self.entry_block_dayrise_pct) * 4.2
            # ofi 강하면 dayrise가 근거 있는 상승 → 패널티 최대 70% 감면
            ofi_pen_discount = min(0.7, max(0.0, (ofi - 2.0) * 0.07))
            penalty_dayrise = raw_pen * (1.0 - ofi_pen_discount)

        penalty_vi = 0.0
        if vi_gap <= self.vi_guard_pct:
            penalty_vi = min(12.0, (self.vi_guard_pct - vi_gap + 0.02) * 90.0)

        high20 = max((px for t, px, *_ in dq if (ts_epoch - t) <= 20.0), default=price)
        penalty_chase = 0.0
        near_high = (high20 > 0 and price >= high20 * 0.999)
        if near_high and pull_rebound <= 0.0:
            # [SCORE-5] 고점 추격 + 반등 미확인 → 패널티 강화 (5 → 15점)
            penalty_chase = 15.0 + (7.0 if dayrise >= self.entry_chase_penalty_dayrise_pct else 0.0)
            if ret5 < self.buy_ret5_min:
                penalty_chase += 4.0
            if spread_bps_raw is not None and spread_bps_raw > (self.buy_spread_max_bps * 0.8):
                penalty_chase += min(8.0, (spread_bps_raw - self.buy_spread_max_bps * 0.8) * 0.4)

        penalty_score = penalty_dayrise + penalty_vi + penalty_chase
        score = score_pos - penalty_score

        contrib_pos = {
            "momentum": momentum_score,
            "liquidity": liquidity_score,
            "orderflow": orderflow_score,
            "rebound": structure_score,
            "ofi_bonus": ofi_bonus,
        }
        contrib_neg = {
            "dayrise": -penalty_dayrise,
            "vi": -penalty_vi,
            "chase": -penalty_chase,
        }

        reasons = [
            f"mom={momentum_score:.1f}",
            f"liq={liquidity_score:.1f}",
            f"flow={orderflow_score:.1f}",
            f"reb={structure_score:.1f}",
            f"burst={burst_score:.1f}",
            f"pen={penalty_score:.1f}",
        ]
        metrics = {
            "_price_hint": price,       # [PATCH] gate_vwap price 전달용
            "_ofi_spike_exempt": 0.0,     # [PATCH] OFI 스파이크 즉시진입 면제 플래그
            "ret10": ret10,
            "ret5": ret5,
            "trv10": trv10,
            "trv30": trv30,
            "trv_accel": accel,
            "ofi": ofi,
            "imb": imb,
            "depth_ratio": depth,
            "spread_bps": spread_bps,
            "dayrise": dayrise,
            "pull_rebound": pull_rebound,
            "vi_gap": vi_gap,
            "burst_ratio": burst_ratio,
            "vi_clear_boost": vi_clear_boost,
            "recent_high": high20,
            "recent_high_gap_pct": ((high20 - price) / high20 * 100.0) if high20 > 0 else 0.0,
            "near_recent_high": 1.0 if near_high else 0.0,
            "score_pos": score_pos,
            "score_pen": penalty_score,
            "contrib_pos": contrib_pos,
            "contrib_neg": contrib_neg,
            "no_book": 1.0 if no_book else 0.0,
            "liq_raw": liq_raw,
            "ofi_raw": ofi_raw,
            "ba_ratio": ba_ratio,
            "ba_trend_30s": ba_trend_30s,
        }
        return score, reasons, metrics

    # ------------------------------------------------------------------ #
    #  진입 게이트 — 4단계로 분리                                          #
    #  1. _gate_system   : 시스템/세션 상태 (시장과 무관)                   #
    #  2. _gate_hard     : 하드 차단 (수치 무관, 무조건 막음)               #
    #  3. _gate_orderflow: 수급 품질 (ofi/imb/spread/ret10)               #
    #  4. _gate_score    : 점수·모멘텀·포트폴리오 순위                      #
    # ------------------------------------------------------------------ #

    def _gate_system(self, sym: str, ts_epoch: float) -> str:
        """1단계: 시스템·세션 상태. 여기서 막히면 시장 데이터와 무관."""
        if self._trading_halted:
            return "trading_halted"
        if self._prev_close(sym) <= 0:
            return "gate_no_prev_close"
        # KIS 동기화 미완료 — 실계좌 상태 불명 시 중복 보유 방지 (PATCH 3)
        if not self._state_validated:
            _wait = ts_epoch - self._init_ts
            if _wait < 30.0:
                return f"sync_pending={_wait:.0f}s"
        if os.path.exists(self.manual_halt_file):
            return "manual_halt"
        # 거래 시간 외 (BUG-021)
        if not (900 <= int(time.strftime("%H%M", time.localtime(ts_epoch))) < 1530):  # FIX-BUG8: _is_afterhours_window와 동일하게 < 1530
            return "outside_hours"
        # 진입 마감 시각 (기본 10:00 — 9~10시 골든타임 전용)
        _entry_end = int(os.getenv("TRADING_END_HHMM", "1000"))
        if int(time.strftime("%H%M", time.localtime(ts_epoch))) >= _entry_end:
            return "gate_eod"
        if sym not in self.watch:
            return "watchlist_out"
        if len(self.pos) >= self.max_positions:
            return f"max_positions={len(self.pos)}"
        return ""

    def _gate_hard(self, sym: str, metrics: Dict[str, float], ts_epoch: float) -> str:
        """2단계: 하드 차단. 수급·점수와 무관하게 진입 불가 조건."""
        # 쿨다운
        if ts_epoch < self.cooldown_until.get(sym, 0.0):
            return f"cooldown<{self.cooldown_until[sym]-ts_epoch:.0f}s"
        # 손절 연속 횟수별 차단 (FIX-3)
        loss_count = self._loss_streak.get(sym, 0)
        if loss_count >= 2:
            day_end = self._eod_ts(ts_epoch)  # FIX-BUG1: KST 자정이 아닌 장마감(15:30) 기준
            if ts_epoch < day_end:
                return f"loss_streak2_daily_block(count={loss_count})"
        if sym in self._loss_streak_blocked:
            return "loss_streak_blocked"
        # 체결 미확인 재진입 차단 (BUG-023)
        if sym in self._pending_fill:
            if ts_epoch - self._pending_fill[sym] < self.pending_fill_block_sec:
                return "pending_fill_block"
            del self._pending_fill[sym]
        # dayrise 하드 상한
        if metrics.get("dayrise", 0.0) >= self.entry_hard_dayrise_block_pct:
            return "gate_dayrise_hard"
        # 틱 부족 — window stat 신뢰 불가 (BUG-E)
        _min_ticks = int(os.getenv("BUY_MIN_TICKS", "5"))
        if len(self.ticks.get(sym, [])) < _min_ticks:
            return f"gate_tick_count={len(self.ticks.get(sym,[]))}<{_min_ticks}"
        # 60초 내 과도한 상승 후 고점 근처 차단 (PATCH 2)
        _dq = self.ticks.get(sym)
        if _dq and metrics.get("dayrise", 0.0) >= float(os.getenv("NEAR_HIGH_DAYRISE_MIN", "7.0")):
            _low60 = min((px for t, px, *_ in _dq if (ts_epoch - t) <= 60.0), default=0.0)
            if _low60 > 0:
                _rise60 = (metrics.get("recent_high", 0.0) / _low60 - 1.0) * 100.0
                if _rise60 >= float(os.getenv("ENTRY_OVEREXTENSION_BLOCK_PCT", "2.0")):
                    if metrics.get("recent_high_gap_pct", 0.0) < 0.5:
                        _ofi_ok = metrics.get("ofi", 0.0) >= float(os.getenv("OVEREXT_OFI_EXEMPT", "5.0"))
                        _mom_ok = metrics.get("ret10", 0.0) >= float(os.getenv("OVEREXT_RET10_EXEMPT", "0.5"))
                        if not (_ofi_ok and _mom_ok):
                            return f"gate_overextension rise60={_rise60:.1f}% near_high={metrics.get('recent_high_gap_pct',0.0):.2f}%"
        # [PATCH] RSI 과매수 게이트
        rsi_block = self._gate_rsi(sym, ts_epoch)
        if rsi_block:
            return rsi_block
        return ""

    def _gate_orderflow(self, sym: str, metrics: Dict[str, float], ts_epoch: float, price: float = 0.0) -> str:
        """3단계: 수급 품질 + 가격 위치.
        v2 리팩터: 후행 타이밍 게이트(ret10, 2tick) 제거, 가격 위치 게이트 추가.
        """
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        # ── 가격 위치 게이트 (POSITION gates) ────────────────────────────
        # [POS-1] 하락 종목 진입 차단 — dayrise <= -3% 이면 ofi 관계없이 차단
        _falling_block_pct = float(os.getenv("FALLING_STOCK_BLOCK_PCT", "-3.0"))
        _dayrise = metrics.get("dayrise", 0.0)
        if _dayrise <= _falling_block_pct:
            return f"gate_falling dayrise={_dayrise:.1f}%<={_falling_block_pct:.1f}%"

        # [POS-2] 고점 근접 진입 차단 — 20초 고점 대비 0.2% 이내 + 반등 미확인 + 틱 10개 이상
        _near_high_block = os.getenv("NEAR_HIGH_BLOCK_ENABLED", "1") == "1"
        if _near_high_block:
            _tick_count = len(self.ticks.get(sym, []))
            _near_high_min_ticks = int(os.getenv("NEAR_HIGH_MIN_TICKS", "10"))
            if _tick_count >= _near_high_min_ticks:
                _near_high_pct = float(os.getenv("NEAR_HIGH_BLOCK_PCT", "0.2"))
                _recent_high = metrics.get("recent_high", 0.0)
                if _recent_high > 0 and price > 0:
                    _dist_from_high = (_recent_high - price) / _recent_high * 100.0
                    if _dist_from_high < _near_high_pct and metrics.get("pull_rebound", 0.0) <= 0.0:
                        return f"gate_near_high dist={_dist_from_high:.2f}%<{_near_high_pct:.1f}% high={_recent_high:.0f}"

        # ── 거래대금 ────────────────────────────────────────────────────
        effective_trv_min = self.buy_trv10_min * (0.5 if (self.morning_trv_relax and 900 <= hhmm < 920) else 1.0)
        if metrics.get("trv10", 0.0) < max(1.0, effective_trv_min):
            return "gate_trv10"
        # ── spread ──────────────────────────────────────────────────────
        spread_bps = metrics.get("spread_bps", -1.0)
        if spread_bps < 0 and not (900 <= hhmm < 910):
            return "spread_missing"
        if spread_bps > self.buy_spread_max_bps:
            return "gate_spread"
        # ── ofi/imb 최소 기준 (장 초반 면제) ────────────────────────────
        ofi_ok = metrics.get("ofi", 0.0) >= self.buy_ofi_min
        imb_ok = metrics.get("imb", 0.0) >= self.buy_imb_min
        if not (ofi_ok or imb_ok) and not (900 <= hhmm < 910):
            return "gate_ofi_imb"
        # ── orderflow 음수 ──────────────────────────────────────────────
        _orderflow = (metrics.get("contrib_pos") or {}).get("orderflow", 0.0)
        if _orderflow < self.entry_orderflow_min:
            return f"orderflow_negative={_orderflow:.1f}"
        # ── imb 약함 (ofi 극강이면 면제) ────────────────────────────────
        if metrics.get("imb", 0.5) < self.entry_imb_min_gate:
            if metrics.get("ofi", 0.0) < float(os.getenv("IMB_OFI_EXEMPT_THRESHOLD", "8.0")):
                return f"imb_weak={metrics.get('imb', 0.5):.3f}"
        # ── VWAP 이탈 게이트 ────────────────────────────────────────────
        vwap_block = self._gate_vwap(sym, price, ts_epoch)
        if vwap_block:
            return vwap_block
        return ""

    def _gate_score(self, sym: str, score: float, metrics: Dict[str, float], ts_epoch: float) -> str:
        """4단계: 점수·모멘텀·포트폴리오 순위."""
        # 시장 하락추세 시 기준 +15점 강화 (PROMPT 7)
        effective_threshold = self.entry_score_threshold + (15.0 if self._market_declining else 0.0)
        if score < effective_threshold:
            return f"score_too_low={score:.1f}<{effective_threshold:.1f}"
        # momentum 최소값 — flow/burst만 높고 가격 안 움직이는 종목 차단 (FIX-1)
        _mom_score = float((metrics.get("contrib_pos") or {}).get("momentum", 0.0))
        _mom_min = float(os.getenv("ENTRY_MOM_MIN", "20.0"))
        if _mom_score < _mom_min and metrics.get("pull_rebound", 0.0) <= 0.0:
            _ofi_exempt = metrics.get("ofi", 0.0) >= float(os.getenv("GATE_MOM_OFI_EXEMPT", "7.0"))
            _mom_exempt_min = float(os.getenv("GATE_MOM_OFI_MOM_MIN", "14.0"))
            if not (_ofi_exempt and _mom_score >= _mom_exempt_min):
                return f"gate_mom_weak={_mom_score:.1f}<{_mom_min:.1f}"
        return ""

    def should_buy(self, sym: str, score: float, metrics: Dict[str, float], ts_epoch: float) -> tuple[bool, str]:
        err = self._gate_system(sym, ts_epoch)
        if err: return False, err
        err = self._gate_hard(sym, metrics, ts_epoch)
        if err: return False, err
        err = self._gate_orderflow(sym, metrics, ts_epoch, price=metrics.get("_price_hint", 0.0))
        if err: return False, err
        err = self._gate_score(sym, score, metrics, ts_epoch)
        if err: return False, err
        return True, "pass"

    def _top_factor_strings(self, metrics: Dict[str, float]) -> tuple[str, str]:
        pos = metrics.get("contrib_pos", {}) or {}
        neg = metrics.get("contrib_neg", {}) or {}
        top_pos = sorted(((k, float(v)) for k, v in pos.items()), key=lambda kv: kv[1], reverse=True)[:3]
        top_neg = sorted(((k, abs(float(v))) for k, v in neg.items() if float(v) < 0), key=lambda kv: kv[1], reverse=True)[:2]
        pos_s = "[" + ",".join(f"{k}+{v:.1f}" for k, v in top_pos) + "]"
        neg_s = "[" + ",".join(f"{k}-{v:.1f}" for k, v in top_neg) + "]"
        return pos_s, neg_s

    def _position_pct_by_score(self, score: float, metrics: Dict[str, float] | None = None) -> tuple[float, float]:
        # BUG-B 수정: strong(165) > threshold(130) 기준으로 포지션 크기 정상화
        # 점수가 높을수록 크게, threshold 직후는 작게
        if score >= self.entry_score_strong:          # 220+: 최고 신호
            base_pct = min(self.position_pct, 0.25)
        elif score >= self.entry_score_strong - 20.0: # 200+: 강한 신호
            base_pct = min(self.position_pct, 0.18)
        elif score >= self.entry_score_threshold + 15.0: # 195+: 괜찮은 신호
            base_pct = min(self.position_pct, 0.12)
        else:                                         # 180~195: 간신히 통과
            base_pct = min(self.position_pct, 0.07)  # 아주 작게만

        if not metrics:
            return base_pct, 1.0

        spread_bps = metrics.get("spread_bps", 99.0)
        imb = metrics.get("imb", 0.5)
        burst = metrics.get("burst_ratio", 1.0)

        spread_mult = 0.70 if spread_bps < 0 else (1.0 if spread_bps < 10 else (0.85 if spread_bps < 20 else 0.70))  # FIX-BUG6: spread_bps=-1(미수신)은 최악 multiplier 적용
        imb_mult = 0.85 + min(0.30, (imb - 0.5) * 1.0)
        burst_mult = min(1.25, 1.0 + max(0.0, burst - 1.5) * 0.15)
        quality_mult = spread_mult * imb_mult * burst_mult

        final_pct = max(0.05, min(self.position_pct, base_pct * quality_mult))
        return final_pct, quality_mult

    def enter_position(self, sym: str, price: float, score: float, reasons: list[str], metrics: Dict[str, float], ts_epoch: float):
        if sym in self.pos:  # 중복 진입 방지
            return
        if len(self.pos) >= self.max_positions:  # fast-path/_select_and_enter 동시 실행 시 max 초과 방지
            return
        if self._trading_halted:  # FIX-BUG7: on_timer 내 manage_position이 halt 유발 후 _select_and_enter 진입하는 경로 차단
            return
        if ts_epoch < self.cooldown_until.get(sym, 0.0):  # FIX-BUG7: SELL→cooldown 설정 후 stale 후보 재진입 차단
            self._log_diag(ts_epoch, sym, "SKIP", f"enter_position cooldown recheck remain={self.cooldown_until[sym]-ts_epoch:.0f}s")
            return
        if sym in self._session_blacklist:
            return
        if self._is_afterhours_window(ts_epoch):  # BUG-021: 장외 시간 안전망
            return
        # 캐시된 현금 사용 (REST 동기호출 제거 — 9:00 연속 진입 시 이벤트루프 블록 방지)
        # on_timer에서 주기적으로 갱신, 여기서는 로컬 차감만
        orderable_cash = self._last_orderable_cash
        cash = float(orderable_cash or 0.0)
        if cash <= 0:
            # 캐시 없으면 한 번만 동기 조회
            orderable_cash = self._refresh_orderable_cash_sync(ts_epoch, use_fallback=True)
            cash = float(orderable_cash or 0.0)
        available_cash_source = "cached" if cash > 0 else "none"

        # surge 구조: 균등 20% 배분 (총자본 기준, 현재 잔고가 아님)
        # _daily_loss_base_cash = 장 시작 시 총 자본. 이걸 기준으로 20% 계산.
        _base_cash = self._daily_loss_base_cash if self._daily_loss_base_cash and self._daily_loss_base_cash > 0 else cash
        _surge_pct = float(os.getenv("SURGE_ALLOC_PCT", "0.20"))  # 기본 20%
        preopen_budget = self._preopen_budgets.get(sym, 0.0)
        if preopen_budget > 0:
            target_budget = min(preopen_budget, cash)
            pct = target_budget / cash if cash > 0 else 0.0
            quality_mult = 1.0
            budget_source = "preopen"
        else:
            target_budget = min(_base_cash * _surge_pct, cash)
            pct = _surge_pct
            quality_mult = 1.0
            budget_source = "surge_20pct"

        ask1 = _f((self.book.get(sym) or {}).get("ASKP1") or (self.book.get(sym) or {}).get("askp1"))
        effective_price = ask1 if ask1 > 0 else price * 1.005  # BUG-009: ask 기반 예산 계산
        qty = int(target_budget // max(1.0, effective_price))
        capped_qty_reason = ""
        if qty <= 0 and target_budget > 0:
            capped_qty_reason = "budget_below_lot"
        self._log_diag(
            ts_epoch,
            sym,
            "BUY_CASH",
            f"orderable_cash={orderable_cash if orderable_cash is not None else -1:.0f} effective_cash={cash:.0f} source={available_cash_source} "
            f"target_budget={target_budget:.0f} budget_source={budget_source} preopen={preopen_budget:.0f} qty={qty} cap={capped_qty_reason or '-'}",
        )
        if qty <= 0:
            self._log_diag(ts_epoch, sym, "SKIP", f"cash_short cash={cash:.0f} price={price:.0f} budget={target_budget:.0f} source={available_cash_source}")
            return

        reason = f"score={score:.1f} pct={pct:.2f} qmult={quality_mult:.3f} reasons={'|'.join(reasons[:5])}"

        # PAPER_TRADE: 실제 주문 없이 가상 체결
        if os.getenv("PAPER_TRADE", "0") == "1":
            fill_price = price
            filled = qty
            fill_confirmed = True
            atr_pct = self._calc_entry_atr(sym, ts_epoch)  # [PATCH] 개선된 ATR 계산
            self.pos[sym] = Position(qty=filled, entry_price=fill_price, entry_ts=ts_epoch, max_price=fill_price, score=score, reasons=reasons[:5], atr_pct=atr_pct, fill_confirmed=fill_confirmed)
            if self._last_orderable_cash is not None:
                self._last_orderable_cash = max(0.0, self._last_orderable_cash - fill_price * filled)
            self._last_buy_time = ts_epoch
            self._last_buy_symbol = sym
            self._record_event(ts_epoch, "BUY", sym, f"score={score:.1f} qty={qty}")
            self._save_state()
            pos_s, neg_s = self._top_factor_strings(metrics)
            self._log_diag(ts_epoch, sym, "BUY",
                f"[PAPER] score={score:.1f} pos={pos_s} neg={neg_s} "
                f"gate=trv10={metrics.get('trv10',0.0):.0f}/{self.buy_trv10_min:.0f},"
                f"ret10={metrics.get('ret10',0.0):.2f}/{self.buy_ret10_min:.2f},"
                f"ofi={metrics.get('ofi',0.0):.2f}/{self.buy_ofi_min:.2f},"
                f"imb={metrics.get('imb',0.0):.2f}/{self.buy_imb_min:.2f},"
                f"spread={metrics.get('spread_bps',0.0):.2f}/{self.buy_spread_max_bps:.2f},pass=1 "
                f"price={price:.0f} fill_price={fill_price:.0f} qty={qty} "
                f"est_notional={fill_price*qty:.0f} cash={cash:.0f} pct={pct:.3f} quality_mult={quality_mult:.3f} "
                f"metrics=ret5={metrics.get('ret5',0.0):.2f},spread={metrics.get('spread_bps',0.0):.2f},"
                f"dayrise={metrics.get('dayrise',0.0):.2f},recent_high={metrics.get('recent_high',0.0):.0f},"
                f"near_high={metrics.get('near_recent_high',0.0):.0f},pull_rebound={metrics.get('pull_rebound',0.0):.0f}")
            return

        j = self._safe_order("BUY", sym, qty, ts_epoch, price, reason)
        if j.get("rt_cd") != "0":
            self._log_diag(ts_epoch, sym, "BUY_FAIL", str(j.get("msg1", ""))[:160])
            # BUY_FAIL 시 120초 쿨다운 + 캐시 잔고 무효화 → 무한 재시도 방지
            self.cooldown_until[sym] = ts_epoch + 120.0
            self._last_orderable_cash_ts = 0  # 다음 시도 전 실제 잔고 재조회
            # 동일 종목 연속 3회 실패 → 당일 세션 블랙리스트
            self._buy_fail_count[sym] = self._buy_fail_count.get(sym, 0) + 1
            if self._buy_fail_count[sym] >= 3:
                self._session_blacklist.add(sym)
                self._log_diag(ts_epoch, sym, "BUY_BLACKLISTED", f"fail_count={self._buy_fail_count[sym]} → session blacklist")
            return

        filled = self._confirmed_fill_qty(j)
        rt_cd = j.get("rt_cd", "")
        if filled <= 0:
            if rt_cd == "0":
                # FIX-20260309: 시장가 주문 즉시응답엔 체결수량 미포함 — rt_cd=0이면 접수=체결로 간주
                filled = qty
                fill_confirmed = False  # 미체결 의심 — _validate_state_once에서 10초 후 검증
                self._log_diag(ts_epoch, sym, "BUY_FILL_ASSUMED",
                               f"rt_cd=0 filled assumed qty={qty} — position created unconfirmed")
            else:
                # rt_cd != "0": 실패 응답 → 포지션 미생성
                self._log_diag(ts_epoch, sym, "BUY_FILL_UNKNOWN",
                               f"rt_cd={rt_cd} msg={j.get('msg1','')} — position NOT created")
                # BUG-023: 체결 미확인 종목 재진입 차단
                self._pending_fill[sym] = ts_epoch
                self.notifier.send(
                    title=f"⚠️ 매수 체결확인 실패 {sym}",
                    color=0xE67E22,
                    lines=[f"주문접수 실패(rt_cd={rt_cd}) → 포지션 미생성",
                           f"수동 확인 필요: price={price:,.0f} qty={qty}"],
                )
                return
        else:
            fill_confirmed = True
        # NEW-003: 실제 체결단가 추출 시도 (실패 시 tick price로 fallback)
        fill_price = self._extract_fill_price(j, price)
        # PROMPT 1: 진입 시 30초 변동성(ATR) 계산
        prices_30s = [px for t, px, *_ in self.ticks[sym] if (ts_epoch - t) <= 30.0]
        if len(prices_30s) >= 2:
            mid30 = (max(prices_30s) + min(prices_30s)) / 2.0
            atr_pct = (max(prices_30s) - min(prices_30s)) / max(1.0, mid30) * 100.0
        else:
            atr_pct = 0.0
        self.pos[sym] = Position(qty=filled, entry_price=fill_price, entry_ts=ts_epoch, max_price=fill_price, score=score, reasons=reasons[:5], atr_pct=atr_pct, fill_confirmed=fill_confirmed)
        if self._last_orderable_cash is not None:
            # FIX-20260309 BUG-2: fill_price 기준 차감 (price*1.003 과다차감 → ok=7 연속 버그 방지)
            self._last_orderable_cash = max(0.0, self._last_orderable_cash - fill_price * filled)
        self._last_buy_time = ts_epoch
        self._last_buy_symbol = sym
        self._record_event(ts_epoch, "BUY", sym, f"score={score:.1f} qty={qty}")
        self._save_state()
        pos_s, neg_s = self._top_factor_strings(metrics)
        self._log_diag(
            ts_epoch,
            sym,
            "BUY",
            f"score={score:.1f} pos={pos_s} neg={neg_s} gate=trv10={metrics.get('trv10',0.0):.0f}/{self.buy_trv10_min:.0f},ret10={metrics.get('ret10',0.0):.2f}/{self.buy_ret10_min:.2f},ofi={metrics.get('ofi',0.0):.2f}/{self.buy_ofi_min:.2f},imb={metrics.get('imb',0.0):.2f}/{self.buy_imb_min:.2f},spread={metrics.get('spread_bps',0.0):.2f}/{self.buy_spread_max_bps:.2f},pass=1 price={price:.0f} fill_price={fill_price:.0f} qty={qty} est_notional={fill_price*qty:.0f} cash={cash:.0f} pct={pct:.3f} quality_mult={quality_mult:.3f} metrics=ret5={metrics.get('ret5',0.0):.2f},spread={metrics.get('spread_bps',0.0):.2f},dayrise={metrics.get('dayrise',0.0):.2f},recent_high={metrics.get('recent_high',0.0):.0f},near_high={metrics.get('near_recent_high',0.0):.0f},pull_rebound={metrics.get('pull_rebound',0.0):.0f}",
        )
        _pre = self._preopen_data.get(sym, {})
        _gap_disc = _pre.get("expected_gap_pct", 0.0) - metrics.get("dayrise", 0.0) if _pre else 0.0
        self.notifier.send(
            title=f"✅ {'[PAPER] ' if os.getenv('PAPER_TRADE','0')=='1' else ''}매수 {sym}",
            color=0x2ECC71,
            lines=[
                f"진입가={price:,.0f}원  수량={qty}주  금액={price*qty:,.0f}원",
                f"score={score:.1f}  배분={pct*100:.0f}%  gap_disc={_gap_disc:+.1f}%",
                f"dayrise={metrics.get('dayrise',0.0):.2f}%  ofi={metrics.get('ofi',0.0):.2f}",
            ],
        )

    def manage_position(self, sym: str, price: float, ts_epoch: float, force_reason: str = ""):
        p = self.pos.get(sym)
        if not p:
            return
        p.last_price = price
        p.last_price_ts = ts_epoch  # BUG-014
        if price > p.max_price:
            p.max_price = price
        pnl_pct = (price / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
        p.max_pnl_pct = max(p.max_pnl_pct, pnl_pct)
        p.min_pnl_pct = min(p.min_pnl_pct, pnl_pct)

        # MANUAL: 수동 매수 포지션은 강제청산 포함 전부 제외, 가격 추적만 함
        if p.manual:
            return

        hold_sec = max(0.0, ts_epoch - p.entry_ts)
        reason = ""

        # BUG-D 수정: fill_confirmed=False 포지션은 15초간 emergency 손절만 적용
        if not p.fill_confirmed and hold_sec < 15.0:
            if pnl_pct <= -abs(self.stop_loss_emergency_pct):
                reason = "stop_loss_panic_unconfirmed"
            if not reason:
                return

        early_stop_pct = abs(self.stop_loss_pct) * max(1.0, self.stop_loss_early_relax_mult)
        if hold_sec < self.stop_loss_early_grace_sec:
            if pnl_pct <= -abs(self.stop_loss_emergency_pct):
                reason = "stop_loss_panic"
            elif pnl_pct <= -early_stop_pct:
                reason = "stop_loss_early"
        elif pnl_pct <= -abs(self.stop_loss_pct):
            reason = "stop_loss_after_grace"

        # ── 상한가 근접 전량 TP — 기준가 대비 +N% 도달 시 split/grace 무관 즉시 전량 매도 ──
        # upper_limit = 기준가 * 1.30, trigger = 기준가 * (1 + N/100) = upper_limit * (1+N/100) / 1.30
        if not reason and self._limit_up_tp_pct > 0 and p.upper_limit > 0:
            _lu_trigger = p.upper_limit * (1.0 + self._limit_up_tp_pct / 100.0) / 1.30
            if price >= _lu_trigger:
                reason = "limit_up_tp"
                self._log_diag(ts_epoch, sym, "LIMIT_UP_TP",
                               f"price={price:.0f} >= trigger={_lu_trigger:.0f} "
                               f"upper_limit={p.upper_limit:.0f} pnl={pnl_pct:.2f}% qty={p.qty}")

        if not reason and hold_sec >= self.exit_grace_sec:
            # ── 50/50 분할청산: B물량 TP (A물량 trail 청산 완료 후) ──
            if self._split_exit_enabled and p.split_a_sold and pnl_pct >= self._split_b_tp_pct:
                reason = "split_b_tp"
                self._log_diag(ts_epoch, sym, "SPLIT_B_TP",
                               f"pnl={pnl_pct:.2f}% tp={self._split_b_tp_pct:.1f}% qty={p.qty}")

            # ── trail ──
            if not reason:
                _trail_ratio = float(os.getenv("TRAIL_DROP_RATIO", "0.25"))
                _trail_max = float(os.getenv("TRAIL_DROP_MAX_PCT", "5.0"))
                _base_drop = abs(self.trail_drop_pct)
                _peak = p.max_pnl_pct
                _scaled_drop = max(_base_drop, min(_trail_max, _peak * _trail_ratio))
                trail_stop = p.max_price * (1.0 - _scaled_drop / 100.0)
                if p.max_pnl_pct >= abs(self.trail_arm_pct) and price <= trail_stop:
                    # 분할청산: B물량만 남았으면 trail 무시 (B는 SL/TP/장마감만)
                    if self._split_exit_enabled and p.split_a_sold:
                        pass  # B물량은 trail 적용 안함
                    elif self._split_exit_enabled and not p.split_a_sold and p.qty < 2:
                        pass  # qty=1: 분할 불가 → B물량 전용 (trail 무시, SL/TP/장마감만)
                    elif self._split_exit_enabled and not p.split_a_sold and p.qty >= 2:
                        # A물량만 trail 매도 (절반)
                        self._log_diag(ts_epoch, sym, "TRAIL_FIRE",
                                       f"SPLIT_A max_pnl={p.max_pnl_pct:.2f}% arm={self.trail_arm_pct:.2f}% "
                                       f"trail_drop={_scaled_drop:.2f}% trail_stop={trail_stop:.0f} "
                                       f"price={price:.0f} pnl={pnl_pct:.2f}% max_price={p.max_price:.0f}")
                        split_qty = p.qty // 2
                        if split_qty > 0:
                            if os.getenv("PAPER_TRADE", "0") == "1":
                                fee_a = price * split_qty * 0.0023
                                pnl_a = (price - p.entry_price) * split_qty - fee_a
                                self._daily_realized_pnl += pnl_a
                                p.qty -= split_qty
                                p.split_a_sold = True
                                if self._last_orderable_cash is not None:
                                    self._last_orderable_cash += price * split_qty * (1 - 0.0023)
                                self._record_event(ts_epoch, "PARTIAL_SELL", sym,
                                                   f"split_a_trail qty={split_qty} pnl={pnl_pct:.2f}%")
                                self._save_state()
                                self._log_diag(ts_epoch, sym, "SPLIT_A_SOLD",
                                    f"[PAPER] qty={split_qty} remain={p.qty} pnl={pnl_pct:.2f}%")
                                self.notifier.send(
                                    title=f"📊 A물량 trail 청산 {sym}",
                                    color=0x3498DB,
                                    lines=[
                                        f"A물량={split_qty}주 청산  B물량={p.qty}주 홀드",
                                        f"pnl={pnl_pct:+.2f}%  peak={p.max_pnl_pct:+.2f}%",
                                        f"B목표: TP={self._split_b_tp_pct:.0f}% / SL={self.stop_loss_pct:.1f}% / 장마감",
                                    ],
                                )
                                return
                            try:
                                qty_sell = max(0, int(sellable_qty(sym)))
                            except Exception:
                                qty_sell = p.qty
                            sell_qty = min(split_qty, qty_sell)
                            if sell_qty <= 0:
                                p.split_a_sold = True
                                return
                            j_a = self._safe_order("SELL", sym, sell_qty, ts_epoch, price, "split_a_trail")
                            if j_a.get("rt_cd") == "0":
                                _sp_a = self._extract_fill_price(j_a, price)
                                fee_a = _sp_a * sell_qty * 0.0023
                                pnl_a = (_sp_a - p.entry_price) * sell_qty - fee_a
                                self._daily_realized_pnl += pnl_a
                                p.qty -= sell_qty
                                p.split_a_sold = True
                                if self._last_orderable_cash is not None:
                                    self._last_orderable_cash += _sp_a * sell_qty * (1 - 0.0023)
                                self._record_event(ts_epoch, "PARTIAL_SELL", sym,
                                                   f"split_a_trail qty={sell_qty} pnl={pnl_pct:.2f}%")
                                self._save_state()
                                self._log_diag(ts_epoch, sym, "SPLIT_A_SOLD",
                                    f"qty={sell_qty} remain={p.qty} pnl={pnl_pct:.2f}%")
                                self.notifier.send(
                                    title=f"📊 A물량 trail 청산 {sym}",
                                    color=0x3498DB,
                                    lines=[
                                        f"A물량={sell_qty}주 청산  B물량={p.qty}주 홀드",
                                        f"pnl={pnl_pct:+.2f}%  peak={p.max_pnl_pct:+.2f}%",
                                        f"B목표: TP={self._split_b_tp_pct:.0f}% / SL={self.stop_loss_pct:.1f}% / 장마감",
                                    ],
                                )
                                return
                            else:
                                # 매도 실패 → 상태 안 건드림, 다음 틱에서 재시도
                                self._log_diag(ts_epoch, sym, "SPLIT_A_FAIL",
                                    f"sell failed, will retry next tick qty={sell_qty}")
                                return
                    else:
                        reason = "trail_stop"
                        self._log_diag(ts_epoch, sym, "TRAIL_FIRE",
                                       f"max_pnl={p.max_pnl_pct:.2f}% arm={self.trail_arm_pct:.2f}% "
                                       f"trail_drop={_scaled_drop:.2f}%(base={_base_drop:.2f}x{_scaled_drop/_base_drop:.1f}) "
                                       f"trail_stop={trail_stop:.0f} price={price:.0f} pnl={pnl_pct:.2f}% max_price={p.max_price:.0f}")

            if not reason and hold_sec >= self.max_hold_sec:
                reason = "max_hold"

        if not reason and force_reason:
            reason = force_reason
        if not reason:
            return

        # PAPER_TRADE: 실제 매도 없이 가상 체결 (sellable_qty 호출 전에 처리)
        if os.getenv("PAPER_TRADE", "0") == "1":
            qty = p.qty
            fee = price * qty * 0.0023
            pnl = (price - p.entry_price) * qty - fee
            self._daily_realized_pnl += pnl
            if self.loss_streak_block_enabled:
                if "stop_loss" in reason:
                    self._loss_streak[sym] += 1
                else:
                    self._loss_streak[sym] = 0
            if self.loss_streak_block_enabled and self._loss_streak.get(sym, 0) >= 2:
                self._loss_streak_blocked.add(sym)
                self.cooldown_until[sym] = self._eod_ts(ts_epoch)
                self._log_diag(ts_epoch, sym, "LOSS_STREAK_BLOCK", f"streak={self._loss_streak[sym]} reason={reason}")
            else:
                self.cooldown_until[sym] = ts_epoch + self._exit_cooldown(reason)
            if self._last_orderable_cash is not None:
                self._last_orderable_cash += price * qty * (1 - 0.0023)
            self.pos.pop(sym, None)
            self._last_sell_time = ts_epoch
            self._last_sell_symbol = sym
            self._record_event(ts_epoch, "SELL", sym, reason)
            self._save_state()
            self._log_diag(ts_epoch, sym, "SELL",
                f"[PAPER] reason={reason} hold={hold_sec:.1f}s pnl={pnl_pct:.2f}% "
                f"entry={p.entry_price:.0f} price={price:.0f} qty={qty} "
                f"peak={p.max_pnl_pct:.2f}% min={p.min_pnl_pct:.2f}% "
                f"atr={p.atr_pct:.2f} sl={self.stop_loss_pct:.2f} "
                f"grace_stop={1 if hold_sec < self.stop_loss_early_grace_sec else 0}")
            return

        try:
            qty_sell = max(0, int(sellable_qty(sym)))
        except Exception as e:
            p.sellable_zero_count += 1
            self._log_diag(ts_epoch, sym, "SELL_WARN",
                           f"sellable_qty_api_error count={p.sellable_zero_count}/3 err={e}")
            return
        qty = min(qty_sell, p.qty)
        if qty <= 0:
            p.sellable_zero_count += 1
            self._log_diag(ts_epoch, sym, "SELL_WARN",
                           f"sellable_qty=0 count={p.sellable_zero_count}/3 reason={reason}")
            if p.sellable_zero_count < 3:
                return  # 재확인 대기, PnL 미반영
            # 3회 연속 → EVICT
            fee = price * p.qty * 0.0023  # BUG-017: 거래세+수수료
            pnl = (price - p.entry_price) * p.qty - fee
            self._daily_realized_pnl += pnl
            self.pos.pop(sym, None)
            self.cooldown_until[sym] = self._eod_ts(ts_epoch)  # 체결 의심 → 당일 재진입 완전 차단
            self._save_state()
            self._log_diag(ts_epoch, sym, "STATE_CLEAN",
                           f"sellable_qty_zero_3x pnl_est={pnl:.0f} cooldown=eod")
            self.notifier.send(
                title=f"⚠️ 매도불가 3회 — 포지션 강제퇴출 {sym}",
                color=0xE74C3C,
                lines=[f"sellable_qty=0 연속 3회 → 엔진에서 제거",
                       f"실계좌 수동 확인 필요! price={price:,.0f}"],
            )
            return

        j = self._safe_order("SELL", sym, qty, ts_epoch, price, reason)
        if j.get("rt_cd") != "0":
            p.sell_fail_count += 1
            self._log_diag(ts_epoch, sym, "SELL_FAIL", f"attempt={p.sell_fail_count} {str(j.get('msg1', ''))[:140]}")
            if p.sell_fail_count >= 3:
                fee = price * p.qty * 0.0023  # BUG-017
                pnl = (price - p.entry_price) * p.qty - fee
                self._daily_realized_pnl += pnl
                self._log_ledger(ts_epoch, "SELL", sym, p.qty, price,
                                 f"EVICT_sell_fail_{p.sell_fail_count}", "EVICT", "force_evicted")
                if self._daily_loss_base_cash and self._daily_loss_base_cash > 0:
                    loss_pct = max(0.0, -self._daily_realized_pnl) / self._daily_loss_base_cash * 100.0
                    if not self._trading_halted and loss_pct >= self.daily_loss_limit_pct:
                        self._trading_halted = True
                        self._log_diag(
                            ts_epoch, "ENGINE", "HALT",
                            f"daily_loss_pct={loss_pct:.2f} limit={self.daily_loss_limit_pct:.2f} pnl={self._daily_realized_pnl:.0f}"
                        )
                if self.loss_streak_block_enabled:
                    if "stop_loss" in reason:
                        self._loss_streak[sym] += 1
                    else:
                        self._loss_streak[sym] = 0
                evict_entry_price = p.entry_price
                evict_entry_ts = p.entry_ts
                evict_qty = p.qty
                evict_fail_count = p.sell_fail_count
                self.pos.pop(sym, None)
                if not self._trading_halted:
                    self._trading_halted = True
                    self._log_diag(ts_epoch, "ENGINE", "HALT",
                                   f"sell_fail_evict sym={sym} sell_fail_count={evict_fail_count} — 수동확인 필요")
                # NEW-004: daily_loss halt 여부와 무관하게 sell_fail evict 알림 항상 발송
                self.notifier.send(
                    title="🚨 매도 API 3회 실패 — 거래 자동중지",
                    color=0xE74C3C,
                    lines=[
                        f"종목: {sym}  수량: {evict_qty}주  가격: {price:,.0f}",
                        f"실계좌에 미매도 잔고 남아 있을 수 있음",
                        f"수동 확인 후 엔진 재시작 필요",
                    ],
                )
                if self.loss_streak_block_enabled and self._loss_streak.get(sym, 0) >= 2:
                    self._loss_streak_blocked.add(sym)
                    self.cooldown_until[sym] = self._eod_ts(ts_epoch)
                else:
                    self.cooldown_until[sym] = ts_epoch + self._exit_cooldown(reason)
                # PATCH-SL-WATCH: EVICT 경로도 stop_loss면 워치리스트 블랙리스트 등록
                if "stop_loss" in reason:
                    try:
                        _register_sl_blacklist(sym)
                    except Exception:
                        pass
                self._last_sell_time = ts_epoch
                self._last_sell_symbol = sym
                self._record_event(ts_epoch, "SELL", sym, f"EVICT_{reason}")
                self._save_state()
                pnl_pct_evict = (price / evict_entry_price - 1.0) * 100.0 if evict_entry_price > 0 else 0.0
                hold_sec_evict = ts_epoch - evict_entry_ts
                self._log_diag(
                    ts_epoch, sym, "SELL",
                    f"reason=EVICT_{reason} hold={hold_sec_evict:.1f}s pnl={pnl_pct_evict:.2f}% "
                    f"entry={evict_entry_price:.0f} price={price:.0f} qty={evict_qty}"
                )
                self._log_diag(ts_epoch, sym, "SELL_EVICT", f"evicted sell_fail_count={evict_fail_count}")
            return

        # SELL fill_price 추출
        sell_price = self._extract_fill_price(j, price)
        if sell_price != price:
            self._log_diag(ts_epoch, sym, "SELL_FILL_PRICE", f"tick={price:.0f} fill={sell_price:.0f} diff={sell_price-price:+.0f}")

        fee = sell_price * qty * 0.0023  # BUG-017: 거래세+수수료
        pnl = (sell_price - p.entry_price) * qty - fee
        self._daily_realized_pnl += pnl
        if self._daily_loss_base_cash and self._daily_loss_base_cash > 0:
            loss_pct = max(0.0, -self._daily_realized_pnl) / self._daily_loss_base_cash * 100.0
            if not self._trading_halted and loss_pct >= self.daily_loss_limit_pct:
                self._trading_halted = True
                self._log_diag(
                    ts_epoch, "ENGINE", "HALT",
                    f"daily_loss_pct={loss_pct:.2f} limit={self.daily_loss_limit_pct:.2f} pnl={self._daily_realized_pnl:.0f}"
                )
                self.notifier.send(
                    title="🚨 일일 손실 한도 도달 — 거래 중지",
                    color=0xE74C3C,
                    lines=[
                        f"누적손실={loss_pct:.2f}%  한도={self.daily_loss_limit_pct:.2f}%",
                        f"실현PnL={self._daily_realized_pnl:,.0f}원",
                    ],
                )

        # PROMPT 3: 연속손절 추적
        if self.loss_streak_block_enabled:
            if "stop_loss" in reason:
                self._loss_streak[sym] += 1
            else:
                self._loss_streak[sym] = 0

        # PROMPT 3: 쿨다운 차별화 + 연손절 2회 시 당일 블락
        if self.loss_streak_block_enabled and self._loss_streak.get(sym, 0) >= 2:
            self._loss_streak_blocked.add(sym)
            self.cooldown_until[sym] = self._eod_ts(ts_epoch)
            self._log_diag(ts_epoch, sym, "LOSS_STREAK_BLOCK", f"streak={self._loss_streak[sym]} reason={reason}")
        else:
            self.cooldown_until[sym] = ts_epoch + self._exit_cooldown(reason)

        # PATCH-SL-WATCH: stop_loss 청산 시 워치리스트 당일 블랙리스트 등록
        if "stop_loss" in reason:
            try:
                _register_sl_blacklist(sym)
            except Exception:
                pass

        self.pos.pop(sym, None)
        self._last_sell_time = ts_epoch
        self._last_sell_symbol = sym
        self._last_orderable_cash_ts = 0.0  # 매도 직후 현금 즉시 REST 갱신
        self._record_event(ts_epoch, "SELL", sym, reason)
        self._save_state()

        sell_pnl_pct = (sell_price / p.entry_price - 1.0) * 100.0 if p.entry_price > 0 else 0.0
        self._log_diag(
            ts_epoch,
            sym,
            "SELL",
            f"reason={reason} hold={hold_sec:.1f}s pnl={sell_pnl_pct:.2f}% entry={p.entry_price:.0f} price={sell_price:.0f} qty={qty} peak={p.max_pnl_pct:.2f}% min={p.min_pnl_pct:.2f}% atr={p.atr_pct:.2f} sl={self.stop_loss_pct:.2f} grace_stop={1 if hold_sec < self.stop_loss_early_grace_sec else 0}",
        )
        self.notifier.send(
            title=f"{'📈' if sell_pnl_pct >= 0 else '📉'} {'[PAPER] ' if os.getenv('PAPER_TRADE','0')=='1' else ''}매도 {sym}",
            color=0x2ECC71 if sell_pnl_pct >= 0 else 0xE74C3C,
            lines=[
                f"진입가={p.entry_price:,.0f}원  청산가={sell_price:,.0f}원  수량={qty}주",
                f"pnl={sell_pnl_pct:+.2f}%  hold={hold_sec:.0f}s  reason={reason}",
                f"peak={p.max_pnl_pct:+.2f}%  min={p.min_pnl_pct:+.2f}%  score={p.score:.0f}",
            ],
        )

    # ---------- event handlers ----------
    def on_preopen_tick(self, row: Dict[str, str], ts_epoch: float):
        """H0STANC0 동시호가 틱 처리 — 8:50~9:00 예상체결가/잔량 추적."""
        sym = row.get("MKSC_SHRN_ISCD", "")
        if not sym:
            return
        bid = _f(row.get("TOTAL_BIDP_RSQN"))
        ask = _f(row.get("TOTAL_ASKP_RSQN"))
        price = _f(row.get("STCK_PRPR"))
        # KIS 기준가 역산 (prev_close.json 의존 제거)
        prdy_vrss_sign = row.get("PRDY_VRSS_SIGN", "3")
        prdy_vrss = _f(row.get("PRDY_VRSS"))
        if prdy_vrss_sign in ("2", "1"):
            prev_close = price - prdy_vrss
        elif prdy_vrss_sign in ("5", "4"):
            prev_close = price + prdy_vrss
        else:
            prev_close = price
        if prev_close <= 0:
            prev_close = self._prev_close(sym)  # fallback
        expected_gap_pct = ((price / prev_close - 1.0) * 100.0) if prev_close > 0 and price > 0 else 0.0
        ba_ratio = bid / max(1.0, ask)
        acml_vol = _f(row.get("ACML_VOL"))
        pvol_rate_pre = _f(row.get("PRDY_VOL_VRSS_ACML_VOL_RATE"))  # 전일거래량 대비율(%)
        entry = {
            "ba_ratio": ba_ratio,
            "expected_gap_pct": expected_gap_pct,
            "bid": bid,
            "ask": ask,
            "price": price,
            "ts": ts_epoch,
            "acml_vol": acml_vol,
            "pvol_rate_pre": pvol_rate_pre,
        }
        # 히스토리: (ts, expected_gap_pct, ba_ratio)
        if sym not in self._preopen_history:
            self._preopen_history[sym] = []
        self._preopen_history[sym].append((ts_epoch, expected_gap_pct, ba_ratio))
        hist = self._preopen_history[sym]
        # gap_slope: 첫 관측 vs 마지막 관측
        if len(hist) >= 2 and (hist[-1][0] - hist[0][0]) > 0:
            entry["gap_slope"] = (hist[-1][1] - hist[0][1]) / ((hist[-1][0] - hist[0][0]) / 60.0)  # %/분
        else:
            entry["gap_slope"] = 0.0
        # ba_trend_3min: 3분 전 ba_ratio 대비 현재 비율 (>1 = 매수벽 증가 추세)
        _3min_ago = ts_epoch - 180.0
        _old_ba = next((h[2] for h in hist if h[0] >= _3min_ago), None)
        if _old_ba is not None and _old_ba > 0.01:
            entry["ba_trend_3min"] = ba_ratio / _old_ba
        else:
            entry["ba_trend_3min"] = 1.0
        # _state_lock: scanner thread(PRE_SUB)에서 dict comprehension 중 변경 방지
        with self._state_lock:
            self._preopen_data[sym] = entry

    def _rebuild_preopen_whitelist(self, ts_epoch: float):
        """_preopen_data 기반 퀄리티 스코어 산출 → 상위 N개 whitelist + 예산 사전배분."""
        # 스캐너 스코어 로드 — preopen quality에 일정 배율로 합산
        _scanner_scores: dict = {}
        try:
            from scanner_theme import get_last_scores
            _scanner_scores = get_last_scores()
        except Exception:
            pass
        _scanner_weight = float(os.getenv("PREOPEN_SCANNER_WEIGHT", "0.3"))
        # 전일 거래대금 캐시 (하드게이트용)
        _prev_cache_stocks: dict = {}
        try:
            from scanner_theme import _load_prev_cache
            _pc = _load_prev_cache()
            _prev_cache_stocks = _pc.get("stocks", {})
        except Exception:
            pass
        _n = int(os.getenv("PREOPEN_WHITELIST_N", str(self.max_positions)))
        _whitelist_n = _n
        _budget_n = _n
        # 대형주 블랙리스트 (scanner와 동일)
        _largecap_bl = set(s.strip() for s in os.getenv(
            "WATCH_LARGECAP_BLACKLIST",
            "005930,000660,035420,005380,005490,051910,006400,035720,068270,028260"
        ).split(",") if s.strip())
        scored: list[tuple[str, float, str]] = []
        _gap_cap = float(os.getenv("PREOPEN_GAP_CAP_PCT", "20.0"))
        _ba_min = float(os.getenv("PREOPEN_BA_MIN", "5.0"))
        _tr_min = float(os.getenv("PREOPEN_TR_MIN", "3e9"))  # 전일 거래대금 최소 30억
        dropped: list[str] = []
        # 교집합 종목(watchlist): 하드게이트 바이패스 — 이미 테마+랭크 이중 검증 통과
        _intersect = set(self.watch) if self.watch else set()
        for sym, d in self._preopen_data.items():
            if not sym or len(sym) != 6 or not sym.isdigit():
                continue  # 오염 심볼 제거
            if sym in _largecap_bl:
                continue
            gap_pct = d.get("expected_gap_pct", 0.0)
            ba = d.get("ba_ratio", 0.0)
            slope = d.get("gap_slope", 0.0)
            pvol_pre = d.get("pvol_rate_pre", 0.0)
            ba_trend = d.get("ba_trend_3min", 1.0)
            _is_intersect = sym in _intersect
            # 교집합 종목은 하드게이트 바이패스 → 스코어링만 수행
            if not _is_intersect:
                # 갭 마이너스 종목 제외 — 전일 대비 하락 출발은 모멘텀 매매 부적합
                if gap_pct < 0.3:
                    dropped.append(f"{sym} gap={gap_pct:+.1f}%<0.3")
                    continue
                # 고갭 종목 제외 — 이미 상한가 근접, 추가 상승 여력 없음
                if gap_pct > _gap_cap:
                    dropped.append(f"{sym} gap={gap_pct:+.1f}%>{_gap_cap:.0f}")
                    continue
                # 매수벽 약한 종목 제외 — 거래량/유동성 부족 가능성
                if ba < _ba_min:
                    dropped.append(f"{sym} ba={ba:.1f}<{_ba_min:.0f}")
                    continue
                # 전일 거래대금 하드게이트 — 캐시에 없거나 거래대금 부족이면 탈락
                _tr_v = _prev_cache_stocks.get(sym, {}).get("tr_value", 0)
                if _prev_cache_stocks:
                    if _tr_v < _tr_min:
                        dropped.append(f"{sym} tr={_tr_v/1e8:.0f}억<{_tr_min/1e8:.0f}")
                        continue
            q = 0.0
            # ── 스캐너 스코어 보너스 — 테마빈도/거래대금/모멘텀 반영 ──
            _ss = _scanner_scores.get(sym, 0.0)
            q += _ss * _scanner_weight
            # ── gap_pct 점수 (0~40) — 연속 곡선, 8~15% 피크 ──
            # 기존: 5~15%=30, >15%=0 → 007340(gap=27%, +32%)이 0점 받는 버그
            # 수정: 연속 점수, 고갭도 감점만(0점 아님)
            if gap_pct < 3.0:
                q += gap_pct * 3.0                    # 0.3%→0.9, 2%→6
            elif gap_pct < 8.0:
                q += 9.0 + (gap_pct - 3.0) * 3.6     # 3%→9, 8%→27
            elif gap_pct <= 15.0:
                q += 27.0 + (gap_pct - 8.0) * 1.86   # 8%→27, 15%→40 (피크)
            elif gap_pct <= 30.0:
                q += 40.0 - (gap_pct - 15.0) * 0.5   # 15%→40, 30%→32.5
            else:
                q += 25.0                              # 30%+ 플로어
            # ── ba_ratio 점수 (0~30) — 매수벽 크기: 3/26 검증에서 ba가 시가후 수익과 최고 상관 ──
            q += min(30.0, ba * 2.5)
            # ── ba_trend 보너스 (0~8) ──
            if ba_trend >= 1.5:
                q += 8.0
            elif ba_trend >= 1.2:
                q += 4.0
            # ── gap_slope 점수 (0~25) — 기대갭 상승 속도 ──
            q += min(25.0, max(0.0, slope) * 12.0)
            # ── gap×ba 시너지 보너스 (0~15) — 고갭+고ba = 강한 확신 ──
            # 007340: gap=27%, ba=32 → 시너지 15점 → 상위 진입
            if gap_pct >= 5.0 and ba >= 5.0:
                q += min(15.0, (gap_pct / 10.0) * (ba / 10.0) * 3.0)
            # pvol_rate_pre: H0STANC0 동시호가에서 항상 0 — 장전이라 전일비교 불가
            # 10점 배정 제거 (유효 데이터만 사용)
            _ix_tag = " INTERSECT" if _is_intersect else ""
            scored.append((sym, q, f"ba={ba:.1f} ba_trend={ba_trend:.2f} slope={slope:.2f} gap={gap_pct:.1f}% scan={_ss:.0f}*{_scanner_weight}{_ix_tag}"))
        scored.sort(key=lambda x: -x[1])
        whitelist_syms = scored[:_whitelist_n]
        budget_syms = scored[:_budget_n]
        self._preopen_whitelist = {s for s, _, _ in whitelist_syms}

        # ── 시장 분위기 감지: 동시호가 통과율 기반 배분 축소 ──
        # pool 대비 통과율이 낮으면 전체적으로 매수세가 약한 장
        pool_size = len(self._preopen_data)
        pass_rate = len(scored) / max(1, pool_size)
        # pass_rate >= 0.4 → mood=1.0 (정상), <= 0.2 → mood=0.5 (약세 절반배분)
        if pass_rate >= 0.4:
            market_mood = 1.0
        elif pass_rate <= 0.2:
            market_mood = 0.5
        else:
            market_mood = 0.5 + (pass_rate - 0.2) * 2.5  # 선형 보간
        self._log_diag(ts_epoch, "ENGINE", "PREOPEN_MOOD",
                       f"pool={pool_size} passed={len(scored)} rate={pass_rate:.2f} mood={market_mood:.2f}")

        # ── 예산 사전배분: surge 구조 → 균등 20% ──
        self._preopen_budgets.clear()
        cash = float(self._last_orderable_cash or 0)
        if cash <= 0:
            cash = float(os.getenv("PREOPEN_FALLBACK_CASH", "2000000"))
        _surge_pct = float(os.getenv("SURGE_ALLOC_PCT", "0.20"))
        _per_slot = cash * _surge_pct
        self._log_diag(ts_epoch, "ENGINE", "PREOPEN_BUDGET_SCALE",
                       f"surge_mode=20% per_slot={_per_slot:,.0f} cash={cash:,.0f} candidates={len(budget_syms)}")
        for sym, q, _ in budget_syms:
            self._preopen_budgets[sym] = _per_slot

        # ── whitelist만 watchlist.txt에 쓰기 (45개 → 5개 축소) ──
        preopen_watch = [s for s, _, _ in whitelist_syms]
        if preopen_watch:
            try:
                wl_path = self.watchlist_file
                with open(wl_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(preopen_watch) + "\n")
                with self._state_lock:
                    self.watch = set(preopen_watch)
                self._log_diag(ts_epoch, "ENGINE", "PREOPEN_WATCHLIST",
                               f"wrote {len(preopen_watch)} syms to {wl_path}")
            except Exception as _wle:
                self._log_diag(ts_epoch, "ENGINE", "PREOPEN_WATCHLIST_ERR",
                               f"{type(_wle).__name__}: {_wle}")

        # 로그
        wl_str = " | ".join(f"{s}({q:.0f})" for s, q, _ in whitelist_syms)
        budget_str = " | ".join(
            f"{s}={self._preopen_budgets.get(s,0):,.0f}(q={q:.0f})"
            for s, q, _ in budget_syms
        )
        self._log_diag(ts_epoch, "ENGINE", "PREOPEN_RANK",
                       f"whitelist{_whitelist_n}=[{wl_str}] budget{_budget_n} total={len(scored)} cash={cash:,.0f}")
        self._log_diag(ts_epoch, "ENGINE", "PREOPEN_BUDGET", budget_str)
        for s, q, detail in whitelist_syms:
            self._log_diag(ts_epoch, s, "PREOPEN_RANK_DETAIL",
                           f"quality={q:.1f} budget={self._preopen_budgets.get(s,0):,.0f} {detail}")
        if dropped:
            self._log_diag(ts_epoch, "ENGINE", "PREOPEN_DROP",
                           f"n={len(dropped)} pool={len(self._preopen_data)} | {' | '.join(dropped)}")
        self._preopen_whitelist_done = True  # ws_capture가 즉시 H0STCNT0으로 전환

    def on_orderbook(self, row: Dict[str, str], ts_epoch: float):
        sym = row.get("MKSC_SHRN_ISCD", "")
        if not sym:
            return
        self.book[sym] = row
        self.book_ts[sym] = ts_epoch
        self._update_book_ofi(sym, row)  # [PATCH] True Book OFI 업데이트

    def _write_ticker_snap(self, sym: str, price: float, score: float, metrics: Dict[str, float], ok: bool, ts_epoch: float, vol_extra=None):
        vol_extra = vol_extra or {}
        last = self._last_ticker_snap_ts.get(sym, 0.0)
        if (ts_epoch - last) < self.ticker_snap_interval:
            return
        self._last_ticker_snap_ts[sym] = ts_epoch
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts_epoch))
        line = (
            f"{ts_str},{sym},{price:.0f},{score:.1f},"
            f"{metrics.get('ret10',0.0):.3f},"
            f"{metrics.get('ret5',0.0):.3f},"
            f"{metrics.get('ofi',0.0):.2f},"
            f"{metrics.get('imb',0.0):.3f},"
            f"{metrics.get('spread_bps',0.0):.1f},"
            f"{metrics.get('dayrise',0.0):.2f},"
            f"{'1' if ok else '0'},"
            f"{int(metrics.get('no_book', 0))},"
            f"{vol_extra.get('cntg_vol',0):.0f},"
            f"{vol_extra.get('acml_vol',0):.0f},"
            f"{vol_extra.get('acml_tr',0):.0f},"
            f"{vol_extra.get('buy_cnt',0):.0f},"
            f"{vol_extra.get('sell_cnt',0):.0f},"
            f"{vol_extra.get('ntby_cnt',0):.0f},"
            f"{vol_extra.get('cttr',0):.1f},"
            f"{vol_extra.get('pvol_rate',0):.2f},"
            f"{vol_extra.get('open_px',0):.0f},"
            f"{vol_extra.get('high_px',0):.0f},"
            f"{vol_extra.get('low_px',0):.0f},"
            f"{metrics.get('liq_raw',0.0):.2f},"
            f"{metrics.get('ofi_raw',0.0):.2f},"
            f"{metrics.get('ba_ratio',0.0):.3f},"
            f"{metrics.get('ba_trend_30s',1.0):.3f}\n"
        )
        try:
            with open(self.ticker_snap_file, "a", encoding="utf-8") as f:
                f.write(line)
            self._ticker_snap_write_count += 1
            if self._ticker_snap_write_count % 200 == 0:
                _roll_file(self.ticker_snap_file, self.ticker_snap_maxlines, self.ticker_snap_maxlines // 2)
        except Exception:
            pass

    def on_trade(self, row: Dict[str, str], ts_epoch: float):
        sym = row.get("MKSC_SHRN_ISCD", "")
        price = _f(row.get("STCK_PRPR"))
        vol = _f(row.get("CNTG_VOL"))
        if (not sym) or price <= 0:
            return

        # 9:00 이전 / 진입마감 이후: 보유 포지션 청산만 — 스캔/로그/REST 전부 차단
        _hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        _entry_end = int(os.getenv("TRADING_END_HHMM", "1000"))
        if _hhmm < 900 or _hhmm >= _entry_end:
            with self._state_lock:
                _in_pos = sym in self.pos
            if _in_pos:
                self.manage_position(sym, price, ts_epoch)
            return

        acml_vol  = _f(row.get("ACML_VOL"))
        acml_tr   = _f(row.get("ACML_TR_PBMN"))
        cntg_vol  = _f(row.get("CNTG_VOL"))
        buy_cnt   = _f(row.get("SHNU_CNTG_CSNU"))
        sell_cnt  = _f(row.get("SELN_CNTG_CSNU"))
        ntby_cnt  = _f(row.get("NTBY_CNTG_CSNU"))
        cttr      = _f(row.get("CTTR"))
        pvol_rate = _f(row.get("PRDY_VOL_VRSS_ACML_VOL_RATE"))
        open_px   = _f(row.get("STCK_OPRC"))
        high_px   = _f(row.get("STCK_HGPR"))
        low_px    = _f(row.get("STCK_LWPR"))
        ccld_dvsn = row.get("CCLD_DVSN", "")  # 1=매수(+), 3=장전, 5=매도(-)

        # 전일종가 역산 → 상한가 거리 계산
        prdy_vrss_sign = row.get("PRDY_VRSS_SIGN", "3")  # 1=상한,2=상승,3=보합,4=하한,5=하락
        prdy_vrss      = _f(row.get("PRDY_VRSS"))         # 전일대비(항상 양수)
        if prdy_vrss_sign in ("2", "1"):
            prev_close = price - prdy_vrss
        elif prdy_vrss_sign in ("5", "4"):
            prev_close = price + prdy_vrss
        else:
            prev_close = price
        if prev_close > 0 and price > 0:
            upper_limit   = prev_close * 1.30
            mxpr_dist_pct = ((upper_limit - price) / price) * 100.0
        else:
            mxpr_dist_pct = 0.0

        # VI 해제 감지 — VI_STND_PRC는 H0STCNT0(체결) 스키마에만 존재
        _vi_raw = row.get("VI_STND_PRC", "")
        vi_stnd_prc = _f(_vi_raw)  # _vi_raw 재사용, row.get 중복 호출 방지
        if _vi_raw != "" and _vi_raw is not None:
            vi_std_new = _f(_vi_raw)
            vi_std_prev = self._vi_prev_std.get(sym, -1.0)
            if vi_std_prev > 0 and vi_std_new == 0.0:
                self._vi_clear_ts[sym] = ts_epoch
            self._vi_prev_std[sym] = vi_std_new

        self._reload_watchlist(ts_epoch)

        with self._state_lock:
            in_watch = sym in self.watch
            in_pos = sym in self.pos
        if not in_watch and not in_pos:
            return

        dq = self.ticks[sym]
        dq.append((ts_epoch, price, max(0.0, cntg_vol),
                   acml_vol, acml_tr, buy_cnt, sell_cnt, ntby_cnt, cttr))
        while dq and (ts_epoch - dq[0][0]) > 360.0:
            dq.popleft()
        self._update_vwap(sym, price, max(0.0, cntg_vol), ts_epoch)  # [PATCH] VWAP 업데이트

        if in_pos:
            if upper_limit > 0:
                self.pos[sym].upper_limit = upper_limit
            self.manage_position(sym, price, ts_epoch)
            return
        if self._fast_path.try_fast_entry(sym, price, row, ts_epoch):
            return
        score, reasons, metrics = self.score_symbol(sym, price, ts_epoch)
        self._watchlist_scores[sym] = score
        self._watchlist_score_ts[sym] = ts_epoch
        self._imb_history[sym].append((ts_epoch, metrics.get("imb", 0.5)))
        self._ba_history[sym].append((ts_epoch, metrics.get("ba_ratio", 1.0)))
        try:
            ok, why = self.should_buy(sym, score, metrics, ts_epoch)
        except Exception as e:
            self._log_diag(ts_epoch, sym, "GATE_ERR", f"{type(e).__name__}: {e}")
            return  # 예외 시 진입 차단
        finally:
            # 2틱 게이트용 prev 업데이트 — should_buy 결과와 무관하게 매 틱 갱신
            self._prev_ofi[sym] = metrics.get("ofi", 0.0)
            self._prev_burst[sym] = metrics.get("burst_ratio", 1.0)
            self._prev_imb[sym] = metrics.get("imb", 0.5)

        last = self._last_candidate_log_ts.get(sym, 0.0)
        if (ts_epoch - last) >= 1.0:
            self._score_eval_total += 1
            if ok:
                self._score_pass_total += 1
            self._last_candidate_log_ts[sym] = ts_epoch
            vol_extra = dict(
                cntg_vol=cntg_vol, acml_vol=acml_vol, acml_tr=acml_tr,
                buy_cnt=buy_cnt, sell_cnt=sell_cnt, ntby_cnt=ntby_cnt,
                cttr=cttr, pvol_rate=pvol_rate,
                open_px=open_px, high_px=high_px, low_px=low_px,
            )
            self._write_ticker_snap(sym, price, score, metrics, ok, ts_epoch, vol_extra)
            status = "PASS" if ok else "DROP"
            pos_s, neg_s = self._top_factor_strings(metrics)
            self._log_diag(
                ts_epoch,
                sym,
                "CAND",
                f"score={score:.1f} pass={1 if ok else 0} why={why} ofi_spike_exempt={int(metrics.get('_ofi_spike_exempt',0))} gate=trv10={metrics.get('trv10',0.0):.0f}/{self.buy_trv10_min:.0f},ret10={metrics.get('ret10',0.0):.2f}/{self.buy_ret10_min:.2f},ofi={metrics.get('ofi',0.0):.2f}/{self.buy_ofi_min:.2f},imb={metrics.get('imb',0.0):.2f}/{self.buy_imb_min:.2f},spread={metrics.get('spread_bps',0.0):.2f}/{self.buy_spread_max_bps:.2f} pos={pos_s} neg={neg_s} metrics=ret5={metrics.get('ret5',0.0):.2f},depth={metrics.get('depth_ratio',0.0):.2f},spread={metrics.get('spread_bps',0.0):.2f},dayrise={metrics.get('dayrise',0.0):.2f},recent_high={metrics.get('recent_high',0.0):.0f},pull_rebound={metrics.get('pull_rebound',0.0):.0f}",
            )

        if not ok:
            self._gate_block_counts[why] += 1
            self._record_event(ts_epoch, "DROP", sym, why)
            # 후보 자격 잃은 종목은 candidates에서 제거
            with self._state_lock:
                self._buy_candidates.pop(sym, None)
            return

        # REFACTOR: 즉시 진입 대신 후보 등록 → on_timer에서 최고점 1개 선택
        with self._state_lock:
            self._buy_candidates[sym] = {
                "score": score, "price": price,
                "reasons": reasons, "metrics": metrics, "ts": ts_epoch,
            }

    def _select_and_enter(self, ts_epoch: float):
        """REFACTOR: on_timer에서 호출 — 후보 중 신선하고 점수 가장 높은 1개 진입."""
        with self._state_lock:
            if not self._buy_candidates:
                return
            if len(self.pos) >= self.max_positions:
                self._buy_candidates.clear()  # max 도달 시 stale 후보 정리
                return
            candidates_snapshot = dict(self._buy_candidates)
        _fresh_sec = float(os.getenv("CANDIDATE_FRESH_SEC", "2.0"))
        # 신선도 필터: 마지막 틱이 _fresh_sec 이내 + halt/cooldown/pos 재확인 (FIX-BUG9: REFACTOR로 생긴 stale 후보 경로)
        fresh = {
            sym: c for sym, c in candidates_snapshot.items()
            if (ts_epoch - c["ts"]) <= _fresh_sec
            and sym not in self.pos
            and not self._trading_halted
            and ts_epoch >= self.cooldown_until.get(sym, 0.0)
        }
        # 만료된 후보 정리
        stale = [sym for sym in candidates_snapshot if sym not in fresh]
        if stale:
            with self._state_lock:
                for sym in stale:
                    self._buy_candidates.pop(sym, None)
        if not fresh:
            return
        # 최고점 선택
        best_sym = max(fresh, key=lambda s: fresh[s]["score"])
        best = fresh[best_sym]
        self._log_diag(
            ts_epoch, best_sym, "SELECT",
            f"score={best['score']:.1f} candidates={len(fresh)} "
            f"all={sorted(fresh.keys(), key=lambda s: fresh[s]['score'], reverse=True)}"
        )
        # 진입 후 전체 후보 초기화 (중복 진입 방지)
        with self._state_lock:
            self._buy_candidates.clear()
        self.enter_position(best_sym, best["price"], best["score"], best["reasons"], best["metrics"], ts_epoch)

    def _record_event(self, ts_epoch: float, event: str, sym: str, detail: str = ""):
        self._recent_events.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch)),
            "event": event,
            "symbol": sym,
            "detail": (detail or "")[:120],
        })

    def _operator_summary_runtime(self, watch_n: int, score_pass_rate: float) -> str:
        hard_block = sum(v for k, v in self._gate_block_counts.items() if (not k.startswith("watch")) and k not in {"pass", "score_too_low", "chase_penalty_dominated"})
        watch_ok = watch_n >= max(3, self.max_positions * 2)
        if watch_n < max(3, self.max_positions):
            return "watch_small and scanner_overfiltered"
        if watch_ok and score_pass_rate < 0.10:
            return "watch_ok but score_blocked"
        if score_pass_rate >= 0.10 and hard_block > self._score_pass_total:
            return "scores_ok but hard_gates_blocking"
        if self._last_buy_time > 0 and (time.time() - self._last_buy_time) < 900:
            return "buying_normally"
        return "watch_ok monitoring"

    def _write_runtime_status(self, ts_epoch: float):
        top_gate = sorted(self._gate_block_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        score_pass_rate = (self._score_pass_total / max(1, self._score_eval_total))
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch)),
            "watch_count": len(self.watch),
            "position_count": len(self.pos),
            "orderable_cash": self._last_orderable_cash,
            "orderable_cash_text": _fmt_won(self._last_orderable_cash),
            "top_gate_blockers": [{"reason": k, "count": int(v)} for k, v in top_gate],
            "score_pass_rate": round(score_pass_rate, 4),
            "last_buy_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_buy_time)) if self._last_buy_time > 0 else "",
            "last_sell_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_sell_time)) if self._last_sell_time > 0 else "",
            "last_buy_symbol": self._last_buy_symbol,
            "last_sell_symbol": self._last_sell_symbol,
            "recent_events": list(self._recent_events),
            "operator_summary": self._operator_summary_runtime(len(self.watch), score_pass_rate),
            "trading_halted": self._trading_halted,
            "daily_realized_pnl": round(self._daily_realized_pnl, 0),
            # BUG-1: ws_capture가 H0STASP0 구독 우선순위 결정에 사용
            "top_scored_symbols": sorted(
                [s for s in self._watchlist_scores if s not in self.pos],
                key=lambda s: self._watchlist_scores[s], reverse=True
            )[:20],
            "positions": [
                {
                    "sym": sym,
                    "entry_price": p.entry_price,
                    "last_price": p.last_price if p.last_price > 0 else p.entry_price,
                    "pnl_pct": round(((p.last_price if p.last_price > 0 else p.entry_price) / p.entry_price - 1) * 100, 2),
                    "hold_sec": int(ts_epoch - p.entry_ts),
                    "qty": p.qty,
                    "score": round(p.score, 1),
                    "max_pnl_pct": round(p.max_pnl_pct, 2),
                }
                for sym, p in self.pos.items()
            ],
        }
        try:
            d = os.path.dirname(self.runtime_status_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.runtime_status_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _send_health(self, ts_epoch: float):
        if self.health_check_sec <= 0:
            return
        if (ts_epoch - self._last_health_ts) < self.health_check_sec:
            return
        self._last_health_ts = ts_epoch

        # 상태는 갱신하되, 장외에는 외부 health 알림을 보내지 않음
        self._refresh_orderable_cash(ts_epoch, use_fallback=True)
        if self._is_afterhours_window(ts_epoch):
            return

        orderable_s = _fmt_won(self._last_orderable_cash)
        lines = [
            f"감시 {len(self.watch)} | 보유 {len(self.pos)}",
            f"주문가능 {orderable_s}",
        ]
        self.notifier.send(title="🩺 Simple Engine Health", color=0x5865F2, lines=lines)

    def _reconcile_ledger_with_fills(self, ts_epoch: float):
        """장 종료 후 KIS 체결내역 API로 ledger 실체결가 보정."""
        try:
            from kis_orders import inquire_daily_ccld
            fills = inquire_daily_ccld()
        except Exception as e:
            self._log_diag(ts_epoch, "ENGINE", "RECONCILE_FAIL", f"{type(e).__name__}: {e}")
            return
        if not fills:
            self._log_diag(ts_epoch, "ENGINE", "RECONCILE_SKIP", "no fills from API")
            return

        # KIS 체결 데이터를 종목+매수매도별로 집계
        # sll_buy_dvsn_cd: 01=매도, 02=매수
        kis_fills = {}  # (sym, side) -> [(avg_price, qty), ...]
        for row in fills:
            sym = str(row.get("pdno", row.get("PDNO", ""))).strip()
            side_cd = str(row.get("sll_buy_dvsn_cd", row.get("SLL_BUY_DVSN_CD", "")))
            side = "BUY" if side_cd == "02" else "SELL"
            qty_s = str(row.get("tot_ccld_qty", row.get("TOT_CCLD_QTY", "0"))).strip()
            avg_s = str(row.get("avg_prvs", row.get("AVG_PRVS",
                   row.get("ccld_unpr", row.get("CCLD_UNPR", "0"))))).strip()
            try:
                qty = int(float(qty_s))
                avg = float(avg_s)
            except (ValueError, TypeError):
                continue
            if qty <= 0 or avg <= 0 or not sym:
                continue
            kis_fills.setdefault((sym, side), []).append((avg, qty))

        if not kis_fills:
            self._log_diag(ts_epoch, "ENGINE", "RECONCILE_SKIP", "no valid fills parsed")
            return

        # ledger 읽기 — 당일 행만 보정
        today = time.strftime("%Y%m%d", time.localtime(ts_epoch))
        import csv
        rows = []
        changed = 0
        try:
            with open(self.ledger_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    rows.append(row)
        except Exception as e:
            self._log_diag(ts_epoch, "ENGINE", "RECONCILE_FAIL", f"ledger read: {e}")
            return

        # 각 행에 대해 당일 + 매칭되는 KIS 체결가로 교정
        for i, row in enumerate(rows):
            if len(row) < 5:
                continue
            try:
                ts_val = float(row[0])
                row_date = time.strftime("%Y%m%d", time.localtime(ts_val))
            except (ValueError, TypeError):
                continue
            if row_date != today:
                continue
            action = row[1].strip().upper()
            sym = row[2].strip()
            if action not in ("BUY", "SELL", "PARTIAL_SELL"):
                continue
            side_key = "BUY" if action == "BUY" else "SELL"
            fill_list = kis_fills.get((sym, side_key))
            if not fill_list:
                continue
            # 매칭: qty가 같은 체결 찾기 (여러 건이면 첫 번째 매칭 사용 후 제거)
            try:
                ledger_qty = int(float(row[3]))
            except (ValueError, TypeError):
                continue
            matched_idx = None
            for fi, (avg, qty) in enumerate(fill_list):
                if qty == ledger_qty:
                    matched_idx = fi
                    break
            if matched_idx is None:
                # qty 정확 매칭 없으면 첫 번째 사용
                matched_idx = 0
            avg_price, _ = fill_list.pop(matched_idx)
            old_price = row[4]
            row[4] = f"{avg_price:.4f}"
            if old_price != row[4]:
                changed += 1
                self._log_diag(ts_epoch, sym, "RECONCILE",
                               f"{action} qty={ledger_qty} price {old_price} → {avg_price:.0f}")

        if changed > 0:
            try:
                with open(self.ledger_file, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerows(rows)
                self._log_diag(ts_epoch, "ENGINE", "RECONCILE_DONE", f"{changed} rows updated")
            except Exception as e:
                self._log_diag(ts_epoch, "ENGINE", "RECONCILE_FAIL", f"ledger write: {e}")
        else:
            self._log_diag(ts_epoch, "ENGINE", "RECONCILE_DONE", "no price changes needed")

    def _reset_daily_state(self, ts_epoch: float):
        # BUG-018: WS tick 타임스탬프 대신 wall clock 사용 (tick ts는 과거일 수 있음)
        today = time.strftime("%Y%m%d", time.localtime(time.time()))
        last = self._last_trading_day
        if last == today:
            return
        self._last_trading_day = today
        if self._trading_halted or self._daily_realized_pnl != 0.0:
            self._log_diag(
                ts_epoch, "ENGINE", "DAILY_RESET",
                f"prev_day={last} pnl={self._daily_realized_pnl:.0f} halted={self._trading_halted}"
            )
        # NEW-002: 날짜가 바뀔 때만 halt/pnl 리셋 (재시작 시 같은 날이면 유지)
        self._trading_halted = False
        self._reconcile_done = False
        self._daily_realized_pnl = 0.0
        self._daily_loss_base_cash = None
        self._loss_streak.clear()
        self._loss_streak_blocked.clear()
        self._pending_fill.clear()
        with self._state_lock:
            self._preopen_data.clear()
        self._preopen_history.clear()
        self._preopen_whitelist.clear()
        self._preopen_budgets.clear()
        self._preopen_whitelist_done = False
        for p in self.pos.values():
            p.split_a_sold = False  # 날짜 넘어가면 split 상태 초기화
        self._fast_path.reset_session()  # BUG-FP1: fast-path 세션 카운터 일 단위 초기화
        self._save_state()  # 리셋된 상태 즉시 저장

    def _confirm_unconfirmed_fills(self, ts_epoch: float):
        """BUY_FILL_ASSUMED 포지션의 실제 체결가를 KIS inquire_holdings로 교정 (비동기)."""
        unconfirmed = [s for s, p in self.pos.items() if not p.fill_confirmed and not p.manual]
        if not unconfirmed:
            return
        if self._fill_confirm_pending:
            return
        if ts_epoch - self._fill_confirm_ts < 5.0:  # 매수 후 5초 여유 후 조회
            return
        self._fill_confirm_pending = True
        self._fill_confirm_ts = ts_epoch

        def _do_confirm(syms, ts):
            try:
                holdings = inquire_holdings()
                kis_map = {}
                for row in holdings:
                    pdno = str(row.get("pdno") or row.get("PDNO") or "").strip()
                    avg_k = float(_f(row.get("pchs_avg_pric") or row.get("PCHS_AVG_PRIC"), 0.0))
                    if pdno and avg_k > 0:
                        kis_map[pdno] = avg_k
                with self._state_lock:
                    for sym in syms:
                        p = self.pos.get(sym)
                        if p is None or p.fill_confirmed:
                            continue
                        avg_k = kis_map.get(sym, 0.0)
                        if avg_k > 0:
                            old = p.entry_price
                            p.entry_price = avg_k
                            if p.max_price == old:  # peak 추적도 보정
                                p.max_price = avg_k
                            p.fill_confirmed = True
                            self._log_diag(ts, sym, "FILL_CONFIRMED",
                                           f"entry_price {old:.0f} -> {avg_k:.0f} (diff={avg_k-old:+.0f}원)")
                self._save_state()
            except Exception as e:
                self._log_diag(ts, "ENGINE", "FILL_CONFIRM_FAIL", f"{type(e).__name__}: {e}")
            finally:
                self._fill_confirm_pending = False

        threading.Thread(target=_do_confirm, args=(unconfirmed, ts_epoch), daemon=True).start()

    def _validate_state_once(self, ts_epoch: float):
        """PATCH 4: KIS 실계좌 재검증 — 실패 시 10초마다 재시도, 성공 시 _state_validated=True."""
        if self._state_validated:
            return
        _wait = ts_epoch - self._init_ts
        if _wait < 10.0:
            return
        # 재시도: 10초마다 한 번만 (무한 루프 방지)
        if not hasattr(self, '_last_validate_attempt'):
            self._last_validate_attempt = 0.0
        if ts_epoch - self._last_validate_attempt < 10.0:
            return
        self._last_validate_attempt = ts_epoch

        try:
            holdings = inquire_holdings()
            kis_map: Dict[str, tuple] = {}
            for row in holdings:
                pdno = str(row.get("pdno") or row.get("PDNO") or "").strip()
                qty_k = int(_f(row.get("hldg_qty") or row.get("HLDG_QTY"), 0))
                avg_k = float(_f(row.get("pchs_avg_pric") or row.get("PCHS_AVG_PRIC"), 0.0))
                if pdno and qty_k > 0:
                    kis_map[pdno] = (qty_k, avg_k)

            # 실계좌에 없는 포지션 제거
            for sym in list(self.pos.keys()):
                if sym not in kis_map:
                    self._log_diag(ts_epoch, sym, "VALIDATE_EVICT",
                                   f"KIS qty=0 -> ghost pos cleared")
                    self.pos.pop(sym, None)

            # 실계좌에 있는데 self.pos에 없는 것 추가
            for pdno, (qty_k, avg_k) in kis_map.items():
                if pdno not in self.pos:
                    self.pos[pdno] = Position(
                        qty=qty_k, entry_price=avg_k if avg_k > 0 else 1.0,
                        entry_ts=ts_epoch, max_price=avg_k if avg_k > 0 else 1.0,
                        fill_confirmed=True,
                        manual=True,  # FIX-BUG4: SYNC_ADD와 동일하게 manual=True — 손절/max_hold 적용 제외
                    )
                    self._log_diag(ts_epoch, pdno, "VALIDATE_ADD",
                                   f"KIS qty={qty_k} avg={avg_k:.0f} manual=True")

            self._state_validated = True
            self._save_state()
            self._log_diag(ts_epoch, "ENGINE", "VALIDATE_OK",
                           f"KIS sync done: holdings={list(kis_map.keys())} pos={list(self.pos.keys())}")

        except Exception as e:
            self._log_diag(ts_epoch, "ENGINE", "VALIDATE_RETRY",
                           f"inquire_holdings failed, will retry: {type(e).__name__}: {e}")
            # _state_validated는 False 유지 → 다음 on_timer에서 재시도

    def on_timer(self, ts_epoch: float):
        self._reset_daily_state(ts_epoch)

        # 8:55 이전은 일별 리셋만 — REST/로그/스캔 전부 차단
        _hhmm_now = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        _entry_end = int(os.getenv("TRADING_END_HHMM", "1000"))
        if _hhmm_now < 855:
            return

        # ── 동시호가 whitelist 확정 (on_timer에서 시간 기반 — WS 상태 무관) ──
        if not self._preopen_whitelist_done:
            _hmss_now = int(time.strftime("%H%M%S", time.localtime(ts_epoch)))
            if _hmss_now >= 85920 and self._preopen_data:
                self._rebuild_preopen_whitelist(ts_epoch)
        # 신규 진입 마감 이후: 포지션 관리만 + 장마감 강제 청산
        if _hhmm_now >= _entry_end:
            _force_exit_hhmm = int(os.getenv("FORCE_EXIT_HHMM", "9999"))  # 기본 비활성 (cmd에서 설정 시만 작동)
            with self._state_lock:
                pos_items = list(self.pos.items())
            if _hhmm_now >= _force_exit_hhmm and pos_items:
                for sym, p in pos_items:
                    if sym in self.pos:
                        last_px = p.last_price if p.last_price > 0 else p.entry_price
                        self._log_diag(ts_epoch, sym, "FORCE_EXIT",
                                       f"hhmm={_hhmm_now} price={last_px:.0f} pnl={(last_px/p.entry_price-1)*100:.2f}%")
                        self.manage_position(sym, last_px, ts_epoch, force_reason="force_exit_eod")
            else:
                for sym, p in pos_items:
                    last_px = p.last_price if p.last_price > 0 else p.entry_price
                    if last_px > 0:
                        self.manage_position(sym, last_px, ts_epoch)
            # 포지션 전부 청산 후 → 실체결가 보정 (1회)
            if _hhmm_now >= _force_exit_hhmm and not self.pos:
                if not self._reconcile_done:
                    self._reconcile_done = True
                    self._reconcile_ledger_with_fills(ts_epoch)
            if (ts_epoch - self._last_runtime_snapshot_ts) >= 1.0:
                self._last_runtime_snapshot_ts = ts_epoch
                self._write_runtime_status(ts_epoch)
            return

        self._fast_path.log_ranking_snapshot(ts_epoch)
        self._validate_state_once(ts_epoch)       # NEW-001
        self._confirm_unconfirmed_fills(ts_epoch)  # BUY_FILL_ASSUMED 체결가 교정
        self._reload_watchlist(ts_epoch)
        self._refresh_orderable_cash(ts_epoch, use_fallback=True)
        if self._daily_loss_base_cash is None and self._last_orderable_cash is not None:
            self._daily_loss_base_cash = self._last_orderable_cash
            self._log_diag(
                ts_epoch, "ENGINE", "DAILY_BASE",
                f"base_cash={self._daily_loss_base_cash:.0f}"
            )
        self._update_market_state(ts_epoch)
        with self._state_lock:
            pos_items = list(self.pos.items())
        _stale_price_sec = float(os.getenv("STALE_PRICE_SEC", "30"))
        for sym, p in pos_items:
            last_px = p.last_price if p.last_price > 0 else p.entry_price
            if last_px > 0:
                if p.last_price_ts > 0 and (ts_epoch - p.last_price_ts) > _stale_price_sec:
                    continue  # BUG-014: 틱 끊긴 종목은 타이머 기반 청산 스킵
                self.manage_position(sym, last_px, ts_epoch)
        # REFACTOR: 후보 중 최고점 1개 선택해서 진입
        if os.getenv("TIMER_ENTRY_ENABLED", "1") == "1":
            self._select_and_enter(ts_epoch)
        self._send_health(ts_epoch)
        if (ts_epoch - self._last_runtime_snapshot_ts) >= 1.0:
            self._last_runtime_snapshot_ts = ts_epoch
            self._write_runtime_status(ts_epoch)
