# -*- coding: utf-8 -*-
"""
?????????????

???? API ??????????????????? KHunter ???
????? IP?????? URL?????????

?????????????
  - im:message??? + ?????
  - ??????????

????:
  ???? / pipeline     -> ???????
  ???? / data          -> ???????
  ??? / strategy        -> ???????
  ?? / status            -> ????????
  ?? / help              -> ??????
"""

import json as jmod
import logging
import threading
import time as time_module
from typing import Optional

import requests

from scheduler.models import PipelineResult, StepResult

logger = logging.getLogger(__name__)

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_LIST_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
FEISHU_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"


class FeishuCommander:

    def __init__(self, config: dict):
        feishu = config.get("feishu", {})
        cmd_config = config.get("feishu_commander", {})
        self.enabled = cmd_config.get("enabled", False)
        self.app_id = feishu.get("app_id", "")
        self.app_secret = feishu.get("app_secret", "")
        self.chat_id = feishu.get("chat_id", "")
        self.poll_interval = cmd_config.get("poll_interval_seconds", 30)
        self.command_timeout = cmd_config.get("command_timeout_minutes", 15) * 60
        self.config = config
        self.trigger_prefix = cmd_config.get("trigger_prefix", "")
        self._token = None
        self._token_expire = 0.0
        self._last_message_id = None
        self._running = False
        logger.info("FeishuCommander: enabled=%s, chat_id=%s, poll=%ds",
                     self.enabled, self.chat_id, self.poll_interval)

    def _get_tenant_access_token(self) -> Optional[str]:
        if self._token and time_module.time() < self._token_expire - 300:
            return self._token
        try:
            resp = requests.post(
                FEISHU_TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error("Get token failed: code=%s, msg=%s", data.get("code"), data.get("msg"))
                return None
            self._token = data["tenant_access_token"]
            self._token_expire = time_module.time() + data.get("expire", 7200)
            return self._token
        except requests.RequestException as e:
            logger.error("Get token error: %s", e)
            return None

    def start_polling(self):
        if not self.enabled:
            logger.info("FeishuCommander not enabled")
            return
        if self._running:
            return
        self._running = True
        thread = threading.Thread(target=self._poll_loop, daemon=True, name="feishu-poller")
        thread.start()
        logger.info("Polling started (interval %ds)", self.poll_interval)

    def _poll_loop(self):
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.error("Poll error: %s", e)
            time_module.sleep(self.poll_interval)

    def _poll_once(self):
        token = self._get_tenant_access_token()
        if not token:
            return
        messages = self._list_messages(token)
        if not messages:
            return
        newest_user_id = None
        for msg in messages:
            msg_id = msg.get("message_id", "")
            msg_type = msg.get("msg_type", "")
            sender = msg.get("sender", {})
            sender_type = sender.get("sender_type", "")
            if sender_type == "app":
                continue
            if self._last_message_id and msg_id == self._last_message_id:
                break
            if msg_type != "text":
                continue
            text = self._extract_text_content(msg.get("body", {}).get("content", ""))
            if not text:
                continue
            command = self._parse_command(text)
            if not command:
                continue
            chat_id = msg.get("chat_id", "")
            logger.info("Command: %s (msg %s)", command, msg_id)
            self._execute_command(command, chat_id)
            if newest_user_id is None:
                newest_user_id = msg_id
        if newest_user_id:
            self._last_message_id = newest_user_id

    def _list_messages(self, token: str) -> list:
        try:
            resp = requests.get(
                FEISHU_LIST_MSG_URL,
                headers={"Authorization": "Bearer " + token},
                params={
                    "container_id_type": "chat",
                    "container_id": self.chat_id,
                    "page_size": 10,
                    "sort_type": "ByCreateTimeDesc",
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("List messages failed: code=%s, msg=%s", data.get("code"), data.get("msg"))
                return []
            return data.get("data", {}).get("items", [])
        except requests.RequestException as e:
            logger.error("List messages error: %s", e)
            return []

    @staticmethod
    def _extract_text_content(content_raw: str) -> str:
        try:
            content = jmod.loads(content_raw)
            return content.get("text", "").strip()
        except (jmod.JSONDecodeError, TypeError, AttributeError):
            return ""

    _COMMAND_MAP = {
        "pipeline": "pipeline",
        "data_update": "data_update",
        "data": "data_update",
        "strategy_run": "strategy_run",
        "strategy": "strategy_run",
        "status": "status",
        "help": "help",
    }

    _CHINESE_COMMAND_MAP = {
        "跑流水线": "pipeline",
        "流水线": "pipeline",
        "更新数据": "data_update",
        "更新": "data_update",
        "跑策略": "strategy_run",
        "策略": "strategy_run",
        "状态": "status",
        "帮助": "help",
        "指令": "help",
        "命令": "help",
    }

    def _parse_command(self, text: str) -> Optional[str]:
        t = text.strip()
        if self.trigger_prefix:
            if not t.startswith(self.trigger_prefix):
                return None
            t = t[len(self.trigger_prefix):].strip()
        # Check Chinese commands first
        if t in self._CHINESE_COMMAND_MAP:
            return self._CHINESE_COMMAND_MAP[t]
        for phrase, cmd in self._CHINESE_COMMAND_MAP.items():
            if t.startswith(phrase):
                return cmd
        # Fallback to English commands
        normalized = t.lower()
        if normalized in self._COMMAND_MAP:
            return self._COMMAND_MAP[normalized]
        for phrase, cmd in self._COMMAND_MAP.items():
            if normalized.startswith(phrase):
                return cmd
        return None

    def _execute_command(self, command: str, chat_id: str):
        logger.info("Exec: %s", command)
        try:
            if command == "pipeline":
                self._run_pipeline()
            elif command == "data_update":
                self._run_data_update()
            elif command == "strategy_run":
                self._run_strategy()
            elif command == "status":
                self._run_status()
            elif command == "help":
                self._send_help(chat_id)
        except Exception as e:
            logger.error("Exec failed: %s - %s", command, e)
            self._send_text(chat_id, "Execution failed: " + str(e))

    def _run_pipeline(self):
        from scheduler.pipeline_orchestrator import PipelineOrchestrator
        orchestrator = PipelineOrchestrator(self.config)
        logger.info("Pipeline start (feishu triggered)")
        orchestrator.run_pipeline()
        logger.info("Pipeline done")

    def _run_data_update(self):
        from utils.data_collection_service import get_data_collection_service
        service = get_data_collection_service(self.config.get("data_dir", "data"))
        start_result = service.start_update(update_types=None)
        if start_result.get("success"):
            self._send_text(None, "Data update started...")
            deadline = time_module.time() + self.command_timeout
            while time_module.time() < deadline:
                progress = service.get_update_progress()
                if not progress.get("running"):
                    status = progress.get("status", "unknown")
                    stats = progress.get("totalStats", {})
                    msg = "Update done: " + status + "\nKline: +" + str(stats.get("kline_added", 0)) + " upd " + str(stats.get("kline_updated", 0))
                    self._send_text(None, msg)
                    return
                time_module.sleep(5)
            self._send_text(None, "Update timeout")
        else:
            self._send_text(None, "Update failed: " + start_result.get("message", "?"))

    def _run_strategy(self):
        from utils.global_db import get_global_db
        db = get_global_db()
        data_dir = self.config.get("data_dir", "data")
        from trading.strategy_runner import StrategyRunner
        runner = StrategyRunner(db, data_dir, self.config)
        tasks = [{}]
        from pathlib import Path
        history_file = Path(data_dir) / "running" / "task_history.json"
        if history_file.exists():
            with open(history_file, "r", encoding="utf-8") as f:
                history = jmod.load(f)
            if history:
                latest = history[-1]
                strategies = latest.get("strategies", [])
                strategies = [s for s in strategies if s]
                timing = latest.get("timing_strategy", "support")
                if strategies:
                    # 每个策略拆分为独立 task（与 Web 端格式一致：selection_strategy + timing_strategy）
                    tasks = [{"selection_strategy": s, "timing_strategy": timing} for s in strategies]
        result = runner.run_strategies_batch(tasks=tasks, config={})
        status = "ok" if result and result.get("status") != "failed" else "fail"
        data = result.get("data", {}) if result else {}
        msg = "Strategy " + status + "\nBuy: " + str(data.get("buy_signals", 0)) + " Sell: " + str(data.get("sell_signals", 0))
        self._send_text(None, msg)

    def _run_status(self):
        from utils.global_db import get_global_db
        db = get_global_db()
        try:
            stock_count = db.query("SELECT COUNT(*) as c FROM stock_basic")[0]["c"]
        except Exception:
            stock_count = 0
        try:
            kline_count = db.query("SELECT COUNT(*) as c FROM stock_kline")[0]["c"]
        except Exception:
            kline_count = 0
        latest_kline_date = ""
        try:
            row = db.query("SELECT MAX(date) as d FROM stock_kline")
            if row and row[0]["d"]:
                latest_kline_date = row[0]["d"]
        except Exception:
            pass
        from trading.strategy_runner import StrategyRunner
        data_dir = self.config.get("data_dir", "data")
        runner = StrategyRunner(db, data_dir, self.config)
        working_date = runner.get_working_date()
        signals_file = runner.running_dir / ("signals_" + working_date + ".json")
        signals = runner._load_signals(str(signals_file)) if signals_file.exists() else []
        buy_count = sum(1 for s in signals if s.get("signal_type") == "buy")
        sell_count = sum(1 for s in signals if s.get("signal_type") == "sell")
        msg = "Stock count: " + str(stock_count) + "\nKline: " + str(kline_count) + " (latest " + latest_kline_date + ")\nDate: " + working_date + "\nSignals: buy " + str(buy_count) + " sell " + str(sell_count)
        self._send_text(None, msg)

    def _send_help(self, chat_id: str):
        msg = ("Commands:\n"
               "pipeline - run full pipeline\n"
               "data_update - update data only\n"
               "strategy_run - run strategy only\n"
               "status - system status\n"
               "help - this help")
        self._send_text(chat_id, msg)

    def _send_text(self, chat_id, text):
        target = chat_id or self.chat_id
        if not target:
            logger.warning("No chat_id")
            return
        token = self._get_tenant_access_token()
        if not token:
            return
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "KHunter"}, "template": "blue"},
            "elements": [{"tag": "markdown", "content": text}],
        }
        try:
            resp = requests.post(
                FEISHU_MSG_URL,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                json={"receive_id": target, "msg_type": "interactive", "content": jmod.dumps(card)},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error("Send failed: code=%s, msg=%s", data.get("code"), data.get("msg"))
        except requests.RequestException as e:
            logger.error("Send error: %s", e)

    def handle_callback(self, body: dict) -> dict:
        event_type = body.get("type", "")
        if event_type == "url_verification":
            challenge = body.get("challenge", "")
            return {"challenge": challenge}
        challenge = body.get("challenge")
        if challenge:
            return {"challenge": challenge}
        if event_type == "event_callback":
            event = body.get("event", {})
            if event.get("type") == "im.message.receive_v1":
                threading.Thread(target=self._handle_callback_message, args=(event,), daemon=True).start()
        return {}

    def _handle_callback_message(self, event: dict):
        try:
            if not self.enabled:
                return
            message = event.get("message", {})
            chat_id = message.get("chat_id", "")
            if message.get("message_type") != "text":
                return
            text = self._extract_text_content(message.get("content", "{}"))
            if not text:
                return
            command = self._parse_command(text)
            if not command:
                return
            logger.info("Callback cmd: %s", command)
            self._execute_command(command, chat_id)
        except Exception as e:
            logger.error("Callback error: %s", e)


def get_commander(config: dict = None):
    if not hasattr(get_commander, "_instance"):
        if config is None:
            import os, yaml
            cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        get_commander._instance = FeishuCommander(config)
    return get_commander._instance
