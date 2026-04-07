# src/kis_http.py
from __future__ import annotations

import os, json, time, threading, requests
from typing import Any, Dict, Optional, Tuple

BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
APP_KEY = os.getenv("KOREA_INVEST_APP_KEY", "")
APP_SECRET = os.getenv("KOREA_INVEST_APP_SECRET", "")
ACC_NO = os.getenv("KOREA_INVEST_ACC_NO", "")  # 10자리(8+2) 또는 8-02
USER_ID = os.getenv("KOREA_INVEST_USER_ID", "")  # 조건검색용(HTS ID)

TOKEN_CACHE = os.getenv("KIS_TOKEN_CACHE", os.path.join("data", "token.json"))
HTTP_TIMEOUT = float(os.getenv("KIS_HTTP_TIMEOUT", "10"))
HTTP_RETRY = int(os.getenv("KIS_HTTP_RETRY", "2"))
HTTP_RETRY_SLEEP = float(os.getenv("KIS_HTTP_RETRY_SLEEP", "0.5"))

# BUG-003: hashkey는 주문 경로에만 필요 (조회 API는 불필요한 추가 호출 방지)
_ORDER_PATH_PREFIX = "/uapi/domestic-stock/v1/trading/order"

# BUG-002: 멀티스레드 토큰 갱신 레이스컨디션 방지
_TOKEN_LOCK = threading.Lock()


def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def split_account(acc: str) -> Tuple[str, str]:
    # BUG-004: 빈 계좌번호 시 즉시 에러 (무음 실패 방지)
    acc = (acc or "").strip()
    if not acc:
        raise ValueError(
            "KOREA_INVEST_ACC_NO is not set — 거래 API 호출 불가. 계좌번호를 설정하세요."
        )
    if "-" in acc:
        a, b = acc.split("-", 1)
        return a.strip(), b.strip()
    if len(acc) >= 10:
        return acc[:8], acc[-2:]
    return acc, "01"


def _now() -> float:
    return time.time()


def _load_token() -> Optional[Dict[str, Any]]:
    try:
        with open(TOKEN_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_token(tok: Dict[str, Any]):
    _ensure_dir(TOKEN_CACHE)
    # 원자적 쓰기로 부분 쓰기 방지
    tmp = TOKEN_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False)
    os.replace(tmp, TOKEN_CACHE)


def get_access_token() -> str:
    # BUG-002: Lock으로 멀티스레드 동시 갱신 방지
    with _TOKEN_LOCK:
        cached = _load_token()
        if cached and cached.get("access_token") and cached.get("expires_at", 0) - _now() > 60:
            return cached["access_token"]

        url = f"{BASE_URL}/oauth2/tokenP"
        headers = {"content-type": "application/json; charset=utf-8"}
        body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
        r = requests.post(url, headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        if "access_token" not in j:
            raise ValueError(f"Token response missing access_token: rt_cd={j.get('rt_cd')} msg={j.get('msg1','')}")
        tok = j["access_token"]
        _save_token({"access_token": tok, "expires_at": _now() + 23 * 3600})
        return tok


def hashkey(body: Dict[str, Any]) -> str:
    tok = get_access_token()
    url = f"{BASE_URL}/uapi/hashkey"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {tok}",
        "appKey": APP_KEY,
        "appSecret": APP_SECRET,
    }
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json().get("HASH", "")


def request(method: str, path: str, tr_id: str, params: Dict[str, Any] = None, body: Dict[str, Any] = None) -> Dict[str, Any]:
    last_err: Exception | None = None
    for attempt in range(HTTP_RETRY + 1):
        try:
            tok = get_access_token()
            url = f"{BASE_URL}{path}"
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {tok}",
                "appKey": APP_KEY,
                "appSecret": APP_SECRET,
                "tr_id": tr_id,
                "custtype": "P",
            }
            # BUG-003: hashkey는 주문 경로에만 적용 (조회 API에는 불필요한 추가 REST 호출 방지)
            if method.upper() == "POST" and body is not None and path.startswith(_ORDER_PATH_PREFIX):
                headers["hashkey"] = hashkey(body)

            if method.upper() == "GET":
                r = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            else:
                r = requests.post(url, headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            # BUG-001: 4xx 에러는 재시도 불필요 (잘못된 파라미터/인증실패는 재시도해도 동일 결과)
            if isinstance(e, requests.HTTPError) and e.response is not None and 400 <= e.response.status_code < 500:
                raise
            if attempt >= HTTP_RETRY:
                raise
            time.sleep(HTTP_RETRY_SLEEP * (attempt + 1))
