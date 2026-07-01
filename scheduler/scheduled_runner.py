# -*- coding: utf-8 -*-
"""
定时调度入口

提供两种运行模式：
  - schedule: 守护进程持续运行，按配置时间每日触发
  - once: 执行一次后退出，配合外部定时器（如 Windows 任务计划）
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from scheduler.models import PipelineResult
from scheduler.pipeline_orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)

# 任务历史文件路径
TASK_HISTORY_FILE = "data/running/task_history.json"


class ScheduledRunner:
    """
    定时调度入口

    模式:
        schedule - 守护进程模式，使用 schedule 库循环
        once     - 执行一次，配合 Windows 任务计划/cron

    属性:
        config: 完整配置字典
        orchestrator: 流水线编排器实例
    """

    def __init__(self, config_file: str = "config/config.yaml"):
        """
        初始化调度入口

        参数:
            config_file: 配置 YAML 文件路径
        """
        self.config_file = config_file
        self.config = self._load_config(config_file)
        self.orchestrator = PipelineOrchestrator(self.config)

    def _load_config(self, config_file: str) -> dict:
        """加载配置文件"""
        config_path = Path(config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_file}")
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}

    # ---- 一次执行模式 ----

    def run_once(self) -> PipelineResult:
        """
        执行一次完整流水线

        用于配合外部定时器（Windows 任务计划 / cron）使用：
            python main.py schedule --once

        返回:
            PipelineResult: 流水线执行结果
        """
        # 检查是否启用
        ps_config = self.config.get("pipeline_schedule", {})
        if not ps_config.get("enabled", True):
            logger.info("定时流水线未启用 (pipeline_schedule.enabled=false)")
            result = PipelineResult()
            result.status = "skipped"
            result.summary = "流水线功能未启用"
            return result

        logger.info("======== 定时流水线开始 ========")
        logger.info("时间: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # 执行流水线
        result = self.orchestrator.run_pipeline()

        # 记录历史
        self._save_task_history(result)

        # 输出摘要
        self._print_summary(result)

        logger.info("======== 定时流水线结束 ========")
        return result

    # ---- 守护进程调度模式 ----

    def start_schedule(self):
        """
        启动守护进程调度模式

        每天按配置时间自动触发流水线。
        使用 schedule 库实现，按配置 pipeline_schedule.time 执行。
        """
        try:
            import schedule  # noqa: F401
        except ImportError:
            logger.error("schedule 库未安装，请执行: pip install schedule")
            sys.exit(1)

        ps_config = self.config.get("pipeline_schedule", {})
        if not ps_config.get("enabled", True):
            logger.warning("定时流水线未启用 (pipeline_schedule.enabled=false)，退出")
            return

        schedule_time = ps_config.get("time", "17:00")
        logger.info("=" * 50)
        logger.info("定时流水线调度器已启动 (守护进程模式)")
        logger.info("执行时间: 每日 %s (北京时间)", schedule_time)
        logger.info("交易日检测: %s", ps_config.get("detect_trading_day", True))
        logger.info("=" * 50)

        import schedule
        schedule.every().day.at(schedule_time).do(self._scheduled_run)

        try:
            while True:
                schedule.run_pending()
                time.sleep(30)
        except KeyboardInterrupt:
            logger.info("收到停止信号，调度器正常退出")

    def _scheduled_run(self):
        """定时触发的执行方法（供 schedule 库回调）"""
        logger.info("调度时间到达，触发流水线执行")
        try:
            self.run_once()
        except Exception as e:
            logger.error("定时流水线异常: %s", e, exc_info=True)

    # ---- 历史记录 ----

    def _save_task_history(self, result: PipelineResult):
        """
        保存任务执行历史到 JSON 文件

        仅保留最近 30 条记录，防止文件过大。

        参数:
            result: 流水线执行结果
        """
        history_path = Path(TASK_HISTORY_FILE)
        history_path.parent.mkdir(parents=True, exist_ok=True)

        # 读取现有历史
        history = []
        if history_path.exists():
            try:
                with open(history_path, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except (json.JSONDecodeError, IOError):
                history = []

        # 追加新记录
        record = {
            "time": datetime.now().isoformat(),
            "pipeline_id": result.pipeline_id,
            "status": result.status,
            "duration_seconds": result.duration_seconds,
            "steps": [
                {
                    "step_name": s.step_name,
                    "status": s.status,
                    "duration_seconds": s.duration_seconds,
                    "error": s.error,
                }
                for s in result.steps
            ],
        }
        history.append(record)

        # 仅保留最近 30 条
        if len(history) > 30:
            history = history[-30:]

        # 写入文件
        with open(history_path, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    # ---- 输出 ----

    def _print_summary(self, result: PipelineResult):
        """打印流水线执行摘要到控制台"""
        prefix = {  # 状态前缀映射
            "success": "[OK]",
            "failed": "[FAIL]",
            "skipped": "[SKIP]",
        }
        for step in result.steps:
            icon = prefix.get(step.status, "[?]")
            line = f"  {icon} {step.step_name} ({step.duration_seconds:.0f}s)"
            if step.error:
                line += f" - {step.error[:60]}"
            logger.info(line)
        logger.info("  总耗时: %.0fs | 状态: %s", result.duration_seconds, result.status)
