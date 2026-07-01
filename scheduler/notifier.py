# -*- coding: utf-8 -*-
"""
飞书通知器

通过飞书 API 发送流水线运行摘要和异常告警。

支持两种认证方式（按优先级）：
  1. App ID + App Secret → 获取 tenant_access_token，通过 API 发送消息
  2. Webhook URL + 签名密钥 → 直接 POST 消息卡片（兼容旧方案）

激活条件: config.yaml 中 feishu 配置非空占位符时自动启用
通知模式: 默认发送运行摘要；支持 alert_only 模式（仅在异常时通知）
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import requests

from scheduler.models import PipelineResult

logger = logging.getLogger(__name__)

# 飞书 API 端点
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
FEISHU_CHAT_LIST_URL = "https://open.feishu.cn/open-apis/im/v1/chats"


class FeishuNotifier:
    """
    飞书消息通知器

    App ID + App Secret 方式:
      1. 调用 tenant_access_token 接口获取 token
      2. 使用 token 调用消息发送 API

    Webhook 方式（兜底）:
      1. 使用 webhook_url + 签名密钥直接 POST

    属性:
        app_id: 飞书应用 App ID
        app_secret: 飞书应用 App Secret
        webhook_url: 飞书机器人 webhook 地址（可选）
        signing_secret: webhook 签名密钥（可选）
        chat_id: 目标群聊 ID（API 方式必填）
        enabled: 是否启用通知（配置非空时自动启用）
        alert_only: 仅异常时通知模式
        _token: 缓存的 tenant_access_token
        _token_expire: token 过期时间戳
    """

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        webhook_url: Optional[str] = None,
        signing_secret: Optional[str] = None,
        chat_id: Optional[str] = None,
        alert_only: bool = False,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.webhook_url = webhook_url
        self.signing_secret = signing_secret
        self.chat_id = chat_id
        self.alert_only = alert_only

        # 缓存 token
        self._token: Optional[str] = None
        self._token_expire: float = 0.0

        # 判断是否可用: App ID 方式或 webhook 方式任一配置即可
        self.enabled = self._has_valid_api_config() or self._has_valid_webhook_config()
        if self.enabled:
            # 记录已启用的方式
            mode = "API" if self._has_valid_api_config() else "webhook"
            logger.info("飞书通知器已启用 (模式: %s)", mode)

    # ---- 配置校验 ----

    def _has_valid_api_config(self) -> bool:
        """检查 App ID + Secret + chat_id 是否有效配置"""
        if not self.app_id or not self.app_secret:
            return False
        # 排除占位符
        for val in (self.app_id, self.app_secret):
            if any(kw in val for kw in ("YOUR_", "your_", "PLACEHOLDER", "placeholder", "TODO")):
                return False
        return True

    def _has_valid_webhook_config(self) -> bool:
        """检查 webhook URL 是否为有效配置"""
        if not self.webhook_url:
            return False
        for kw in ("YOUR_", "your_", "PLACEHOLDER", "placeholder", "TODO"):
            if kw in self.webhook_url:
                return False
        return True

    # ---- Token 管理 (App ID + Secret 方式) ----

    def _get_tenant_access_token(self) -> Optional[str]:
        """
        获取 tenant_access_token，自动处理缓存

        飞书 token 有效期 2 小时，缓存到过期前 5 分钟刷新。

        返回:
            token 字符串，获取失败返回 None
        """
        # 缓存的 token 还有 5 分钟以上有效期，直接返回
        if self._token and time.time() < self._token_expire - 300:
            return self._token

        if not self._has_valid_api_config():
            logger.warning("飞书 App 配置无效，无法获取 token")
            return None

        try:
            # 请求 tenant_access_token
            resp = requests.post(
                FEISHU_TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            data = resp.json()
            # 飞书 API 返回 code=0 表示成功
            if data.get("code") != 0:
                logger.error("获取飞书 token 失败: code=%s, msg=%s",
                             data.get("code"), data.get("msg"))
                return None

            self._token = data["tenant_access_token"]
            # expire 字段单位是秒，转为时间戳
            self._token_expire = time.time() + data.get("expire", 7200)
            logger.info("飞书 tenant_access_token 获取成功")
            return self._token

        except requests.RequestException as e:
            logger.error("获取飞书 token 网络异常: %s", e)
            return None

    # ---- 消息发送 ----

    def send_summary(self, pipeline_result: PipelineResult) -> bool:
        """
        发送流水线运行摘要（Markdown 卡片）

        仅在 enabled=True 且 alert_only=False 时发送。

        参数:
            pipeline_result: 流水线执行结果

        返回:
            发送成功返回 True，失败或被跳过返回 False
        """
        if not self.enabled:
            logger.debug("飞书通知未启用，跳过摘要发送")
            return False
        if self.alert_only and pipeline_result.status == "success":
            logger.debug("alert_only 模式，流水线正常，跳过摘要")
            return False

        # 生成消息内容
        title = "KHunter 定时流水线运行报告"
        content = self._build_summary_markdown(pipeline_result)

        return self._send(title, content)

    def send_alert(self, pipeline_result: PipelineResult) -> bool:
        """
        发送异常告警（Markdown 卡片）

        仅在 enabled=True 且状态非 success 时发送。

        参数:
            pipeline_result: 流水线执行结果

        返回:
            发送成功返回 True，失败或被跳过返回 False
        """
        if not self.enabled:
            logger.debug("飞书通知未启用，跳过告警发送")
            return False

        # 仅异常时告警
        if pipeline_result.status == "success":
            return False

        # 生成告警消息
        title = "KHunter 定时流水线异常告警"
        content = self._build_alert_markdown(pipeline_result)

        return self._send(title, content)

    # ---- 消息构建 ----

    def _build_summary_markdown(self, result: PipelineResult) -> str:
        """
        构建运行摘要 Markdown 内容

        格式参考设计文档 7.2.2 节
        """
        # 获取当日日期
        from datetime import date
        today = date.today()
        weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_map[today.weekday()]

        # 格式化耗时
        total_dur = self._format_duration(result.duration_seconds)
        start_str = result.start_time.strftime("%H:%M:%S") if result.start_time else "N/A"

        # 构建 Markdown 内容
        lines = [
            f"**日期**: {today} (周{weekday})",
            f"**开始时间**: {start_str} | **总耗时**: {total_dur}",
            "",
        ]

        # 各步骤详情
        for step in result.steps:
            step_dur = self._format_duration(step.duration_seconds)
            emoji = "✅" if step.status == "success" else ("❌" if step.status == "failed" else "⚠️")
            lines.append(f"### {step.step_name} {emoji} (耗时: {step_dur})")

            # 根据步骤名展示详情
            if step.step_name == "data_update" and step.details:
                lines.append(self._format_data_update_details(step.details))
            elif step.step_name == "strategy_run" and step.details:
                lines.append(self._format_strategy_run_details(step.details))
            elif step.step_name == "notification" and step.details:
                lines.append(self._format_notification_details(step.details))

            if step.error:
                lines.append(f"**错误**: {step.error}")
            lines.append("")

        # 整体状态
        status_emoji = "✅" if result.status == "success" else ("⚠️" if result.status == "partial_failure" else "❌")
        lines.append(f"**整体状态**: {status_emoji} {result.status}")

        return "\n".join(lines)

    def _build_alert_markdown(self, result: PipelineResult) -> str:
        """
        构建异常告警 Markdown 内容

        格式参考设计文档 7.2.3 节
        """
        from datetime import date
        today = date.today()

        lines = [
            f"**日期**: {today} | **状态**: {result.status}",
            "",
        ]

        # 各步骤状态
        for step in result.steps:
            emoji = "✅" if step.status == "success" else ("❌" if step.status == "failed" else "⚠️")
            err_info = f" - 错误: {step.error}" if step.error else ""
            lines.append(f"### {step.step_name} {emoji}{err_info}")

        return "\n".join(lines)

    def _format_duration(self, seconds: float) -> str:
        """将秒数格式化为人类可读的耗时字符串"""
        if seconds < 60:
            return f"{seconds:.0f}秒"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}分{secs}秒"

    def _format_data_update_details(self, details: dict) -> str:
        """格式化数据更新步骤详情"""
        parts = []
        if details.get("kline_updated"):
            parts.append(f"- K线增量更新: {details['kline_updated']} 只")
        if details.get("exdividend_rebuilt"):
            parts.append(f"- 除权历史重建: {details['exdividend_rebuilt']} 只")
        if details.get("new_stocks_initialized"):
            parts.append(f"- 新股初始化: {details['new_stocks_initialized']} 只")
        if details.get("basic_data_synced", False):
            parts.append("- 基础数据同步: 完成")
        return "\n".join(parts) if parts else "- 无详情"

    def _format_strategy_run_details(self, details: dict) -> str:
        """格式化策略运行步骤详情"""
        parts = []
        if details.get("buy_signals") is not None:
            parts.append(f"- 买入信号: {details['buy_signals']} 条")
        if details.get("sell_signals") is not None:
            parts.append(f"- 卖出信号: {details['sell_signals']} 条")
        if details.get("signal_file"):
            parts.append(f"- 信号文件: {details['signal_file']}")
        return "\n".join(parts) if parts else "- 无详情"

    def _format_notification_details(self, details: dict) -> str:
        """格式化通知步骤详情"""
        parts = []
        if details.get("feishu_sent", False):
            parts.append("- 飞书通知: 已发送")
        return "\n".join(parts) if parts else "- 无详情"

    # ---- 发送核心逻辑 ----

    def _send(self, title: str, content: str) -> bool:
        """
        发送消息，优先使用 App API 方式，失败回退到 webhook

        参数:
            title: 卡片标题
            content: Markdown 内容

        返回:
            发送成功返回 True
        """
        # 优先尝试 App API 方式
        if self._has_valid_api_config():
            if self._send_via_api(title, content):
                return True
            logger.warning("App API 发送失败，尝试 webhook 兜底")

        # 回退到 webhook 方式
        if self._has_valid_webhook_config():
            return self._send_via_webhook(title, content)

        logger.error("飞书通知：无可用发送方式")
        return False

    def _send_via_api(self, title: str, content: str) -> bool:
        """
        通过飞书 API 发送消息

        1. 获取 tenant_access_token
        2. 构建 interactive 卡片
        3. POST 到消息发送接口

        需要 chat_id 配置。
        """
        if not self.chat_id:
            logger.error("飞书 API 方式缺少 chat_id 配置")
            return False

        # 获取 token
        token = self._get_tenant_access_token()
        if not token:
            return False

        # 构建卡片消息体
        card_body = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": content}
            ],
        }

        try:
            # 飞书 API 消息体格式: content 需要 JSON 字符串
            resp = requests.post(
                FEISHU_MSG_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": self.chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card_body),
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error("飞书消息发送失败: code=%s, msg=%s",
                             data.get("code"), data.get("msg"))
                return False

            logger.info("飞书消息发送成功 (API) - message_id=%s", data.get("data", {}).get("message_id"))
            return True

        except requests.RequestException as e:
            logger.error("飞书 API 发送网络异常: %s", e)
            return False
        except (ValueError, KeyError) as e:
            logger.error("飞书 API 响应解析异常: %s", e)
            return False

    def _send_via_webhook(self, title: str, content: str) -> bool:
        """
        通过飞书 webhook 发送消息（兜底方案）

        使用 HMAC-SHA256 签名机制:
          1. 计算 timestamp
          2. sign = base64(hmac_sha256(secret, timestamp + '\n' + secret))
          3. POST 到 webhook_url
        """
        if not self.webhook_url:
            return False

        # 计算签名
        timestamp = str(int(time.time()))
        sign = ""
        if self.signing_secret:
            string_to_sign = f"{timestamp}\n{self.signing_secret}"
            sign = base64.b64encode(
                hmac.new(
                    self.signing_secret.encode(),
                    string_to_sign.encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()

        # 构建卡片消息体
        body = {
            "timestamp": timestamp,
            "sign": sign,
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": content}
                ],
            },
        }

        try:
            resp = requests.post(self.webhook_url, json=body, timeout=10)
            data = resp.json()
            # webhook 成功返回 {"code": 0}
            if resp.status_code != 200 or data.get("code") != 0:
                logger.error("飞书 webhook 发送失败: http=%s, code=%s, msg=%s",
                             resp.status_code, data.get("code"), data.get("msg"))
                return False

            logger.info("飞书消息发送成功 (webhook)")
            return True

        except requests.RequestException as e:
            logger.error("飞书 webhook 发送网络异常: %s", e)
            return False
        except (ValueError, KeyError) as e:
            logger.error("飞书 webhook 响应解析异常: %s", e)
            return False

    # ---- 辅助方法 ----

    def get_chat_id(self) -> Optional[str]:
        """
        获取 bot 所在的群聊列表，返回第一个群聊 ID

        用于自动发现 chat_id，需要 im:chat 权限。

        返回:
            第一个群聊的 chat_id，失败返回 None
        """
        token = self._get_tenant_access_token()
        if not token:
            return None

        try:
            resp = requests.get(
                FEISHU_CHAT_LIST_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={"page_size": 10},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error("获取群聊列表失败: code=%s, msg=%s",
                             data.get("code"), data.get("msg"))
                return None

            items = data.get("data", {}).get("items", [])
            if items:
                chat_id = items[0]["chat_id"]
                logger.info("发现群聊: name=%s, chat_id=%s",
                            items[0].get("name", "未知"), chat_id)
                return chat_id

            logger.warning("未发现任何群聊，请确认 bot 已添加到群中")
            return None

        except requests.RequestException as e:
            logger.error("获取群聊列表网络异常: %s", e)
            return None

    @classmethod
    def from_config(cls, config: dict) -> "FeishuNotifier":
        """
        从配置字典创建 FeishuNotifier 实例

        参数:
            config: 完整配置字典 (config.yaml 加载结果)

        返回:
            FeishuNotifier 实例
        """
        feishu = config.get("feishu", {})

        return cls(
            app_id=feishu.get("app_id"),
            app_secret=feishu.get("app_secret"),
            webhook_url=feishu.get("webhook_url"),
            signing_secret=feishu.get("signing_secret", feishu.get("secret")),
            chat_id=feishu.get("chat_id"),
            alert_only=feishu.get("alert_only", False),
        )
