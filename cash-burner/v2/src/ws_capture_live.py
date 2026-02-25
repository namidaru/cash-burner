from __future__ import annotations

import os, json, time, threading, csv
import requests, websocket

APP_KEY = os.getenv("KOREA_INVEST_APP_KEY","")
APP_SECRET = os.getenv("KOREA_INVEST_APP_SECRET","")

WS_URL = os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:21000")
BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

def _dated_out_file() -> str:
    raw = os.getenv("OUT_FILE", os.path.join("data", "ws_dump.log"))
    ymd = time.strftime("%Y%m%d")
    if "{date}" in raw:
        return raw.replace("{date}", ymd)
    base, ext = os.path.splitext(raw)
    if not ext:
        ext = ".log"
    return f"{base}_{ymd}{ext}"


OUT_FILE = _dated_out_file()
CONTROL_FILE = os.getenv("CONTROL_FILE", os.path.join("data", "ws_control.log"))
WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", os.path.join("data", "watchlist.txt"))
LEDGER_FILE = os.getenv("LEDGER_FILE", os.path.join("data", "ledger_real.csv"))
PREOPEN_TRACK_MIN = int(os.getenv("PREOPEN_TRACK_MIN", "15"))
PREOPEN_START_HHMM = int(os.getenv("PREOPEN_START_HHMM", "900"))

TR_IDS = [t.strip() for t in os.getenv("TR_IDS", "H0STCNT0,H0STASP0").split(",") if t.strip()]
POLL_WATCH_SEC = float(os.getenv("WATCH_POLL_SEC", "2.0"))
MAX_SUB_BACKOFF_SEC = float(os.getenv("WS_SUB_BACKOFF_MAX_SEC", "60"))
BASE_SUB_BACKOFF_SEC = float(os.getenv("WS_SUB_BACKOFF_BASE_SEC", "2"))


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


class WSCapture:
    def __init__(self):
        self.approval_key = None
        self.ws = None
        self.ws_thread = None
        self.stop_evt = threading.Event()
        self.reconnect_evt = threading.Event()
        self.subscribed = set()
        self.pending_subscribe: dict[tuple[str, str], float] = {}
        self.sub_blocked_until: dict[tuple[str, str], float] = {}
        self.sub_blocked_retry_exp: dict[tuple[str, str], int] = {}
        self.last_sync_desired: set[str] = set()
        self.lock = threading.Lock()
        self.preopen_day = ""
        self.preopen_snapshot: set[str] = set()
        self.last_held_symbols: set[str] = set()

    def _in_preopen_window(self, ts_epoch: float | None = None) -> bool:
        hhmm = _hhmm_now(ts_epoch)
        start_h = PREOPEN_START_HHMM // 100
        start_m = PREOPEN_START_HHMM % 100
        start_min = start_h * 60 + start_m
        now_min = (hhmm // 100) * 60 + (hhmm % 100)
        return start_min <= now_min < (start_min + PREOPEN_TRACK_MIN)

    def _desired_symbols(self) -> set[str]:
        today = time.strftime("%Y%m%d")
        current_watchlist = read_watchlist()

        if self.preopen_day != today:
            self.preopen_day = today
            self.preopen_snapshot = set()

        if self._in_preopen_window():
            if not self.preopen_snapshot and current_watchlist:
                self.preopen_snapshot = set(current_watchlist)
                _append(CONTROL_FILE, f"{_ts()}\tPREOPEN snapshot n={len(self.preopen_snapshot)}")
            base = self.preopen_snapshot or current_watchlist
        else:
            base = current_watchlist

        held = _held_symbols_from_ledger()
        if held != self.last_held_symbols:
            _append(CONTROL_FILE, f"{_ts()}\tHELD merge n={len(held)}")
            self.last_held_symbols = set(held)
        return set(base) | held

    def start(self):
        _ensure_dir(OUT_FILE)
        _append(OUT_FILE, f"# ---- session start {_ts()} mode=real tr_ids={TR_IDS} ----")
        _append(CONTROL_FILE, f"{_ts()}\tBOOT watchlist_file={WATCHLIST_FILE}")
        self.approval_key = get_approval_key()

        def on_open(ws):
            _append(CONTROL_FILE, f"{_ts()}\tOPEN {WS_URL}")
            self._sync_subscriptions(ws, self._desired_symbols(), force=True)

        def on_message(ws, message):
            s = message if isinstance(message, str) else message.decode("utf-8", "ignore")
            if s == "PINGPONG":
                try:
                    ws.send("PINGPONG")
                except Exception:
                    pass
                return
            if s.startswith("{"):
                j = {}
                try:
                    j = json.loads(s)
                    if str(j.get("header", {}).get("tr_id", "")).upper() == "PINGPONG":
                        ws.send("PINGPONG")
                        _append(CONTROL_FILE, f"{_ts()}\tPING_ACK json")
                        return
                except Exception:
                    pass
                self._handle_control_json(j if isinstance(j, dict) else {})
                _append(CONTROL_FILE, f"{_ts()}\t{s[:2000]}")
                return
            if s.startswith("0|") or s.startswith("1|"):
                _append(OUT_FILE, f"{_ts()}\t{s}")

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
                self.pending_subscribe.clear()
                self.sub_blocked_until.clear()
                self.sub_blocked_retry_exp.clear()
                self.last_sync_desired = set()
                _append(CONTROL_FILE, f"{_ts()}\tRECONNECT start")
                try:
                    self.approval_key = get_approval_key()
                except Exception as e:
                    _append(CONTROL_FILE, f"{_ts()}\tRECONNECT approval_err {type(e).__name__}: {e}")
                    continue
                _spawn_ws()

    def _sub_key(self, sym: str, tr_id: str) -> tuple[str, str]:
        return (sym, tr_id)

    def _handle_control_json(self, j: dict):
        body = j.get("body", {}) if isinstance(j, dict) else {}
        header = j.get("header", {}) if isinstance(j, dict) else {}
        rt_cd = str(j.get("rt_cd", body.get("rt_cd", ""))).strip()
        msg = str(j.get("msg1", body.get("msg1", ""))).strip()
        tr_id = str(header.get("tr_id", body.get("tr_id", ""))).strip()
        tr_key = str(body.get("tr_key", body.get("input", {}).get("tr_key", ""))).strip()
        if not tr_id or not tr_key:
            return
        key = self._sub_key(tr_key, tr_id)
        now = time.time()
        if rt_cd == "0":
            self.pending_subscribe.pop(key, None)
            self.sub_blocked_until.pop(key, None)
            self.sub_blocked_retry_exp.pop(key, None)
            self.subscribed.add(tr_key)
            return
        if "MAX SUBSCRIBE OVER" in msg.upper():
            exp = self.sub_blocked_retry_exp.get(key, 0)
            wait = min(MAX_SUB_BACKOFF_SEC, BASE_SUB_BACKOFF_SEC * (2 ** exp))
            self.sub_blocked_until[key] = now + wait
            self.sub_blocked_retry_exp[key] = min(exp + 1, 8)
            self.pending_subscribe.pop(key, None)
            self.subscribed.discard(tr_key)
            _append(CONTROL_FILE, f"{_ts()}	SUB_BLOCK key={tr_key}/{tr_id} wait={wait:.1f}s msg={msg[:120]}")
            return
        if rt_cd:
            self.pending_subscribe.pop(key, None)
            self.subscribed.discard(tr_key)

    def _unsubscribe_symbol(self, ws, sym: str):
        for tr in TR_IDS:
            key = self._sub_key(sym, tr)
            self.pending_subscribe.pop(key, None)
            self.sub_blocked_until.pop(key, None)
            self.sub_blocked_retry_exp.pop(key, None)
            try:
                ws.send(build_msg(self.approval_key, tr, sym, "0"))
            except Exception:
                pass
        self.subscribed.discard(sym)

    def _try_subscribe_symbol(self, ws, sym: str) -> int:
        sent = 0
        now = time.time()
        for tr in TR_IDS:
            key = self._sub_key(sym, tr)
            if self.pending_subscribe.get(key):
                continue
            blocked_until = self.sub_blocked_until.get(key, 0.0)
            if blocked_until > now:
                continue
            try:
                ws.send(build_msg(self.approval_key, tr, sym, "1"))
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
            desired = set(desired)
            rem = self.subscribed - desired
            if rem:
                for sym in sorted(rem):
                    self._unsubscribe_symbol(ws, sym)

            add = desired - self.subscribed
            sent_req = 0
            if force or desired != self.last_sync_desired or add:
                for sym in sorted(add):
                    sent_req += self._try_subscribe_symbol(ws, sym)
                self.last_sync_desired = set(desired)

            if add or rem or force or sent_req:
                blocked = 0
                now = time.time()
                for sym in add:
                    if any(self.sub_blocked_until.get(self._sub_key(sym, tr), 0.0) > now for tr in TR_IDS):
                        blocked += 1
                _append(
                    CONTROL_FILE,
                    f"{_ts()}	SYNC desired={len(desired)} add={len(add)} rem={len(rem)} subscribed={len(self.subscribed)} sent={sent_req} blocked={blocked}",
                )
