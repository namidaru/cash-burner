#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매매 로직 v6.3.0 (스코어링 시스템 적용)
"""

import time
import threading
from datetime import datetime, timedelta
from typing import Tuple, Dict
from collections import deque

from config import (
    MAX_STOCKS, INVESTMENT_RATIO, COOLDOWN_MINUTES, TRADING_START_TIME,
    EMERGENCY_CLOSE_TIME, STOP_LOSS, PROFIT_TARGET, TRAILING_STOP,
    TRAILING_ACTIVATION, SLIPPAGE_TICKS, MIN_VOL_RATIO, EXCLUDE_KEYWORDS,
    MIN_PRICE, MAX_PRICE, MIN_PRICE_CHANGE, MAX_PRICE_CHANGE,
    DAILY_MAX_TRADES, DAILY_MAX_LOSS_RATE, MAX_POSITION_PER_STOCK,
    get_time_based_thresholds  # 🔥 추가
)
from utils import logger, send_discord_message, get_tick_size, calculate_order_price, calculate_total_cost
from api_client import (
    get_stock_info, get_price, get_balance, get_holdings, holdings_data,
    check_vi_status, check_vi_cooldown, vi_triggered_codes, vi_cooldown_time,
    place_order, check_order_status, cancel_order
)
from order_manager import OrderManager, OrderInfo, OrderStatus
from surge_validator import SurgeValidator

# 전역 변수
order_manager = OrderManager()
surge_validator = SurgeValidator()
pending_orders = set()
last_trade_time = {}
sold_codes = set()
emergency_stop = False
trading_enabled = True
trade_history = deque(maxlen=100)
order_lock = threading.Lock()
daily_stats = {
    'start_balance': 0,
    'current_balance': 0,
    'trades_count': 0,
    'profit_loss': 0,
    'last_reset': datetime.now().date()
}

def can_trade(code: str) -> Tuple[bool, str]:
    """거래 가능 여부 확인"""
    if emergency_stop:
        return False, "긴급 정지"
    
    if code in sold_codes:
        return False, "금일 매도 종목"
    
    if code in pending_orders:
        return False, "주문 처리 중"
    
    if code in last_trade_time:
        elapsed = (datetime.now() - last_trade_time[code]).total_seconds() / 60
        if elapsed < COOLDOWN_MINUTES:
            return False, f"쿨다운 ({int(COOLDOWN_MINUTES - elapsed)}분 남음)"
    
    if check_vi_cooldown(code):
        return False, "VI 쿨다운"
    
    # 일일 리스크 확인
    if daily_stats['trades_count'] >= DAILY_MAX_TRADES:
        return False, f"일일 최대 매매 횟수 초과 ({DAILY_MAX_TRADES}회)"
    
    current_balance = get_balance()
    holdings = get_holdings()
    
    total_value = current_balance
    for h in holdings:
        price = get_price(h['code'])
        if price > 0:
            total_value += price * h['qty']
    
    if daily_stats['start_balance'] > 0:
        loss_rate = (total_value - daily_stats['start_balance']) / daily_stats['start_balance']
        if loss_rate <= -DAILY_MAX_LOSS_RATE:
            return False, f"일일 최대 손실률 도달 ({loss_rate*100:.1f}%)"
    
    return True, ""

def is_valid_stock(info: Dict) -> Tuple[bool, str]:
    """
    종목 필터링 (개선 버전)
    
    🔥 변경사항:
    - 시간대별 동적 기준 적용
    """
    name = info['name']
    price = info['price']
    change_rate = info['change_rate']
    
    # 🔥 시간대별 설정
    thresholds = get_time_based_thresholds()
    
    if any(kw in name for kw in EXCLUDE_KEYWORDS):
        return False, f"제외 키워드: {name}"
    
    if not (MIN_PRICE <= price <= MAX_PRICE):
        return False, f"가격 범위: {price:,}원"
    
    if change_rate >= 28:
        return False, f"상한가 근접: +{change_rate:.1f}%"
    
    # 🔥 동적 등락률 기준
    if not (thresholds['min_price_change'] <= change_rate <= MAX_PRICE_CHANGE):
        return False, f"등락률 범위: {change_rate:.1f}%"
    
    if '관리' in name or '주의' in name or '경고' in name:
        return False, f"관리/주의 종목: {name}"
    
    return True, ""

def order_with_retry(code: str, qty: int, side: str, max_retries: int = 3) -> Dict:
    """재시도 주문"""
    for attempt in range(max_retries):
        try:
            current_price = get_price(code)
            if current_price == 0:
                logger.error(f"❌ 가격 조회 실패: {code}")
                return {'success': False, 'msg': '가격 조회 실패'}
            
            result = place_order(code, qty, side)
            
            if not result['success']:
                logger.warning(f"⚠️  주문 실패 ({attempt+1}/{max_retries}): {result.get('msg')}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return result
            
            order_no = result.get('order_no')
            order_price = result.get('order_price')
            
            if not order_no or order_no == 'N/A':
                logger.warning(f"⚠️  주문번호 없음, 재시도")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return result
            
            # OrderManager에 등록
            order_info = OrderInfo(
                order_no=order_no,
                code=code,
                side=side,
                total_qty=qty,
                order_price=order_price,
                status=OrderStatus.SUBMITTED,
                retry_count=attempt
            )
            order_manager.register_order(order_info)
            
            # 체결 대기
            timeout = 5.0 + (attempt * 1.0)
            logger.info(f"⏳ 체결 감지 중... (최대 {timeout:.1f}초)")
            
            is_filled, final_order = order_manager.wait_for_fill(
                order_no, timeout, check_order_func=check_order_status
            )
            
            if is_filled and final_order:
                elapsed = time.time() - order_info.created_at
                logger.info(f"✅ 체결 완료: {code} {final_order.filled_qty}주 @ {order_price:,}원 "
                           f"(소요: {elapsed:.2f}초)")
                return {
                    'success': True,
                    'order_no': order_no,
                    'order_price': order_price,
                    'filled_qty': final_order.filled_qty
                }
            
            # 미체결 처리
            logger.warning(f"⚠️  미체결 ({attempt+1}/{max_retries}): {code}")
            
            if final_order and final_order.is_partial():
                fill_ratio = final_order.fill_ratio()
                
                if fill_ratio >= 50.0:
                    logger.info(f"💡 부분 체결 수용: {final_order.filled_qty}/{qty}주 ({fill_ratio:.1f}%)")
                    return {
                        'success': True,
                        'order_no': order_no,
                        'order_price': order_price,
                        'filled_qty': final_order.filled_qty,
                        'partial': True
                    }
            
            # 취소 후 재시도
            if attempt < max_retries - 1:
                cancel_success = cancel_order(code, order_no, qty)
                
                if cancel_success:
                    order_manager.cancel_order_status(order_no)
                    time.sleep(0.5 + (attempt * 0.3))
                    
                    # 슬리피지 증가
                    new_price = get_price(code)
                    if new_price > 0:
                        aggressive_ticks = SLIPPAGE_TICKS + (attempt + 1) * 2
                        continue
                else:
                    logger.warning(f"⚠️  취소 실패, 그대로 진행")
                    return result
            
        except Exception as e:
            logger.error(f"❌ 주문 오류 ({attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
    
    return {'success': False, 'msg': '최대 재시도 초과'}

def execute_buy_on_surge(code: str):
    """
    급등 시 매수 (개선 버전)
    
    🔥 주요 개선:
    - 스코어링 시스템 적용
    - 시간대별 동적 점수 기준
    """
    global emergency_stop, trading_enabled, daily_stats
    
    if not trading_enabled:
        return
    
    can_trade_now, reason = can_trade(code)
    if not can_trade_now:
        logger.debug(f"거래 불가 ({code}): {reason}")
        return
    
    try:
        now = datetime.now()
        current_time = now.time()
        trading_start = datetime.strptime(TRADING_START_TIME, "%H:%M").time()
        emergency_close = datetime.strptime(EMERGENCY_CLOSE_TIME, "%H:%M").time()
        
        if current_time < trading_start or current_time >= emergency_close:
            return
        
        # VI 체크
        is_vi, vi_type = check_vi_status(code)
        if is_vi:
            logger.warning(f"⚠️  VI 발동 감지: {code} ({vi_type})")
            vi_triggered_codes.add(code)
            vi_cooldown_time[code] = datetime.now()
            return
        
        # 종목 정보 조회
        info = get_stock_info(code)
        if not info:
            return
        
        # 종목 필터링
        is_valid, invalid_reason = is_valid_stock(info)
        if not is_valid:
            logger.debug(f"종목 제외 ({code}): {invalid_reason}")
            return
        
        name = info['name']
        price = info['price']
        change_rate = info.get('change_rate', 0)
        vol_ratio = info.get('vol_ratio', 0)
        
        # 보유 종목 수 확인
        with order_lock:
            if len(holdings_data) >= MAX_STOCKS:
                return
        
        # 급등 일관성 확인
        if not surge_validator.is_consistent_surge(code):
            return
        
        # 🔥 스코어링 시스템 적용
        # 체결강도는 surge_validator에서 이미 계산됨
        surge_records = surge_validator.surge_data.get(code, [])
        if not surge_records:
            return
        
        recent_strengths = [d['strength'] for d in surge_records]
        avg_strength = sum(recent_strengths) / len(recent_strengths) if recent_strengths else 0
        
        # 거래량 비율 계산 (실제로는 API에서 가져와야 함)
        # 현재는 info에서 가져오거나 추정
        if 'vol_ratio' not in info or info['vol_ratio'] == 0:
            # API 호출로 정확한 거래량 비율 계산
            try:
                from api_client import api_call
                data = api_call("FHKST01010100", "/uapi/domestic-stock/v1/quotations/inquire-price",
                    {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
                
                if data and 'output' in data:
                    output = data['output']
                    volume = int(output.get('acml_vol', 0))
                    avg_volume = int(output.get('prdy_vol', 1))
                    vol_ratio = (volume / avg_volume * 100) if avg_volume > 0 else 0
                else:
                    vol_ratio = 250.0  # 기본값
            except:
                vol_ratio = 250.0  # 기본값
        else:
            vol_ratio = info['vol_ratio']
        
        # 모멘텀 점수 계산
        momentum_score = surge_validator.calculate_momentum_score(
            code=code,
            strength=avg_strength,
            vol_ratio=vol_ratio,
            change_rate=change_rate,
            price=price
        )
        
        # 점수 기준 확인
        if not surge_validator.meets_threshold(momentum_score):
            thresholds = get_time_based_thresholds()
            logger.debug(f"점수 부족 ({code}): {momentum_score}점 < {thresholds['score_threshold']}점")
            return
        
        logger.info(f"📊 모멘텀 점수: {momentum_score}점 ✅")
        
        # 매수 수량 계산
        cash = get_balance()
        total_invest = int(cash * INVESTMENT_RATIO)
        
        order_cost = calculate_total_cost(price, 1, True)
        max_qty = total_invest // order_cost
        
        if max_qty < 1:
            return
        
        # 종목당 최대 비중 제한
        holdings = get_holdings()
        total_value = cash
        for h in holdings:
            p = get_price(h['code'])
            if p > 0:
                total_value += p * h['qty']
        
        max_stock_value = int(total_value * MAX_POSITION_PER_STOCK)
        max_qty_by_position = max_stock_value // price
        max_qty = min(max_qty, max_qty_by_position)
        
        if max_qty < 1:
            return
        
        # 주문
        pending_orders.add(code)
        
        try:
            logger.info(f"\n{'='*60}")
            logger.info(f"🔥 급등 매수 신호: {name} ({code})")
            logger.info(f"현재가: {price:,}원 | 수량: {max_qty}주 | 점수: {momentum_score}점")
            logger.info(f"체결강도: {avg_strength:.1f} | 거래량: {vol_ratio:.0f}% | 상승률: {change_rate:.1f}%")
            logger.info(f"{'='*60}")
            
            result = order_with_retry(code, max_qty, "buy", max_retries=3)
            
            if result['success']:
                filled_qty = result.get('filled_qty', max_qty)
                order_price = result.get('order_price', price)
                
                # 보유 정보 업데이트
                with order_lock:
                    holdings_data[code] = {
                        'code': code,
                        'name': name,
                        'qty': filled_qty,
                        'avg_price': order_price,
                        'current_price': price,
                        'buy_time': datetime.now(),
                        'highest_price': price,
                        'trailing_active': False,
                        'momentum_score': momentum_score  # 🔥 점수 저장
                    }
                
                last_trade_time[code] = datetime.now()
                daily_stats['trades_count'] += 1
                
                send_discord_message(
                    f"**💰 매수 체결**\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"종목: {name}\n"
                    f"가격: ₩{order_price:,}\n"
                    f"수량: {filled_qty}주\n"
                    f"점수: {momentum_score}점\n"
                    f"━━━━━━━━━━━━━━━"
                )
                
                logger.info(f"✅ 매수 성공: {name} {filled_qty}주 @ {order_price:,}원")
            else:
                logger.error(f"❌ 매수 실패: {result.get('msg')}")
                send_discord_message(f"❌ 매수 실패: {name}\n사유: {result.get('msg')}")
        
        finally:
            if code in pending_orders:
                pending_orders.remove(code)
    
    except Exception as e:
        logger.error(f"❌ 매수 오류: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
        if code in pending_orders:
            pending_orders.remove(code)

def check_sell_positions(data_store):
    """매도 체크"""
    global daily_stats
    
    with order_lock:
        codes_to_sell = []
        
        for code, holding in list(holdings_data.items()):
            try:
                current_price = data_store.get_price(code)
                
                if current_price == 0:
                    current_price = get_price(code)
                
                if current_price == 0:
                    continue
                
                buy_price = holding['avg_price']
                qty = holding['qty']
                name = holding['name']
                
                profit_rate = (current_price - buy_price) / buy_price
                
                # 고점 업데이트
                if current_price > holding.get('highest_price', buy_price):
                    holding['highest_price'] = current_price
                
                highest_price = holding.get('highest_price', current_price)
                from_high_rate = (current_price - highest_price) / highest_price
                
                # 매도 조건
                should_sell = False
                sell_reason = ""
                
                # 손절
                if profit_rate <= -STOP_LOSS:
                    should_sell = True
                    sell_reason = f"손절 ({profit_rate*100:.1f}%)"
                
                # 익절
                elif profit_rate >= PROFIT_TARGET:
                    should_sell = True
                    sell_reason = f"익절 ({profit_rate*100:.1f}%)"
                
                # 트레일링 스톱
                elif profit_rate >= TRAILING_ACTIVATION:
                    if not holding.get('trailing_active'):
                        holding['trailing_active'] = True
                        logger.info(f"🎯 트레일링 스톱 활성화: {name}")
                    
                    if from_high_rate <= -TRAILING_STOP:
                        should_sell = True
                        sell_reason = f"트레일링 ({from_high_rate*100:.1f}%)"
                
                if should_sell:
                    codes_to_sell.append((code, name, qty, current_price, sell_reason))
                    holdings_data[code]['current_price'] = current_price
            
            except Exception as e:
                logger.error(f"❌ 매도 체크 오류 ({code}): {e}")
        
        # 매도 실행
        for code, name, qty, price, reason in codes_to_sell:
            try:
                pending_orders.add(code)
                
                logger.info(f"\n{'='*60}")
                logger.info(f"📤 매도 신호: {name} ({code})")
                logger.info(f"사유: {reason} | 수량: {qty}주 @ {price:,}원")
                logger.info(f"{'='*60}")
                
                result = order_with_retry(code, qty, "sell", max_retries=3)
                
                if result['success']:
                    sell_price = result.get('order_price', price)
                    buy_price = holdings_data[code]['avg_price']
                    
                    net_profit, profit_rate = calculate_total_cost(buy_price, qty, True), \
                                              calculate_total_cost(sell_price, qty, False)
                    net_profit = profit_rate - net_profit
                    profit_rate_pct = (sell_price - buy_price) / buy_price * 100
                    
                    daily_stats['profit_loss'] += net_profit
                    
                    send_discord_message(
                        f"**💸 매도 체결**\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"종목: {name}\n"
                        f"가격: ₩{sell_price:,}\n"
                        f"수량: {qty}주\n"
                        f"수익: ₩{net_profit:+,} ({profit_rate_pct:+.1f}%)\n"
                        f"사유: {reason}\n"
                        f"━━━━━━━━━━━━━━━"
                    )
                    
                    logger.info(f"✅ 매도 성공: {name} @ {sell_price:,}원 (수익: {net_profit:+,}원)")
                    
                    del holdings_data[code]
                    sold_codes.add(code)
                else:
                    logger.error(f"❌ 매도 실패: {result.get('msg')}")
                
                if code in pending_orders:
                    pending_orders.remove(code)
            
            except Exception as e:
                logger.error(f"❌ 매도 오류 ({code}): {e}")
                if code in pending_orders:
                    pending_orders.remove(code)

def emergency_liquidate_all():
    """긴급 전량 청산"""
    global emergency_stop
    
    emergency_stop = True
    logger.warning("🚨 긴급 청산 시작!")
    send_discord_message("**🚨 긴급 청산 시작**")
    
    with order_lock:
        codes_to_sell = list(holdings_data.items())
    
    for code, holding in codes_to_sell:
        try:
            name = holding['name']
            qty = holding['qty']
            price = get_price(code)
            
            if price == 0:
                continue
            
            logger.warning(f"🚨 긴급 매도: {name} {qty}주")
            
            result = order_with_retry(code, qty, "sell", max_retries=2)
            
            if result['success']:
                logger.info(f"✅ 긴급 매도 완료: {name}")
                with order_lock:
                    if code in holdings_data:
                        del holdings_data[code]
            else:
                logger.error(f"❌ 긴급 매도 실패: {name}")
        
        except Exception as e:
            logger.error(f"❌ 긴급 매도 오류 ({code}): {e}")
    
    send_discord_message("**🚨 긴급 청산 완료**")
