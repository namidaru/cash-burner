# src/kis_orders.py
from __future__ import annotations

from typing import Any, Dict, Tuple
from kis_http import request, split_account, ACC_NO

# 매수가능조회
TRID_BUYABLE = "TTTC8908R"
PATH_BUYABLE = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"

# 매도가능수량조회
TRID_SELLABLE = "TTTC8408R"
PATH_SELLABLE = "/uapi/domestic-stock/v1/trading/inquire-psbl-sell"

# 현금주문 (문서 기준)
TRID_SELL = "TTTC0011U"
TRID_BUY  = "TTTC0012U"
PATH_ORDER = "/uapi/domestic-stock/v1/trading/order-cash"


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _extract_buyable_cash_strict(payload: Dict[str, Any]) -> float:
    """주문가능금액을 KIS 응답 변형(output/output1/output2)에서 엄격 추출한다."""
    cash_keys = (
        "ord_psbl_cash",
        "ORD_PSBL_CASH",
        "ord_psbl_cash_icdc",
        "ORD_PSBL_CASH_ICDC",
        "nrcvb_buy_amt",
        "NRCVB_BUY_AMT",
        "max_buy_amt",
        "MAX_BUY_AMT",
    )

    candidates: list[Dict[str, Any]] = []
    for root_key in ("output", "output1", "output2"):
        out = payload.get(root_key)
        if isinstance(out, dict):
            candidates.append(out)
        elif isinstance(out, list):
            for it in out:
                if isinstance(it, dict):
                    candidates.append(it)

    if not candidates:
        raise ValueError(
            f"buyable_cash_parse_error: missing output payload, top_keys={sorted(payload.keys())[:12]}"
        )

    for out in candidates:
        for k in cash_keys:
            val = _to_float(out.get(k))
            if val is not None:
                return val

    sample_keys = sorted({k for out in candidates for k in out.keys()})[:24]
    rt_cd = str(payload.get("rt_cd", ""))
    msg1 = str(payload.get("msg1", payload.get("msg", "")))
    raise ValueError(
        "buyable_cash_parse_error: no cash field found "
        f"(tried={cash_keys}), output_keys={sample_keys}, rt_cd={rt_cd}, msg1={msg1[:120]}"
    )


def buyable_cash(symbol: str, ord_dvsn: str="01", price: str="0") -> float:
    cano, prdt = split_account(ACC_NO)
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "PDNO": symbol,
        "ORD_DVSN": ord_dvsn,
        "ORD_UNPR": str(price),
        # KIS 문서 필수 파라미터. 누락 시 rt_cd/msg만 오고 output이 비는 케이스가 발생한다.
        "CMA_EVLU_AMT_ICLD_YN": "Y",
        "OVRS_ICLD_YN": "N",
    }
    j = request("GET", PATH_BUYABLE, TRID_BUYABLE, params=params)
    try:
        return _extract_buyable_cash_strict(j)
    except ValueError as e:
        rt_cd = str(j.get("rt_cd", ""))
        msg_cd = str(j.get("msg_cd", ""))
        msg1 = str(j.get("msg1", j.get("msg", "")))
        raise ValueError(f"{e}; rt_cd={rt_cd}; msg_cd={msg_cd}; msg={msg1[:160]}")


def sellable_qty(symbol: str) -> int:
    cano, prdt = split_account(ACC_NO)
    params = {"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": symbol}
    j = request("GET", PATH_SELLABLE, TRID_SELLABLE, params=params)
    out = j.get("output", {}) or j.get("output1", {}) or {}
    for k in ("ord_psbl_qty", "ORD_PSBL_QTY", "sell_psbl_qty"):
        n = _to_float(out.get(k))
        if n is not None:
            return int(n)
    return 0


def account_buying_power(symbol: str = "005930", ord_dvsn: str = "01", price: str = "0") -> float:
    return buyable_cash(symbol=symbol, ord_dvsn=ord_dvsn, price=price)


def order_cash(side: str, symbol: str, qty: int, ord_dvsn: str="01", ord_unpr: str="0") -> Dict[str,Any]:
    cano, prdt = split_account(ACC_NO)
    tr_id = TRID_BUY if side.upper()=="BUY" else TRID_SELL
    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "PDNO": symbol,
        "ORD_DVSN": ord_dvsn,
        "ORD_QTY": str(int(qty)),
        "ORD_UNPR": str(ord_unpr),
    }
    return request("POST", PATH_ORDER, tr_id, body=body)
