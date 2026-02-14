#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
트레이딩 봇 메인 실행 파일
"""

import time
import threading
from datetime import datetime

import config
from utils import logger, send_discord_message
from api_client import (
    get_token, get_approval_key, get_balance, get_holdings, 
    get_price, get_volume_rank_codes
)
from order_manager import OrderManager
from surge_validator import SurgeValidator
from data_store import RealtimeDataStore
from websocket_client import KISWebSocket
from trading_logic import (
    execute_buy_on_surge, check_sell_positions, emergency_liquidate_all,
    order_manager, surge_validator, daily_stats
)

def main():
    """메인 함수"""
    try:
        logger.info("\n" + "="*60)
        logger.info("🚀 트레이딩 봇 v6.2.7 시작")
        logger.info("="*60)
        
        # STEP 1: 환경변수 검증
        logger.info("\nSTEP 1: 환경변수 검증")
        config.validate_environment()
        logger.info("✅ 환경변수 검증 완료")
        
        # STEP 2: OrderManager 시작
        logger.info("\nSTEP 2: OrderManager 시작")
        order_manager.start()
        
        # STEP 3: 토큰 발급
        logger.info("\nSTEP 3: 토큰 발급")
        token = get_token()
        if not token:
            logger.critical("❌ 토큰 발급 실패")
            return
        logger.info("✅ 토큰 발급 성공")
        
        # STEP 4: 웹소켓 접속키 발급
        logger.info("\nSTEP 4: 웹소켓 접속키 발급")
        
        approval_key = None
        max_approval_retries = 3
        approval_retry_count = 0
        
        while not approval_key and approval_retry_count < max_approval_retries:
            approval_key = get_approval_key()
            
            if not approval_key:
                approval_retry_count += 1
                if approval_retry_count < max_approval_retries:
                    wait_time = 60
                    logger.warning(f"⚠️  접속키 발급 실패, {wait_time}초 후 재시도 ({approval_retry_count}/{max_approval_retries})...")
                    send_discord_message(f"⚠️ 접속키 발급 실패 - {wait_time}초 후 재시도...")
                    time.sleep(wait_time)
        
        if not approval_key:
            logger.critical("❌ 접속키 발급 최종 실패")
            send_discord_message("❌ 접속키 발급 실패 - 한투 고객센터 문의 필요")
            return
        
        # 접속키 활성화 대기
        logger.info("⏳ 접속키 활성화 대기 중 (5초)...")
        time.sleep(5)
        logger.info("✅ 접속키 활성화 완료!")
        
        # STEP 5: 계좌 정보
        logger.info("\nSTEP 5: 계좌 정보 조회")
        cash = get_balance()
        holdings = get_holdings()
        
        total_value = cash
        for h in holdings:
            price = get_price(h['code'])
            if price > 0:
                total_value += price * h['qty']
        
        daily_stats['start_balance'] = total_value
        daily_stats['current_balance'] = total_value
        
        logger.info(f"💰 현금: {cash:,}원 | 보유: {len(holdings)}개 | 총자산: {total_value:,}원")
        
        # STEP 6: 데이터 저장소
        logger.info("\nSTEP 6: 데이터 저장소 초기화")
        data_store = RealtimeDataStore()
        data_store.register_surge_callback(execute_buy_on_surge)
        
        # STEP 7: 웹소켓 연결
        logger.info("\nSTEP 7: 웹소켓 연결")
        ws_client = KISWebSocket(
            config.WS_URL, 
            approval_key, 
            data_store,
            get_approval_key_func=get_approval_key
        )
        
        # 첫 연결 실패 시 재시도
        max_initial_attempts = 3
        for attempt in range(1, max_initial_attempts + 1):
            logger.info(f"🔌 웹소켓 연결 시도 {attempt}/{max_initial_attempts}")
            if ws_client.connect():
                break
            elif attempt < max_initial_attempts:
                wait_time = 5 * attempt
                logger.warning(f"⚠️  연결 실패, {wait_time}초 후 재시도...")
                time.sleep(wait_time)
            else:
                logger.critical("❌ 웹소켓 초기 연결 실패 - 백그라운드 재연결 시작")
                send_discord_message("⚠️ 웹소켓 초기 연결 실패 - 백그라운드에서 재연결 시도 중...")
                threading.Thread(target=ws_client._attempt_reconnect, daemon=True).start()
        
        # STEP 8: 종목 사전 구독
        logger.info("\nSTEP 8: 종목 사전 구독")
        
        now = datetime.now()
        current_time = now.time()
        pre_market_time = datetime.strptime(config.PRE_MARKET_SUBSCRIBE_TIME, "%H:%M").time()
        
        if current_time < pre_market_time:
            wait_seconds = (datetime.combine(now.date(), pre_market_time) - 
                           datetime.combine(now.date(), current_time)).total_seconds()
            logger.info(f"⏰ 사전 구독 대기 중... ({int(wait_seconds/60)}분 {int(wait_seconds%60)}초)")
            time.sleep(wait_seconds)
        
        watch_codes = get_volume_rank_codes(50)
        
        if not watch_codes:
            logger.critical("❌ 모니터링 종목 없음")
            ws_client.disconnect()
            return
        
        logger.info(f"🔍 {len(watch_codes)}개 종목 사전 구독 중...")
        
        for i, code in enumerate(watch_codes):
            ws_client.subscribe(code, "H0STCNT0")
            ws_client.subscribe(code, "H0STASP0")
            time.sleep(0.05)
            if (i + 1) % 10 == 0:
                logger.info(f"  진행: {i+1}/{len(watch_codes)}")
        
        logger.info(f"\n✅ 사전 구독 완료")
        
        send_discord_message(
            f"**🚀 자동매매 v6.2.7 시작**\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 현금: ₩{cash:,}\n"
            f"📊 보유: {len(holdings)}개\n"
            f"🔍 구독: {len(watch_codes)}개\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⏰ 매수: {config.TRADING_START_TIME} ~\n"
            f"🚫 VI 회피 ON\n"
            f"💵 수수료 계산 ON"
        )
        
        # STEP 9: 메인 루프
        logger.info("\nSTEP 9: 메인 루프 시작")
        logger.info("="*60)
        
        last_sell_check = time.time()
        last_cleanup = time.time()
        last_rotation = time.time()
        last_sync = time.time()
        last_ws_check = time.time()
        
        ROTATION_INTERVAL = 1800  # 30분
        SYNC_INTERVAL = 300  # 5분
        WS_CHECK_INTERVAL = 30  # 30초
        
        trading_enabled = True
        emergency_stop = False
        
        while True:
            now = datetime.now()
            
            # 주말 체크
            if now.weekday() >= 5:
                time.sleep(60)
                continue
            
            current_time = now.time()
            market_open = datetime.strptime(config.MARKET_OPEN_TIME, "%H:%M").time()
            market_close = datetime.strptime(config.MARKET_CLOSE_TIME, "%H:%M").time()
            trading_start = datetime.strptime(config.TRADING_START_TIME, "%H:%M").time()
            emergency_close = datetime.strptime(config.EMERGENCY_CLOSE_TIME, "%H:%M").time()
            
            # 긴급 청산 시간
            if emergency_close <= current_time < market_close and not emergency_stop:
                emergency_liquidate_all()
            
            # 장 시간 체크
            if current_time < market_open:
                trading_enabled = False
                time.sleep(10)
                continue
            elif market_open <= current_time < trading_start:
                trading_enabled = False
                if int(now.second) == 0:
                    logger.info(f"⏰ VI 회피 구간 ({current_time.strftime('%H:%M:%S')})")
                time.sleep(1)
                continue
            elif trading_start <= current_time <= market_close:
                if not trading_enabled:
                    logger.info("✅ 매매 시작!")
                    send_discord_message("**✅ 매매 시작**\n장 초반 VI 회피 완료")
                    emergency_stop = False
                trading_enabled = True
            else:
                trading_enabled = False
                time.sleep(60)
                continue
            
            # 매도 체크 (1초마다)
            if time.time() - last_sell_check >= 1:
                check_sell_positions(data_store)
                last_sell_check = time.time()
            
            # 데이터 정리
            if time.time() - last_cleanup >= 60:
                surge_validator.clear_old_data()
                last_cleanup = time.time()
            
            # 보유 종목 동기화 (5분마다)
            if time.time() - last_sync >= SYNC_INTERVAL:
                get_holdings()
                last_sync = time.time()
            
            # 종목 교체 (30분마다)
            if time.time() - last_rotation >= ROTATION_INTERVAL:
                logger.info("🔄 종목 교체 중...")
                new_codes = get_volume_rank_codes(50)
                
                if new_codes:
                    for i, code in enumerate(new_codes):
                        ws_client.subscribe(code, "H0STCNT0")
                        ws_client.subscribe(code, "H0STASP0")
                        time.sleep(0.05)
                    
                    logger.info(f"✅ {len(new_codes)}개 종목 교체 완료")
                
                last_rotation = time.time()
            
            # 웹소켓 상태 체크
            if time.time() - last_ws_check >= WS_CHECK_INTERVAL:
                if not ws_client.connected:
                    logger.warning("⚠️  웹소켓 연결 끊김 감지 - 재연결 시도")
                    send_discord_message("⚠️ 웹소켓 재연결 시도 중...")
                    threading.Thread(target=ws_client._attempt_reconnect, daemon=True).start()
                else:
                    logger.debug(f"✅ 웹소켓 정상 (구독: {len(ws_client.subscribed_items)}개 종목)")
                last_ws_check = time.time()
            
            time.sleep(0.5)
        
    except KeyboardInterrupt:
        logger.info("\n⏹️  사용자 종료 요청...")
        ws_client.disconnect()
        
        # 통계 출력
        logger.info("\n" + "="*60)
        logger.info("📊 일일 매매 통계")
        logger.info("="*60)
        logger.info(f"시작 자산: {daily_stats['start_balance']:,}원")
        logger.info(f"매매 횟수: {daily_stats['trades_count']}회")
        logger.info(f"손익: {daily_stats['profit_loss']:+,}원")
        
        send_discord_message(
            "**⏹️ 자동매매 종료**\n"
            f"매매: {daily_stats['trades_count']}회\n"
            f"손익: ₩{daily_stats['profit_loss']:+,}"
        )
    
    except Exception as e:
        logger.critical(f"❌ 치명적 오류: {e}")
        import traceback
        logger.critical(traceback.format_exc())
        
        try:
            ws_client.disconnect()
        except:
            pass
        
        send_discord_message(f"**❌ 시스템 오류**\n{str(e)[:100]}")
    
    finally:
        order_manager.stop()
        logger.info("프로그램 종료")

if __name__ == "__main__":
    main()
