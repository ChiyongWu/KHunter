# -*- coding: utf-8 -*-
"""
流水线数据模型

定义流水线执行结果和步骤结果的数据结构。
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class StepResult:
    """
    单个流水线步骤的执行结果

    属性:
        step_name: 步骤名称 ("data_update"/"strategy_run"/"notification")
        status: 执行状态 ("success"/"failed"/"skipped")
        start_time: 步骤开始时间
        end_time: 步骤结束时间
        duration_seconds: 步骤耗时（秒）
        details: 步骤详情字典
            data_update: { kline_updated, exdividend_rebuilt, new_stocks_initialized, basic_data_synced }
            strategy_run: { signals_generated, buy_signals, sell_signals }
            notification: { feishu_sent }
        error: 错误信息（如有）
    """
    step_name: str
    status: str = "pending"
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: float = 0.0
    details: Dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        """转换为字典，便于序列化和日志输出"""
        return {
            "step_name": self.step_name,
            "status": self.status,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": self.duration_seconds,
            "details": self.details,
            "error": self.error,
        }


@dataclass
class PipelineResult:
    """
    流水线完整执行结果

    属性:
        pipeline_id: 流水线执行ID (UUID)
        start_time: 整体开始时间
        end_time: 整体结束时间
        duration_seconds: 总耗时（秒）
        status: 整体状态 ("success"/"partial_failure"/"failed")
        steps: 各步骤结果列表
        summary: 人类可读的摘要字符串
    """
    pipeline_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: float = 0.0
    status: str = "pending"
    steps: List[StepResult] = field(default_factory=list)
    summary: str = ""

    def add_step(self, step: StepResult):
        """添加步骤结果"""
        self.steps.append(step)

    def to_dict(self) -> Dict:
        """转换为字典，便于序列化和日志输出"""
        return {
            "pipeline_id": self.pipeline_id,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "summary": self.summary,
        }
