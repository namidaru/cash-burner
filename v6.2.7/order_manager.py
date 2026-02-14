#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
주문 관리자
"""

import time
import threading
from enum import Enum
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from utils import logger

# ============================================================================
# OrderStatus & OrderInfo
# ============================================================================
class OrderStatus(Enum):
    """주문 상태"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMEOUT = "timeout"

@dataclass
class OrderInfo:
    """주문 정보"""
    order_no: str
    code: str
    side: str
    total_qty: int
    filled_qty: int = 0
    order_price: int = 0
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = 0.0
    retry_count: int = 0
    
    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()
    
    def is_filled(self) -> bool:
        """전량 체결 여부"""
        return self.filled_qty >= self.total_qty
    
    def is_partial(self) -> bool:
        """부분 체결 여부"""
        return 0 < self.filled_qty < self.total_qty
    
    def fill_ratio(self) -> float:
        """체결 비율"""
        return (self.filled_qty / self.total_qty * 100) if self.total_qty > 0 else 0.0

# ============================================================================
# OrderManager
# ============================================================================
class OrderManager:
    """이벤트 기반 주문 관리 시스템"""
    
    def __init__(self):
        self.orders: Dict[str, OrderInfo] = {}
        self.lock = threading.Lock()
        self.timeout_checker_thread = None
        self.running = False
        self.fill_timeout = 5.0
        self.max_wait_time = 10.0
        self.minimum_fill_ratio = 50.0
        self.api_check_interval = 1.0
    
    def start(self):
        """OrderManager 시작"""
        self.running = True
        self.timeout_checker_thread = threading.Thread(
            target=self._timeout_checker, daemon=True
        )
        self.timeout_checker_thread.start()
        logger.info("✅ OrderManager 시작 (이벤트 기반 체결 확인)")
    
    def stop(self):
        """OrderManager 종료"""
        self.running = False
        if self.timeout_checker_thread:
            self.timeout_checker_thread.join(timeout=2)
        logger.info("🛑 OrderManager 종료")
    
    def register_order(self, order_info: OrderInfo):
        """주문 등록"""
        with self.lock:
            self.orders[order_info.order_no] = order_info
            logger.debug(f"📝 주문 등록: {order_info.code} {order_info.order_no}")
    
    def update_fill(self, order_no: str, filled_qty: int, fill_price: int = 0):
        """체결 정보 업데이트 (웹소켓에서 호출)"""
        with self.lock:
            if order_no not in self.orders:
                return
            
            order = self.orders[order_no]
            order.filled_qty = filled_qty
            
            if order.is_filled():
                order.status = OrderStatus.FILLED
                elapsed = time.time() - order.created_at
                logger.info(f"✅ 전량 체결 감지: {order.code} {filled_qty}주 "
                           f"@ {fill_price:,}원 (소요: {elapsed:.2f}초)")
            elif order.is_partial():
                order.status = OrderStatus.PARTIALLY_FILLED
                logger.info(f"💡 부분 체결: {order.code} {order.fill_ratio():.1f}%")
    
    def get_order_status(self, order_no: str) -> Optional[OrderInfo]:
        """주문 상태 조회"""
        with self.lock:
            return self.orders.get(order_no)
    
    def wait_for_fill(self, order_no: str, timeout: float = 5.0, 
                      check_order_func=None) -> Tuple[bool, Optional[OrderInfo]]:
        """
        체결 대기 (이벤트 기반 + API 폴백)
        
        Args:
            order_no: 주문번호
            timeout: 타임아웃 (초)
            check_order_func: API 체결 확인 함수
        
        Returns:
            (성공 여부, OrderInfo)
        """
        start_time = time.time()
        last_api_check = 0
        
        while time.time() - start_time < timeout:
            order = self.get_order_status(order_no)
            
            if not order:
                return False, None
            
            # 전량 체결
            if order.status == OrderStatus.FILLED:
                elapsed = time.time() - order.created_at
                logger.info(f"⚡ 체결 완료! (실제 소요: {elapsed:.2f}초)")
                return True, order
            
            # 타임아웃
            if order.status == OrderStatus.TIMEOUT:
                return False, order
            
            # API 폴백 확인
            current_time = time.time()
            if check_order_func and current_time - start_time >= last_api_check + self.api_check_interval:
                try:
                    is_filled, api_qty = check_order_func(order_no)
                    if api_qty > 0:
                        self.update_fill(order_no, api_qty)
                        logger.debug(f"📡 API 체결 확인: {api_qty}주")
                        
                        if is_filled:
                            order.status = OrderStatus.FILLED
                            order.filled_qty = api_qty
                            elapsed = time.time() - order.created_at
                            logger.info(f"⚡ 체결 완료! (API 확인, 소요: {elapsed:.2f}초)")
                            return True, order
                except Exception as e:
                    logger.debug(f"API 체결 확인 오류: {e}")
                
                last_api_check = current_time - start_time
            
            # 부분 체결 수용
            if order.is_partial() and time.time() - start_time > timeout * 0.7:
                fill_ratio = order.fill_ratio()
                
                if fill_ratio >= self.minimum_fill_ratio:
                    logger.info(f"⏰ 부분 체결 수용: {fill_ratio:.1f}% (최소 {self.minimum_fill_ratio:.0f}% 충족)")
                    order.status = OrderStatus.FILLED
                    return True, order
                else:
                    logger.warning(f"⚠️  부분 체결 부족: {fill_ratio:.1f}% (최소 {self.minimum_fill_ratio:.0f}% 필요)")
            
            time.sleep(0.1)
        
        # 타임아웃 - 마지막 API 확인
        if check_order_func:
            try:
                is_filled, api_qty = check_order_func(order_no)
                if api_qty > 0:
                    self.update_fill(order_no, api_qty)
                    order = self.get_order_status(order_no)
                    
                    if order and is_filled:
                        order.status = OrderStatus.FILLED
                        logger.info(f"✅ 최종 API 확인: 체결 완료 {api_qty}주")
                        return True, order
                    
                    if order and order.fill_ratio() >= self.minimum_fill_ratio:
                        order.status = OrderStatus.FILLED
                        logger.info(f"✅ 최종 부분 체결 수용: {order.fill_ratio():.1f}%")
                        return True, order
            except Exception as e:
                logger.debug(f"최종 API 확인 오류: {e}")
        
        # 타임아웃
        order = self.get_order_status(order_no)
        if order:
            order.status = OrderStatus.TIMEOUT
            logger.warning(f"⏰ 체결 타임아웃: {order.code} (체결률: {order.fill_ratio():.1f}%)")
        
        return False, order
    
    def _timeout_checker(self):
        """백그라운드 타임아웃 체커"""
        while self.running:
            try:
                current_time = time.time()
                
                with self.lock:
                    for order_no, order in list(self.orders.items()):
                        elapsed = current_time - order.created_at
                        
                        if order.status in [OrderStatus.SUBMITTED, OrderStatus.PENDING]:
                            if elapsed > self.fill_timeout:
                                if order.is_partial():
                                    order.status = OrderStatus.PARTIALLY_FILLED
                                else:
                                    order.status = OrderStatus.TIMEOUT
                        
                        # 오래된 주문 정리
                        if elapsed > self.max_wait_time:
                            if order.status not in [OrderStatus.FILLED, OrderStatus.CANCELLED]:
                                logger.debug(f"🗑️  오래된 주문 제거: {order_no}")
                                del self.orders[order_no]
                
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"타임아웃 체커 오류: {e}")
                time.sleep(1)
    
    def cancel_order_status(self, order_no: str):
        """주문 취소 표시"""
        with self.lock:
            if order_no in self.orders:
                self.orders[order_no].status = OrderStatus.CANCELLED
