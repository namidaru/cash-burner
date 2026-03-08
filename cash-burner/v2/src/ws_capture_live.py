from __future__ import annotations

import os, json, time, threading, csv, queue, re
from collections import deque
from dataclasses import dataclass
import requests, websocket
from kis_http import request

APP_KEY = os.getenv("KOREA_INVEST_APP_KEY","")
APP_SECRET = os.getenv("KOREA_INVEST_APP_SECRET","")

WS_URL = os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:21000")
BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

def _dated_out_file() -> str:
    raw = os.getenv("OUT_FILE", os.path.join("data", "ws_dump.log"))
    ymd = time.strftime("%Y%m%d")
    if "{date}" in raw:
        return raw.replace("{date}", ymd)
    return raw


CONTROL_FILE = os.getenv("CONTROL_FILE", os.path.join("data", "ws_control.log"))
WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
LEDGER_FILE = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
PREOPEN_TRACK_MIN = int(os.getenv("PREOPEN_TRACK_MIN", "15"))
PREOPEN_START_HHMM = int(os.getenv("PREOPEN_START_HHMM", "900"))

RAW_TR_IDS = [t.strip() for t in os.getenv("TR_IDS", "H0STCNT0,H0STASP0").split(",") if t.strip()]
ALLOWED_TR_IDS = {"H0STCNT0", "H0STASP0", "H0NXCNT0"}
ALLOWED_TR_TYPES = {"1", "2"}
PRIORITY_TRADE_TR_IDS = ("H0STCNT0", "H0NXCNT0")
POLL_WATCH_SEC = float(os.getenv("WATCH_POLL_SEC", "2.0"))
MAX_SUB_BACKOFF_SEC = float(os.getenv("WS_SUB_BACKOFF_MAX_SEC", "60"))
BASE_SUB_BACKOFF_SEC = float(os.getenv("WS_SUB_BACKOFF_BASE_SEC", "2"))
MAX_ACTIVE_SUB_KEYS = int(os.getenv("WS_MAX_ACTIVE_SUB_KEYS", "50"))
WATCH_REMOVE_DELAY_SEC = float(os.getenv("WATCH_REMOVE_DELAY_SEC", "0"))
LEADER_RADAR_ENABLE = os.getenv("LEADER_RADAR_ENABLE", "0") == "1"
LEADER_RADAR_RET3S_PCT = float(os.getenv("LEADER_RADAR_RET3S_PCT", "1.0"))
LEADER_RADAR_MAX_SUBS = max(0, int(os.getenv("LEADER_RADAR_MAX_SUBS", "5")))
LEADER_RADAR_HOLD_SEC = float(os.getenv("LEADER_RADAR_HOLD_SEC", "120"))


def _normalized_tr_ids(raw_ids: list[str]) -> tuple[list[str], list[str]]:
    ordered: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for tr in raw_ids:
        if tr not in ALLOWED_TR_IDS:
            dropped.append(tr)
            continue
        if tr in seen:
            continue
        seen.add(tr)
        ordered.append(tr)
    return ordered, dropped


TR_IDS, DROPPED_TR_IDS = _normalized_tr_ids(RAW_TR_IDS)
if not TR_IDS:
    TR_IDS = ["H0STCNT0"]


def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _append(path: str, line: str):
    _ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_approval_key() -> str:
    url = f"{BASE_URL}/oauth2/Approval"
    headers = {"content-type": "application/json; charset=utf-8"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "secretkey": APP_SECRET}
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
    r.raise_for_status()
    return r.json()["approval_key"]


def build_msg(approval_key: str, tr_id: str, sym: str, tr_type: str) -> str:
    return json.dumps(
        {
            "header": {"approval_key": approval_key, "custtype": "P", "tr_type": tr_type, "content-type": "utf-8"},
            "body": {"input": {"tr_id": tr_id, "tr_key": sym}},
        }
    )


def read_watchlist() -> set[str]:
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            syms = [ln.strip() for ln in f if ln.strip()]
        return set(syms)
    except Exception:
        return set()


def _hhmm_now(ts_epoch: float | None = None) -> int:
    if ts_epoch is None:
        ts_epoch = time.time()
    return int(time.strftime("%H%M", time.localtime(ts_epoch)))


def _held_symbols_from_ledger() -> set[str]:
    qty_by_symbol: dict[str, int] = {}
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 4:
                    continue
                action = str(row[1]).strip().upper()
                symbol = str(row[2]).strip()
                if not symbol:
                    continue
                try:
                    qty = int(float(row[3]))
                except Exception:
                    continue
                if qty <= 0:
                    continue
                prev = qty_by_symbol.get(symbol, 0)
                if action == "BUY":
                    qty_by_symbol[symbol] = prev + qty
                elif action == "SELL":
                    qty_by_symbol[symbol] = max(0, prev - qty)
    except Exception:
        return set()
    return {sym for sym, qty in qty_by_symbol.items() if qty > 0}


@dataclass(frozen=True)
class OutgoingWSMessage:
    payload: str
    tr_id: str | None = None
    tr_key: str | None = None
    tr_type: str | None = None

    @property
    def is_subscription(self) -> bool:
        return bool(self.tr_id and self.tr_key and self.tr_type)


class WSCapture:
    def __init__(self):
        self.approval_key = None
        self.ws = None
        self.ws_thread = None
        self.ws_send_queue: queue.Queue[OutgoingWSMessage] = queue.Queue(maxsize=10000)
        self.ws_send_thread = None
        self.stop_evt = threading.Event()
        self.reconnect_evt = threading.Event()
        self.subscribed = set()
        self.subscribed_keys: set[tuple[str, str]] = set()
        self.pending_subscribe: dict[tuple[str, str], float] = {}
        self.sub_blocked_until: dict[tuple[str, str], float] = {}
        self.sub_blocked_retry_exp: dict[tuple[str, str], int] = {}
        self.last_sync_desired_symbols: set[str] = set()
        self.last_sync_desired_keys: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.preopen_day = ""
        self.preopen_snapshot: set[str] = set()
        self.last_held_symbols: set[str] = set()
        self.watch_remove_after: dict[str, float] = {}
        self.leader_radar_active_until: dict[str, float] = {}
        self.leader_radar_price_hist: dict[str, deque] = {}

    def _in_preopen_window(self, ts_epoch: float | None = None) -> bool:
        hhmm = _hhmm_now(ts_epoch)
        start_h = PREOPEN_START_HHMM // 100
        start_m = PREOPEN_START_HHMM % 100
        start_min = start_h * 60 + start_m
        now_min = (hhmm // 100) * 60 + (hhmm % 100)
        return start_min <= now_min < (start_min + PREOPEN_TRACK_MIN)

    def _desired_symbols(self) -> set[str]:
        now = time.time()
        today = time.strftime("%Y%m%d")
        current_watchlist = read_watchlist()

        if self.preopen_day != today:
            self.preopen_day = today
            self.preopen_snapshot = set()

        if self._in_preopen_window():
            if not self.preopen_snapshot and current_watchlist:
                self.preopen_snapshot = set(current_watchlist)
                _append(CONTROL_FILE, f"{_ts()}	PREOPEN snapshot n={len(self.preopen_snapshot)}")
            base = self.preopen_snapshot or current_watchlist
        else:
            base = current_watchlist

        watch_now = set(base)
        kept_watch: set[str] = set(watch_now)
        if WATCH_REMOVE_DELAY_SEC > 0:
            removed = self.last_sync_desired_symbols - watch_now
            for sym in removed:
                self.watch_remove_after.setdefault(sym, now + WATCH_REMOVE_DELAY_SEC)
            for sym in watch_now:
                self.watch_remove_after.pop(sym, None)
            expired = [sym for sym, deadline in self.watch_remove_after.items() if deadline <= now]
            for sym in expired:
                self.watch_remove_after.pop(sym, None)
            kept_watch |= set(self.watch_remove_after.keys())
        else:
            self.watch_remove_after.clear()

        held = _held_symbols_from_ledger()
        if held != self.last_held_symbols:
            _append(CONTROL_FILE, f"{_ts()}	HELD merge n={len(held)}")
            self.last_held_symbols = set(held)

        base_desired = kept_watch | held
        radar_syms = self._leader_radar_symbols(now, excluded=base_desired)
        if radar_syms:
            _append(CONTROL_FILE, f"{_ts()}\tLEADER_RADAR add={len(radar_syms)} thr={LEADER_RADAR_RET3S_PCT:.2f}%")
            # watchlist에도 반영 — 엔진이 score_eval을 실행하려면 watchlist.txt에 있어야 함
            try:
                wl_path = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
                existing: set[str] = set()
                try:
                    with open(wl_path, "r", encoding="utf-8") as f:
                        existing = {ln.strip() for ln in f if ln.strip()}
                except Exception:
                    pass
                new_syms = radar_syms - existing
                if new_syms:
                    with open(wl_path, "a", encoding="utf-8") as f:
                        for s in sorted(new_syms):
                            f.write(s + "\n")
                    _append(CONTROL_FILE, f"{_ts()}\tLEADER_RADAR watchlist_inject n={len(new_syms)}")
            except Exception as e:
                _append(CONTROL_FILE, f"{_ts()}\tLEADER_RADAR watchlist_inject_err {e}")
        return base_desired | radar_syms

    def feed_leader_price(self, sym: str, price: float, ts: float):
        """WS 체결 틱에서 직접 호출. REST 폴링 불필요."""
        if not LEADER_RADAR_ENABLE:
            return
        h = self.leader_radar_price_hist.setdefault(sym, deque())
        h.append((ts, price))
        cutoff = ts - 4.0
        while h and h[0][0] < cutoff:
            h.popleft()
        if not h:
            self.leader_radar_price_hist.pop(sym, None)
            return
        base = next((p for t, p in h if (ts - t) >= 3.0), None)
        if base and base > 0:
            ret3 = (price / base - 1.0) * 100.0
            if ret3 >= LEADER_RADAR_RET3S_PCT:
                self.leader_radar_active_until[sym] = ts + LEADER_RADAR_HOLD_SEC

    def _leader_radar_symbols(self, now: float, excluded: set[str]) -> set[str]:
        if (not LEADER_RADAR_ENABLE) or LEADER_RADAR_MAX_SUBS <= 0:
            self.leader_radar_active_until.clear()
            return set()

        # expire
        expired = [sym for sym, until in self.leader_radar_active_until.items() if until <= now]
        for sym in expired:
            self.leader_radar_active_until.pop(sym, None)

        # enforce max slots by remaining ttl
        ranked = sorted(self.leader_radar_active_until.items(), key=lambda kv: kv[1], reverse=True)
        keep_syms = {sym for sym, _ in ranked[:LEADER_RADAR_MAX_SUBS]}
        for sym in list(self.leader_radar_active_until.keys()):
            if sym not in keep_syms:
                self.leader_radar_active_until.pop(sym, None)

        return keep_syms - excluded

    def start(self):
        _ensure_dir(_dated_out_file())
        _append(_dated_out_file(), f"# ---- session start {_ts()} mode=real tr_ids={TR_IDS} ----")
        _append(CONTROL_FILE, f"{_ts()}\tBOOT watchlist_file={WATCHLIST_FILE}")
        if DROPPED_TR_IDS:
            _append(CONTROL_FILE, f"{_ts()}\tTR_ID_DROP unsupported={','.join(DROPPED_TR_IDS)}")
        self.approval_key = get_approval_key()


        def _send_loop():
            while not self.stop_evt.is_set():
                try:
                    msg = self.ws_send_queue.get(timeout=0.5)
                except Exception:
                    continue
                try:
                    ws_ref = self.ws
                    if ws_ref is not None:
                        if msg.is_subscription:
                            ok, reason = self._validate_sub_request(msg.tr_id or "", msg.tr_key or "", msg.tr_type or "")
                            if not ok:
                                _append(CONTROL_FILE, f"{_ts()}	SEND_DROP {reason}")
                                continue
                        ws_ref.send(msg.payload)
                except Exception as e:
                    _append(CONTROL_FILE, f"{_ts()}	SEND_ERR {type(e).__name__}: {e}")

        if self.ws_send_thread is None or (not self.ws_send_thread.is_alive()):
            self.ws_send_thread = threading.Thread(target=_send_loop, daemon=True)
            self.ws_send_thread.start()

        def on_open(ws):
            _append(CONTROL_FILE, f"{_ts()}\tOPEN {WS_URL}")
            self._sync_subscriptions(ws, self._desired_symbols(), force=True)

        def on_message(ws, message):
            s = message if isinstance(message, str) else message.decode("utf-8", "ignore")
            if s == "PINGPONG":
                try:
                    self._enqueue_ws_raw("PINGPONG")
                except Exception:
                    pass
                return
            if s.startswith("{"):
                j = {}
                try:
                    j = json.loads(s)
                    if str(j.get("header", {}).get("tr_id", "")).upper() == "PINGPONG":
                        self._enqueue_ws_raw("PINGPONG")
                        _append(CONTROL_FILE, f"{_ts()}\tPING_ACK json")
                        return
                except Exception:
                    pass
                self._handle_control_json(j if isinstance(j, dict) else {})
                _append(CONTROL_FILE, f"{_ts()}\t{s[:2000]}")
                return
            if s.startswith("0|") or s.startswith("1|"):
                _append(_dated_out_file(), f"{_ts()}\t{s}")

        def on_error(ws, err):
            _append(CONTROL_FILE, f"{_ts()}\tERR {err}")
            self.reconnect_evt.set()

        def on_close(ws, code, msg):
            _append(CONTROL_FILE, f"{_ts()}\tCLOSE {code} {msg}")
            self.reconnect_evt.set()

        def _spawn_ws():
            self.ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self.ws_thread = threading.Thread(target=lambda: self.ws.run_forever(ping_interval=30, ping_timeout=10), daemon=True)
            self.ws_thread.start()

        _spawn_ws()

        while not self.stop_evt.is_set():
            time.sleep(POLL_WATCH_SEC)
            if self.ws:
                self._sync_subscriptions(self.ws, self._desired_symbols())

            dead = (self.ws_thread is not None) and (not self.ws_thread.is_alive())
            if self.reconnect_evt.is_set() or dead:
                self.reconnect_evt.clear()
                self.subscribed.clear()
                self.subscribed_keys.clear()
                self.pending_subscribe.clear()
                self.sub_blocked_until.clear()
                self.sub_blocked_retry_exp.clear()
                self.last_sync_desired_symbols = set()
                self.last_sync_desired_keys = set()
                _append(CONTROL_FILE, f"{_ts()}\tRECONNECT start")
                try:
                    self.approval_key = get_approval_key()
                except Exception as e:
                    _append(CONTROL_FILE, f"{_ts()}\tRECONNECT approval_err {type(e).__name__}: {e}")
                    continue
                _spawn_ws()

    def _sub_key(self, sym: str, tr_id: str) -> tuple[str, str]:
        return (sym, tr_id)

    def _desired_sub_keys(self, desired_symbols: set[str]) -> list[tuple[str, str]]:
        # 한도 초과 방지를 위해 체결(H0STCNT0)을 우선 배치하고, 남는 슬롯에 호가를 배치한다.
        # 보유 종목은 구독 잘림 시에도 먼저 유지되도록 우선순위를 높인다.
        held_syms = sorted(desired_symbols & self.last_held_symbols)
        other_syms = sorted(desired_symbols - self.last_held_symbols)
        ordered_syms = held_syms + other_syms
        keys: list[tuple[str, str]] = []
        for trade_tr in PRIORITY_TRADE_TR_IDS:
            if trade_tr in TR_IDS:
                for sym in ordered_syms:
                    keys.append((sym, trade_tr))
        for tr in TR_IDS:
            if tr in PRIORITY_TRADE_TR_IDS:
                continue
            for sym in ordered_syms:
                keys.append((sym, tr))
        return keys[:max(0, MAX_ACTIVE_SUB_KEYS)]

    def _enqueue_ws_raw(self, payload: str):
        try:
            self.ws_send_queue.put_nowait(OutgoingWSMessage(payload=payload))
        except Exception:
            _append(CONTROL_FILE, f"{_ts()}	SEND_DROP queue_full")

    def _validate_sub_request(self, tr_id: str, tr_key: str, tr_type: str) -> tuple[bool, str]:
        if tr_type not in ALLOWED_TR_TYPES:
            return False, f"invalid_tr_type {tr_type}"
        if tr_id not in ALLOWED_TR_IDS:
            return False, f"invalid_tr_id {tr_id}"
        if (not re.fullmatch(r"\d{6}", tr_key or "")):
            return False, f"invalid_tr_key {tr_key}"
        return True, ""

    def _enqueue_sub_message(self, tr_id: str, sym: str, tr_type: str) -> bool:
        ok, reason = self._validate_sub_request(tr_id, sym, tr_type)
        if not ok:
            _append(CONTROL_FILE, f"{_ts()}	SUB_DROP {reason}")
            return False
        try:
            payload = build_msg(self.approval_key, tr_id, sym, tr_type)
            self.ws_send_queue.put_nowait(
                OutgoingWSMessage(
                    payload=payload,
                    tr_id=tr_id,
                    tr_key=sym,
                    tr_type=tr_type,
                )
            )
            return True
        except Exception as e:
            _append(CONTROL_FILE, f"{_ts()}	SUB_BUILD_ERR {type(e).__name__}: {e}")
            return False

    def _extract_control_fields(self, j: dict) -> tuple[str, str, str, str, str]:
        body = j.get("body", {}) if isinstance(j, dict) else {}
        header = j.get("header", {}) if isinstance(j, dict) else {}
        input_body = body.get("input", {}) if isinstance(body, dict) else {}
        output = body.get("output", {}) if isinstance(body, dict) else {}

        rt_cd = str(j.get("rt_cd", body.get("rt_cd", ""))).strip()
        msg = str(j.get("msg1", body.get("msg1", j.get("msg", "")))).strip()

        tr_id = str(
            header.get("tr_id", "")
            or body.get("tr_id", "")
            or input_body.get("tr_id", "")
            or output.get("tr_id", "")
        ).strip()
        tr_key = str(
            body.get("tr_key", "")
            or input_body.get("tr_key", "")
            or output.get("tr_key", "")
            or header.get("tr_key", "")
        ).strip()
        tr_type = str(
            header.get("tr_type", "")
            or body.get("tr_type", "")
            or input_body.get("tr_type", "")
            or output.get("tr_type", "")
        ).strip()
        return rt_cd, msg, tr_id, tr_key, tr_type

    def _refresh_subscribed_symbols(self):
        self.subscribed = {sym for (sym, _tr) in self.subscribed_keys}

    def _handle_control_json(self, j: dict):
        rt_cd, msg, tr_id, tr_key, tr_type = self._extract_control_fields(j)
        if not tr_id or not tr_key:
            return
        key = self._sub_key(tr_key, tr_id)
        now = time.time()
        if rt_cd == "0":
            was_pending_sub = key in self.pending_subscribe
            self.pending_subscribe.pop(key, None)
            self.sub_blocked_until.pop(key, None)
            self.sub_blocked_retry_exp.pop(key, None)
            if tr_type == "2":
                self.subscribed_keys.discard(key)
            elif tr_type == "1" or was_pending_sub:
                self.subscribed_keys.add(key)
            self._refresh_subscribed_symbols()
            return
        if "MAX SUBSCRIBE OVER" in msg.upper():
            exp = self.sub_blocked_retry_exp.get(key, 0)
            wait = min(MAX_SUB_BACKOFF_SEC, BASE_SUB_BACKOFF_SEC * (2 ** exp))
            self.sub_blocked_until[key] = now + wait
            self.sub_blocked_retry_exp[key] = min(exp + 1, 8)
            self.pending_subscribe.pop(key, None)
            self.subscribed_keys.discard(key)
            self._refresh_subscribed_symbols()
            _append(CONTROL_FILE, f"{_ts()}	SUB_BLOCK key={tr_key}/{tr_id} wait={wait:.1f}s msg={msg[:120]}")
            return
        if rt_cd:
            self.pending_subscribe.pop(key, None)
            self.subscribed_keys.discard(key)
            self._refresh_subscribed_symbols()

    def _unsubscribe_key(self, sym: str, tr: str):
        key = self._sub_key(sym, tr)
        self.pending_subscribe.pop(key, None)
        self.sub_blocked_until.pop(key, None)
        self.sub_blocked_retry_exp.pop(key, None)
        try:
            self._enqueue_sub_message(tr, sym, "2")
        except Exception:
            pass
        self.subscribed_keys.discard(key)

    def _try_subscribe_key(self, sym: str, tr: str) -> int:
        sent = 0
        now = time.time()
        key = self._sub_key(sym, tr)
        pending_ts = self.pending_subscribe.get(key)
        if pending_ts and (now - pending_ts) < 15.0:
            return 0
        blocked_until = self.sub_blocked_until.get(key, 0.0)
        if blocked_until > now:
            return 0
        try:
            if self._enqueue_sub_message(tr, sym, "1"):
                self.pending_subscribe[key] = now
                sent += 1
        except Exception:
            pass
        return sent

    def stop(self):
        self.stop_evt.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def _sync_subscriptions(self, ws, desired: set[str], force: bool = False):
        with self.lock:
            desired_symbols = set(desired)
            desired_key_seq = self._desired_sub_keys(desired_symbols)
            desired_keys = set(desired_key_seq)

            rem_keys = self.subscribed_keys - desired_keys
            for sym, tr in sorted(rem_keys):
                self._unsubscribe_key(sym, tr)

            add_keys_ordered = [k for k in desired_key_seq if k not in self.subscribed_keys]
            sent_req = 0
            if force or desired_keys != self.last_sync_desired_keys or add_keys_ordered:
                for sym, tr in add_keys_ordered:
                    sent_req += self._try_subscribe_key(sym, tr)
                self.last_sync_desired_keys = set(desired_keys)
                self.last_sync_desired_symbols = set(desired_symbols)

            self._refresh_subscribed_symbols()

            if add_keys_ordered or rem_keys or force or sent_req:
                blocked = 0
                now = time.time()
                for sym, tr in add_keys_ordered:
                    if self.sub_blocked_until.get(self._sub_key(sym, tr), 0.0) > now:
                        blocked += 1
                _append(
                    CONTROL_FILE,
                    f"{_ts()}	SYNC desired_sym={len(desired_symbols)} desired_key={len(desired_keys)} add={len(add_keys_ordered)} rem={len(rem_keys)} subscribed_sym={len(self.subscribed)} subscribed_key={len(self.subscribed_keys)} sent={sent_req} blocked={blocked}",
                )
