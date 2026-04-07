# src/morning_fastpath.py
"""
즉시매수 fast-path 진입 모듈 (v4).

전략:
  - 테마 ∩ 랭크(거래량) 교집합 종목을 9시 장 시작과 동시에 즉시 매수
  - 종목선정이 곧 필터 — 진입 시 별도 시그널(surge/dip) 없음
  - 매수 단위: 균등 20% (총자본의 1/5)
  - 종목당 1회 매수, 전체 최대 5회 매수
  - 일일 손실 한도: 3.5% 초과 시 신규 매수 차단

환경변수:
  FAST_PATH_ENABLED       : 0/1 (기본 1)
  FAST_PATH_END_HHMM      : 종료시각 HHMM (기본 1000)
  FAST_PATH_COOLDOWN_SEC  : 종목별 fast-path 쿨다운 (기본 30)
  FAST_PATH_MAX_ENTRIES   : 세션 내 fast-path 최대 진입 횟수 (기본 5)
  FAST_PATH_MIN_TICKS     : 최소 수신 틱 수 (기본 3) — warm-up
  SURGE_MAX_ENTRIES_PER_SYM : 종목당 최대 매수 횟수 (기본 1)

버전 이력:
  v1: 9시 즉시매수 (commit 6da4b73 이전)
  v2: surge-trigger 10초/2% (commit bc6db73)
  v3: dip-accumulation 눌림목+매수잔량 (실패, 트리거 예측력 0)
  v4: 테마∩랭크 교집합 즉시매수 (현재)
"""
from __future__ import annotations

import os
import time
from collections import deque
from typing import TYPE_CHECKING, Dict, Any, Deque, Tuple

if TYPE_CHECKING:
    from engine_simple import EngineSimple


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d


class MorningFastPath:
    """
    Dip-accumulation fast-path: 눌림목 + 매수잔량 축적 감지 → 즉시 매수.

    20종목 후보 관찰 → dip+bid 감지 순서대로 20%씩 진입 → 최대 5회.
    """

    def __init__(self, engine: "EngineSimple", rvol_loader=None):
        self._engine = engine
        self._rvol = rvol_loader  # RvolLoader | None
        self._session_entries: int = 0
        self._sym_entries: Dict[str, int] = {}  # sym → 해당 종목 매수 횟수
        self._fast_cooldown: Dict[str, float] = {}  # sym → 쿨다운 만료 ts
        self._last_ranking_ts: float = 0.0
        self._fp_debug_date: str = ""
        self._fp_debug_path: str = ""
        # 가격 링버퍼: sym → deque of (epoch, price)
        self._price_buf: Dict[str, Deque[Tuple[float, float]]] = {}
        # 호가 링버퍼: sym → deque of (epoch, bid_1to3, total_bid)
        self._bid_buf: Dict[str, Deque[Tuple[float, float, float]]] = {}

    # ── 설정 ──────────────────────────────────────────────────────────

    @staticmethod
    def enabled() -> bool:
        return os.getenv("FAST_PATH_ENABLED", "1") == "1"

    @staticmethod
    def end_hhmm() -> int:
        return _env_int("FAST_PATH_END_HHMM", 1520)

    @staticmethod
    def cooldown_sec() -> float:
        return _env_float("FAST_PATH_COOLDOWN_SEC", 30.0)

    @staticmethod
    def max_entries() -> int:
        return _env_int("FAST_PATH_MAX_ENTRIES", 5)

    @staticmethod
    def min_ticks() -> int:
        return _env_int("FAST_PATH_MIN_TICKS", 3)

    @staticmethod
    def surge_window_sec() -> float:
        return _env_float("SURGE_WINDOW_SEC", 10.0)

    @staticmethod
    def surge_threshold_pct() -> float:
        return _env_float("SURGE_THRESHOLD_PCT", 2.0)

    @staticmethod
    def max_entries_per_sym() -> int:
        return _env_int("SURGE_MAX_ENTRIES_PER_SYM", 2)

    # ── 버퍼 업데이트 ────────────────────────────────────────────────

    def _update_price_buf(self, sym: str, price: float, ts_epoch: float):
        """가격 버퍼 업데이트."""
        if sym not in self._price_buf:
            self._price_buf[sym] = deque(maxlen=500)
        buf = self._price_buf[sym]
        buf.append((ts_epoch, price))
        cutoff = ts_epoch - self.surge_window_sec() - 25  # 여유
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _update_bid_buf(self, sym: str, ts_epoch: float):
        """eng.book[sym]에서 호가 데이터를 읽어 bid 버퍼에 저장."""
        ob = self._engine.book.get(sym)
        if not ob:
            return
        bid_1to3 = (
            _f(ob.get("BIDP_RSQN1"))
            + _f(ob.get("BIDP_RSQN2"))
            + _f(ob.get("BIDP_RSQN3"))
        )
        total_bid = _f(ob.get("TOTAL_BIDP_RSQN"))
        if total_bid <= 0:
            return
        if sym not in self._bid_buf:
            self._bid_buf[sym] = deque(maxlen=500)
        buf = self._bid_buf[sym]
        # 같은 호가 반복 저장 방지 (book이 안 바뀌었으면 스킵)
        if buf and buf[-1][1] == bid_1to3 and buf[-1][2] == total_bid:
            return
        buf.append((ts_epoch, bid_1to3, total_bid))
        cutoff = ts_epoch - self.surge_window_sec() - 25
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    # ── 감지 메서드 ──────────────────────────────────────────────────

    def _price_change_pct(self, sym: str, ts_epoch: float) -> float:
        """윈도우 기간 가격 변화율(%)."""
        buf = self._price_buf.get(sym)
        if not buf or len(buf) < 2:
            return 0.0
        now_price = buf[-1][1]
        if now_price <= 0:
            return 0.0
        target_ts = ts_epoch - self.surge_window_sec()
        base_price = None
        for ep, pr in buf:
            if ep <= target_ts:
                base_price = pr
            else:
                break
        if not base_price or base_price <= 0:
            return 0.0
        return (now_price - base_price) / base_price * 100.0

    def _bid_growth_pct(self, sym: str, ts_epoch: float) -> Tuple[float, float]:
        """윈도우 기간 bid 변화율(%). Returns: (bid_near_growth%, total_bid_growth%)."""
        buf = self._bid_buf.get(sym)
        if not buf or len(buf) < 2:
            return 0.0, 0.0
        now_near = buf[-1][1]
        now_total = buf[-1][2]
        target_ts = ts_epoch - self.surge_window_sec()
        old_near = None
        old_total = None
        for ep, bn, tb in buf:
            if ep <= target_ts:
                old_near = bn
                old_total = tb
            else:
                break
        if old_total is None or old_total <= 0:
            return 0.0, 0.0
        near_g = (now_near - old_near) / max(1.0, old_near) * 100.0 if old_near else 0.0
        total_g = (now_total - old_total) / old_total * 100.0
        return near_g, total_g

    def _detect_dip_accumulation(self, sym: str, price: float, ts_epoch: float) -> Tuple[bool, str]:
        """
        눌림목 + 매수잔량 축적 감지.

        Returns: (triggered, detail_string)

        Combo A: 큰 눌림 (deep dip) — 가격 하락만으로 트리거
        Combo B: 작은 눌림 + 매수잔량 증가 — 복합 트리거
        """
        price_chg = self._price_change_pct(sym, ts_epoch)
        near_g, total_g = self._bid_growth_pct(sym, ts_epoch)

        deep_thr = _env_float("DIP_DEEP_PCT", -0.30)
        mod_thr = _env_float("DIP_MOD_PCT", -0.10)
        bid_min = _env_float("DIP_BID_GROWTH_MIN", 8.0)

        detail = f"pchg={price_chg:+.2f}% tbid_g={total_g:+.1f}% bnear_g={near_g:+.1f}%"

        # Combo A: deep dip (가격 하락 단독)
        if price_chg <= deep_thr:
            return True, f"deep_dip {detail}"

        # Combo B: moderate dip + bid growth (복합)
        if price_chg <= mod_thr and total_g >= bid_min:
            return True, f"dip+bid {detail}"

        return False, detail

    def _detect_surge(self, sym: str, price: float, ts_epoch: float) -> float:
        """기존 surge 감지 — 로깅용으로만 유지."""
        buf = self._price_buf.get(sym)
        if not buf:
            return 0.0
        target_ts = ts_epoch - self.surge_window_sec()
        base_price = None
        for ep, pr in buf:
            if ep <= target_ts:
                base_price = pr
            else:
                break
        if not base_price or base_price <= 0:
            return 0.0
        return (price - base_price) / base_price * 100.0


    # ── 핵심 메서드 ──────────────────────────────────────────────────────────

    def try_fast_entry(
        self,
        sym: str,
        price: float,
        row: Dict[str, str],
        ts_epoch: float,
    ) -> bool:
        """
        즉시매수 fast-path: whitelist 종목 첫 체결 시 바로 매수.

        Returns:
            True  → 매수 실행
            False → 조건 미충족 (whitelist 미포함, 한도 초과 등)
        """
        eng = self._engine

        # ── 1. 활성화 + 시간대 ───────────────────────────────────────────
        if not self.enabled():
            return False

        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if not (900 <= hhmm < self.end_hhmm()):
            return False

        # ── 항상 버퍼 업데이트 (감지 준비) ────────────────────────────────
        self._update_price_buf(sym, price, ts_epoch)
        self._update_bid_buf(sym, ts_epoch)

        # ── 2. 시스템 상태 ───────────────────────────────────────────────
        if eng._trading_halted:
            return False
        if os.path.exists(eng.manual_halt_file):
            return False
        if sym not in eng.watch:
            return False

        # ── 2b. 후보 풀 (whitelist = 20종목) ─────────────────────────────
        if not eng._preopen_whitelist:
            return False
        if sym not in eng._preopen_whitelist:
            return False

        # ── 3. 세션/종목 횟수 제한 ───────────────────────────────────────
        if self._session_entries >= self.max_entries():
            return False
        if self._sym_entries.get(sym, 0) >= self.max_entries_per_sym():
            return False

        # ── 4. 쿨다운 ───────────────────────────────────────────────────
        if ts_epoch < self._fast_cooldown.get(sym, 0.0):
            return False
        if ts_epoch < eng.cooldown_until.get(sym, 0.0):
            return False

        # ── 5. 손절 연속 차단 ────────────────────────────────────────────
        if eng._loss_streak.get(sym, 0) >= 2:
            return False
        if sym in eng._loss_streak_blocked:
            return False

        # ── 6. 틱 최소 수 (warm-up) ─────────────────────────────────────
        tick_count = len(eng.ticks.get(sym, []))
        if tick_count < self.min_ticks():
            return False

        # ── 7. 일별 손실 한도 ────────────────────────────────────────────
        if eng._daily_loss_base_cash is not None and eng._daily_loss_base_cash > 0:
            loss_pct = (-eng._daily_realized_pnl / eng._daily_loss_base_cash) * 100.0
            if loss_pct >= eng.daily_loss_limit_pct:
                eng._log_diag(ts_epoch, sym, "FAST_PATH_SKIP",
                              f"daily_loss_limit loss={loss_pct:.2f}%>={eng.daily_loss_limit_pct:.1f}%")
                return False

        # ── 8. 체결 미확인 재진입 차단 ───────────────────────────────────
        if sym in eng._pending_fill:
            if ts_epoch - eng._pending_fill[sym] < eng.pending_fill_block_sec:
                return False
            del eng._pending_fill[sym]

        # ── 9. 전일종가 + dayrise ────────────────────────────────────────
        prdy_vrss_sign = row.get("PRDY_VRSS_SIGN", "3")
        prdy_vrss = _f(row.get("PRDY_VRSS"))
        if prdy_vrss_sign in ("2", "1"):
            prev_close_tick = price - prdy_vrss
        elif prdy_vrss_sign in ("5", "4"):
            prev_close_tick = price + prdy_vrss
        else:
            prev_close_tick = price
        prev_close = eng._prev_close(sym)
        if prev_close <= 0:
            prev_close = prev_close_tick if prev_close_tick > 0 else 0.0
        if prev_close <= 0:
            return False
        dayrise = (price / prev_close - 1.0) * 100.0

        pvol_rate = _f(row.get("PRDY_VOL_VRSS_ACML_VOL_RATE"))

        # ── 10. score + 즉시 진입 ────────────────────────────────────────
        _preopen = eng._preopen_data.get(sym, {})
        _ba_ratio = _preopen.get("ba_ratio", 0.0)
        _gap_slope = _preopen.get("gap_slope", 0.0)
        _ba_trend = _preopen.get("ba_trend_3min", 1.0)
        _expected_gap = _preopen.get("expected_gap_pct", 0.0)

        score_stub = 180.0
        if _ba_ratio >= 5.0:
            score_stub += 20.0

        reasons = [
            "fast_path",
            f"pvol={pvol_rate:.1f}x",
            f"dayrise={dayrise:.2f}%",
            f"sym_entries={self._sym_entries.get(sym, 0)+1}/{self.max_entries_per_sym()}",
            f"ba={_ba_ratio:.1f}",
        ]
        metrics_stub: Dict[str, float] = {
            "dayrise": dayrise,
            "pvol_rate": pvol_rate,
            "expected_gap_pct": _expected_gap,
            "actual_gap_pct": ((price / prev_close - 1.0) * 100.0) if prev_close > 0 else 0.0,
            "gap_discount": 0.0,
            "gap_slope": _gap_slope,
            "ba_ratio": _ba_ratio,
            "ba_trend_3min": _ba_trend,
            "trv10": 0.0, "ret10": 0.0, "ofi": 0.0, "imb": 0.5, "spread_bps": -1.0,
        }

        eng._log_diag(
            ts_epoch, sym, "FAST_PATH_ENTRY",
            f"price={price:.0f} dayrise={dayrise:.2f}% pvol={pvol_rate:.1f}x "
            f"ba={_ba_ratio:.2f} slope={_gap_slope:.2f} "
            f"score={score_stub:.0f} "
            f"session={self._session_entries}/{self.max_entries()} "
            f"sym_n={self._sym_entries.get(sym, 0)+1}/{self.max_entries_per_sym()}",
        )

        # 쿨다운 등록
        self._fast_cooldown[sym] = ts_epoch + self.cooldown_sec()

        pos_before = len(eng.pos)
        eng.enter_position(sym, price, score_stub, reasons, metrics_stub, ts_epoch)
        if len(eng.pos) > pos_before or sym in eng._pending_fill:
            self._session_entries += 1
            self._sym_entries[sym] = self._sym_entries.get(sym, 0) + 1
            return True
        else:
            return False

    def log_ranking_snapshot(self, ts_epoch: float):
        """주기적으로 fast_path 후보 랭킹 스냅샷을 찍는다."""
        if not self.enabled():
            return
        hhmm = int(time.strftime("%H%M", time.localtime(ts_epoch)))
        if not (859 <= hhmm < self.end_hhmm()):
            return
        today = time.strftime("%Y%m%d", time.localtime(ts_epoch))
        if today != self._fp_debug_date:
            self._fp_debug_date = today
            _fp_dir = os.path.join("data", "logs", today)
            os.makedirs(_fp_dir, exist_ok=True)
            self._fp_debug_path = os.path.join(_fp_dir, "fastpath_debug.log")

        interval = _env_float("FP_DEBUG_INTERVAL_SEC", 10.0)
        if (ts_epoch - self._last_ranking_ts) < interval:
            return
        self._last_ranking_ts = ts_epoch

        eng = self._engine
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts_epoch))

        # whitelist 정보
        wl_syms = list(eng._preopen_whitelist) if eng._preopen_whitelist else []

        rows = []
        for sym in eng.watch:
            dq = eng.ticks.get(sym)
            if not dq:
                continue
            last_tick = dq[-1]
            price = last_tick[1]
            if price <= 0:
                continue

            tick_count = len(dq)
            prev_close = eng._prev_close(sym)
            if prev_close <= 0:
                continue
            dayrise = (price / prev_close - 1.0) * 100.0

            # preopen data
            _preopen = eng._preopen_data.get(sym, {})
            _ba_ratio = _preopen.get("ba_ratio", 0.0)
            _gap_slope = _preopen.get("gap_slope", 0.0)

            # dip/bid 감지 현황
            pchg = self._price_change_pct(sym, ts_epoch)
            _, tbid_g = self._bid_growth_pct(sym, ts_epoch)

            score_stub = 180.0
            if _ba_ratio >= 5.0:
                score_stub += 20.0

            # 상태 판정
            status = "READY"
            in_wl = not eng._preopen_whitelist or sym in eng._preopen_whitelist
            if sym in eng.pos:
                status = "HOLDING"
            elif not in_wl:
                status = "NO_PRE"
            elif ts_epoch < self._fast_cooldown.get(sym, 0.0):
                status = "COOLDOWN"
            elif ts_epoch < eng.cooldown_until.get(sym, 0.0):
                status = "SL_COOL"
            elif sym in eng._loss_streak_blocked:
                status = "STREAK"
            elif tick_count < self.min_ticks():
                status = "FEW_TICK"

            _sym_n = self._sym_entries.get(sym, 0)

            rows.append((
                score_stub, sym, dayrise, _ba_ratio, _gap_slope,
                pchg, tbid_g, tick_count, status, price, _sym_n
            ))

        rows.sort(key=lambda x: -x[0])

        lines = [f"\n[{ts_str}] fast_path ranking  entries={self._session_entries}/{self.max_entries()}  "
                 f"whitelist={len(wl_syms)}"]
        lines.append(f"{'#':>2} {'sym':>8} {'score':>6} {'dayrise':>7} {'ba':>5} {'slope':>6} {'pchg':>6} {'tbid_g':>7} {'ticks':>5} {'n':>2} {'status':>8} {'price':>8}")
        lines.append("-" * 93)
        for i, r in enumerate(rows[:20]):
            sc, sym, dr, ba, sl, pc, tg, tc, st, px, sn = r
            lines.append(f"{i+1:>2} {sym:>8} {sc:>6.0f} {dr:>+6.2f}% {ba:>5.2f} {sl:>6.2f} {pc:>+5.2f}% {tg:>+6.1f}% {tc:>5} {sn:>2} {st:>8} {px:>8.0f}")

        try:
            with open(self._fp_debug_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def reset_session(self):
        """일 단위 리셋 — engine의 _reset_daily_state에서 호출."""
        self._session_entries = 0
        self._sym_entries.clear()
        self._fast_cooldown.clear()
        self._last_ranking_ts = 0.0
        self._price_buf.clear()
        self._bid_buf.clear()
