#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
웹소켓 클라이언트
"""

import websocket
import json
import time
import threading
import ssl
import traceback

from config import (
    MAX_RECONNECT, RECONNECT_BASE_BACKOFF, RECONNECT_MAX_BACKOFF, MIN_STRENGTH
)
from utils import logger, send_discord_message
from data_store import RealtimeDataStore
from surge_validator import SurgeValidator

# 전역 surge_validator
surge_validator = SurgeValidator()

class KISWebSocket:
    """한국투자증권 웹소켓 클라이언트"""
    
    def __init__(self, ws_url: str, approval_key: str, data_store: RealtimeDataStore,
                 get_approval_key_func=None):
        self.ws_url = ws_url
        self.approval_key = approval_key
        self.data_store = data_store
        self.get_approval_key_func = get_approval_key_func
        self.ws = None
        self.subscriptions = set()
        self.running = False
        self.connected = False
        self.reconnect_count = 0
        self.lock = threading.Lock()
        self.subscribed_items = {}
        self._reconnecting = False
    
    def connect(self) -> bool:
        """웹소켓 연결"""
        try:
            # 재연결 시 새로운 접속키 발급
            if self.reconnect_count > 0 and self.get_approval_key_func:
                logger.info("🔄 재연결을 위한 새 접속키 발급 중...")
                
                for key_attempt in range(3):
                    new_approval_key = self.get_approval_key_func()
                    
                    if new_approval_key:
                        self.approval_key = new_approval_key
                        logger.info("✅ 새 접속키 발급 완료")
                        logger.info("⏳ 접속키 활성화 대기 중 (5초)...")
                        time.sleep(5)
                        break
                    elif key_attempt < 2:
                        wait_time = (key_attempt + 1) * 30
                        logger.warning(f"⚠️  접속키 재발급 실패, {wait_time}초 후 재시도...")
                        time.sleep(wait_time)
                else:
                    logger.error("❌ 접속키 재발급 최종 실패 - 기존 키로 시도")
            
            logger.info("🔌 웹소켓 연결 시도...")
            logger.info(f"📍 URL: {self.ws_url}")
            logger.info(f"🔑 접속키: {self.approval_key[:20]}...")
            
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            
            self.running = True
            ws_thread = threading.Thread(target=self._run, daemon=True)
            ws_thread.start()
            
            # 연결 대기
            for i in range(20):
                time.sleep(0.5)
                if self.connected:
                    logger.info("✅ 웹소켓 연결 성공!")
                    self.reconnect_count = 0
                    return True
                
                if i % 4 == 0:
                    logger.debug(f"⏳ 연결 대기 중... ({i*0.5:.1f}초)")
            
            logger.error("❌ 웹소켓 연결 타임아웃 (10초 경과)")
            return False
        
        except Exception as e:
            logger.error(f"❌ 웹소켓 연결 오류: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def _run(self):
        """웹소켓 실행"""
        try:
            self.ws.run_forever(
                ping_interval=30,
                ping_timeout=10,
                sslopt={"cert_reqs": ssl.CERT_NONE}
            )
        except Exception as e:
            logger.error(f"❌ 웹소켓 실행 오류: {e}")
        finally:
            self.connected = False
            self._attempt_reconnect()
    
    def _attempt_reconnect(self):
        """재연결 시도"""
        with self.lock:
            if self._reconnecting:
                logger.debug("이미 재연결 진행 중...")
                return
            self._reconnecting = True
        
        try:
            if not self.running or self.reconnect_count >= MAX_RECONNECT:
                logger.error(f"❌ 재연결 포기 (시도: {self.reconnect_count}/{MAX_RECONNECT})")
                send_discord_message(f"⚠️ 웹소켓 재연결 실패 - 수동 재시작 필요")
                return
            
            self.reconnect_count += 1
            
            backoff_time = min(
                RECONNECT_BASE_BACKOFF * (2 ** (self.reconnect_count - 1)),
                RECONNECT_MAX_BACKOFF
            )
            
            logger.warning(f"🔄 {backoff_time}초 후 재연결 시도 ({self.reconnect_count}/{MAX_RECONNECT})...")
            send_discord_message(f"🔄 웹소켓 재연결 시도 중... ({self.reconnect_count}/{MAX_RECONNECT})")
            time.sleep(backoff_time)
            
            if self.connect():
                logger.info("✅ 재연결 성공!")
                send_discord_message("✅ 웹소켓 재연결 성공")
                self._restore_subscriptions()
            else:
                logger.error(f"❌ 재연결 실패 (시도: {self.reconnect_count}/{MAX_RECONNECT})")
        finally:
            with self.lock:
                self._reconnecting = False
    
    def _restore_subscriptions(self):
        """구독 복원"""
        logger.info(f"🔄 {len(self.subscribed_items)}개 종목 재구독 중...")
        
        count = 0
        for code, tr_ids in self.subscribed_items.items():
            for tr_id in tr_ids:
                self.subscribe(code, tr_id)
                time.sleep(0.05)
                count += 1
        
        logger.info(f"✅ {count}개 구독 복원 완료")
    
    def on_open(self, ws):
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("✅ 웹소켓 연결 성공!")
        logger.info(f"   URL: {self.ws_url}")
        logger.info(f"   접속키: {self.approval_key[:20]}...")
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        self.connected = True
    
    def on_message(self, ws, message):
        try:
            if not message.startswith('0|') and not message.startswith('1|'):
                return
            
            parts = message.split('|')
            if len(parts) < 4:
                return
            
            tr_id = parts[1]
            data_str = parts[3]
            
            # 체결 데이터
            if tr_id == "H0STCNT0":
                try:
                    data = json.loads(data_str) if data_str.startswith('{') else {}
                    code = data.get('MKSC_SHRN_ISCD', '')
                    
                    if code:
                        self.data_store.update_contract(code, data)
                        strength = float(data.get('CNTG_YN', 0))
                        price = int(data.get('STCK_PRPR', 0))
                        
                        if strength >= MIN_STRENGTH and price > 0:
                            surge_validator.add_detection(code, strength, price)
                            self.data_store.trigger_surge_check(code)
                except Exception as e:
                    logger.debug(f"체결 데이터 파싱 오류: {e}")
            
            # 호가 데이터
            elif tr_id == "H0STASP0":
                try:
                    data = json.loads(data_str) if data_str.startswith('{') else {}
                    code = data.get('MKSC_SHRN_ISCD', '')
                    price = int(data.get('STCK_PRPR', 0))
                    
                    if code and price > 0:
                        self.data_store.update_price(code, price)
                except Exception as e:
                    logger.debug(f"호가 데이터 파싱 오류: {e}")
        
        except Exception as e:
            logger.debug(f"메시지 처리 오류: {e}")
    
    def on_error(self, ws, error):
        logger.error(f"⚠️  웹소켓 에러: {error}")
        self.connected = False
        if self.running:
            threading.Thread(target=self._attempt_reconnect, daemon=True).start()
    
    def on_close(self, ws, close_status_code, close_msg):
        logger.warning(f"⚠️  웹소켓 종료! (코드: {close_status_code}, 메시지: {close_msg})")
        self.connected = False
        if self.running and close_status_code != 1000:
            threading.Thread(target=self._attempt_reconnect, daemon=True).start()
    
    def subscribe(self, code: str, tr_id: str) -> bool:
        """종목 구독"""
        try:
            if not self.connected:
                return False
            
            if code not in self.subscribed_items:
                self.subscribed_items[code] = set()
            self.subscribed_items[code].add(tr_id)
            
            message = json.dumps({
                "header": {
                    "approval_key": self.approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8"
                },
                "body": {
                    "input": {
                        "tr_id": tr_id,
                        "tr_key": code
                    }
                }
            })
            
            self.ws.send(message)
            
            with self.lock:
                self.subscriptions.add(f"{code}:{tr_id}")
            
            return True
        
        except Exception as e:
            logger.error(f"❌ 구독 오류: {e}")
            return False
    
    def disconnect(self):
        """연결 종료"""
        self.running = False
        self.connected = False
        if self.ws:
            self.ws.close()
