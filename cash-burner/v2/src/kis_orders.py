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

# 잔고/예수금조회 (계좌 전체 주문가능금액 확인용)
TRID_BALANCE = "TTTC8434R"
PATH_BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"

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
    """주문가능금액을 KIS 응답 변형(output/output1/output2)에서 엄격 추출한다.

    우선 canonical 키(ord_psbl_cash)만 전 후보에서 먼저 탐색해 오인 파싱을 방지한다.
    """
    canonical_keys = ("ord_psbl_cash", "ORD_PSBL_CASH")
    fallback_keys = (
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

    for k in canonical_keys:
        for out in candidates:
            val = _to_float(out.get(k))
            if val is not None:
                return val

    for k in fallback_keys:
        for out in candidates:
            val = _to_float(out.get(k))
            if val is not None:
                return val

    sample_keys = sorted({k for out in candidates for k in out.keys()})[:24]
    rt_cd = str(payload.get("rt_cd", ""))
    msg1 = str(payload.get("msg1", payload.get("msg", "")))
    raise ValueError(
        "buyable_cash_parse_error: no cash field found "
        f"(tried={canonical_keys + fallback_keys}), output_keys={sample_keys}, rt_cd={rt_cd}, msg1={msg1[:120]}"
    )


def _balance_rows(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    output2 = payload.get("output2")
    rows: list[Dict[str, Any]] = []
    if isinstance(output2, dict):
        rows.append(output2)
    elif isinstance(output2, list):
        rows.extend([it for it in output2 if isinstance(it, dict)])
    if not rows:
        out = payload.get("output")
        if isinstance(out, dict):
            rows.append(out)
    return rows


def _pick_first(rows: list[Dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        for row in rows:
            v = _to_float(row.get(k))
            if v is not None:
                return v
    return None


def account_cash_snapshot() -> Dict[str, float]:
    """계좌 기준 현금 관련 스냅샷 반환(로그/진단용)."""
    cano, prdt = split_account(ACC_NO)
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "Y",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    j = request("GET", PATH_BALANCE, TRID_BALANCE, params=params)
    rows = _balance_rows(j)
    if not rows:
        raise ValueError(
            f"account_cash_parse_error: missing output2/output(dict), top_keys={sorted(j.keys())[:12]}"
        )

    dep = _pick_first(rows, ("dnca_tot_amt", "DNCA_TOT_AMT"))
    withdrawable = _pick_first(rows, (
        "wdrw_psbl_amt", "WDRW_PSBL_AMT", "prvs_rcdl_excc_amt", "PRVS_RCDL_EXCC_AMT"
    ))
    orderable = _pick_first(rows, (
        "ord_psbl_cash", "ORD_PSBL_CASH", "ord_psbl_amt", "ORD_PSBL_AMT"
    ))
    d2_dep = _pick_first(rows, (
        "prvs_rcdl_excc_amt", "PRVS_RCDL_EXCC_AMT", "nxdy_excc_amt", "NXDY_EXCC_AMT"
    ))

    out: Dict[str, float] = {}
    if dep is not None:
        out["deposit"] = dep
    if withdrawable is not None:
        out["withdrawable"] = withdrawable
    if orderable is not None:
        out["orderable"] = orderable
    if d2_dep is not None:
        out["d2_deposit"] = d2_dep
    return out


def _extract_account_cash_from_balance(payload: Dict[str, Any]) -> float:
    """계좌 전체 기준 주문가능금액(ord_psbl_cash)을 잔고조회 응답에서 추출한다."""
    rows = _balance_rows(payload)
    if not rows:
        raise ValueError(
            f"account_cash_parse_error: missing output2/output(dict), top_keys={sorted(payload.keys())[:12]}"
        )

    orderable = _pick_first(rows, ("ord_psbl_cash", "ORD_PSBL_CASH", "ord_psbl_amt", "ORD_PSBL_AMT"))
    if orderable is not None:
        return orderable

    sample_keys = sorted({k for row in rows for k in row.keys()})[:24]
    raise ValueError(
        f"account_cash_parse_error: ord_psbl_cash missing, output_keys={sample_keys}"
    )


def account_buying_power(symbol: str = "005930", ord_dvsn: str = "01", price: str = "0") -> float:
    cano, prdt = split_account(ACC_NO)
    params = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "Y",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    j = request("GET", PATH_BALANCE, TRID_BALANCE, params=params)
    try:
        return _extract_account_cash_from_balance(j)
    except ValueError:
        # 잔고조회 응답 형식이 예상과 다르면 기존 per-symbol 조회로 폴백
        return buyable_cash(symbol=symbol, ord_dvsn=ord_dvsn, price=price)


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
