# src/kis_http.py
from __future__ import annotations

import os, json, time, requests
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

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def split_account(acc: str) -> Tuple[str, str]:
    acc = (acc or "").strip()
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
    with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False)

def get_access_token() -> str:
    cached = _load_token()
    if cached and cached.get("access_token") and cached.get("expires_at", 0) - _now() > 60:
        return cached["access_token"]

    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json; charset=utf-8"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    j = r.json()
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
            if method.upper() == "POST" and body is not None:
                headers["hashkey"] = hashkey(body)

            if method.upper() == "GET":
                r = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            else:
                r = requests.post(url, headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt >= HTTP_RETRY:
                raise
            time.sleep(HTTP_RETRY_SLEEP * (attempt + 1))

