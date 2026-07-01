# -*- coding: utf-8 -*-
"""
流水线编排器

按顺序执行三步骤流水线：数据更新 → 策略运行 → 飞书通知。
每个步骤独立记录状态和耗时，单个步骤失败不阻断后续步骤。
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from scheduler.models import PipelineResult, StepResult

# 跨平台文件锁：使用原子文件创建 + 目录锁
# POSIX 用 fcntl，Windows 用 os.open O_EXCL 原子创建
if sys.platform == "win32":
    import msvcrt
    _HAS_FCNTL = False
else:
    import fcntl
    _HAS_FCNTL = True

logger = logging.getLogger(__name__)

# 文件锁路径，防止并发执行
LOCK_FILE = "data/running/pipeline.lock"
# 锁超时时间（秒），超过此时间未释放则视为残留锁自动清理
LOCK_TIMEOUT_SECONDS = 30 * 60  # 30 分钟


class PipelineOrchestrator:
    """
    流水线编排器

    负责按顺序执行：
        Step 1: 数据更新（K线增量 + 除权重建 + 新股初始化 + 基础数据同步）
        Step 2: 策略运行（5维评分 + 信号生成 + CSV输出）
        Step 3: 飞书通知（运行摘要推送）

    属性:
        config: 完整配置字典
        data_dir: 数据目录路径
        kline_updater: K线更新器实例
        new_stock_detector: 新股检测器实例
        strategy_runner: 策略运行器实例
        notifier: 飞书通知器实例
        calendar: 交易日历实例
    """

    def __init__(self, config: dict):
        """
        初始化流水线编排器

        参数:
            config: 完整配置字典 (config.yaml 加载结果)
        """
        self.config = config
        self.data_dir = config.get("data_dir", "data")

        # 延迟初始化的子模块（按需加载避免导入时异常）
        self._kline_updater = None
        self._new_stock_detector = None
        self._fetcher = None
        self._strategy_runner = None
        self._notifier = None
        self._calendar = None

    # ---- 子模块懒加载 ----

    @property
    def kline_updater(self):
        """K线更新器（懒加载）"""
        if self._kline_updater is None:
            from utils.kline_updater import KlineUpdater
            self._kline_updater = KlineUpdater(self.data_dir)
        return self._kline_updater

    @property
    def new_stock_detector(self):
        """新股检测器（懒加载）"""
        if self._new_stock_detector is None:
            from utils.new_stock_detector import NewStockDetector
            self._new_stock_detector = NewStockDetector(self.data_dir)
        return self._new_stock_detector

    @property
    def fetcher(self):
        """数据采集器（懒加载）"""
        if self._fetcher is None:
            from utils.akshare_fetcher import AKShareFetcher
            self._fetcher = AKShareFetcher(self.data_dir)
        return self._fetcher

    @property
    def strategy_runner(self):
        """策略运行器（懒加载）"""
        if self._strategy_runner is None:
            from utils.global_db import get_global_db
            db = get_global_db()
            from trading.strategy_runner import StrategyRunner
            self._strategy_runner = StrategyRunner(db, self.data_dir, self.config)
        return self._strategy_runner

    @property
    def notifier(self):
        """飞书通知器（懒加载）"""
        if self._notifier is None:
            from scheduler.notifier import FeishuNotifier
            self._notifier = FeishuNotifier.from_config(self.config)
        return self._notifier

    @property
    def calendar(self):
        """交易日历（懒加载）"""
        if self._calendar is None:
            from scheduler.trading_calendar import TradingCalendar
            self._calendar = TradingCalendar()
        return self._calendar

    # ---- 锁管理 ----

    def _acquire_lock(self) -> bool:
        """
        获取文件锁，防止流水线并发执行

        使用原子文件创建（os.O_CREAT | os.O_EXCL）实现跨平台互斥。
        POSIX 上追加 fcntl.flock 确保进程崩溃后自动释放。

        超时检测：锁文件超过 LOCK_TIMEOUT_SECONDS 未更新则视为残留，
        自动删除后重试获取。

        返回:
            获取锁成功返回 True
        """
        lock_path = Path(LOCK_FILE)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # 原子创建锁文件（O_EXCL 确保文件不存在时才创建）
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            self._lock_fd = os.fdopen(fd, 'w')
            # 写入 PID 和时间，便于排查
            self._lock_fd.write(f"pid={os.getpid()}\nstart={datetime.now()}\n")
            self._lock_fd.flush()

            # POSIX: 额外加 fcntl 锁，确保进程崩溃后内核自动释放
            if _HAS_FCNTL:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            return True
        except OSError:
            # 锁文件已存在 → 检测是否超时残留
            if self._is_lock_stale(lock_path):
                logger.warning("残留锁文件已超时，自动清理后重试")
                self._force_release_stale_lock(lock_path)
                # 清理后递归重试一次
                return self._acquire_lock()
            else:
                logger.warning("流水线正在执行中（lock held），跳过本次运行")
                return False

    def _is_lock_stale(self, lock_path: Path) -> bool:
        """
        检测锁文件是否已超时

        通过文件最后修改时间判断，超过 LOCK_TIMEOUT_SECONDS 视为残留。

        参数:
            lock_path: 锁文件路径

        返回:
            超时返回 True
        """
        try:
            mtime = os.path.getmtime(str(lock_path))
            age_seconds = time.time() - mtime
            if age_seconds > LOCK_TIMEOUT_SECONDS:
                logger.warning(
                    "锁文件已过期: %s (修改时间 %s 前, 超时阈值 %d 分钟)",
                    lock_path,
                    self._format_duration(age_seconds),
                    LOCK_TIMEOUT_SECONDS // 60
                )
                return True
        except OSError:
            # 文件可能在检测时被删除，视为可获取锁
            return True
        return False

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """格式化时间跨度为可读字符串"""
        if seconds < 60:
            return f"{seconds:.0f} 秒"
        elif seconds < 3600:
            return f"{seconds / 60:.1f} 分钟"
        else:
            return f"{seconds / 3600:.1f} 小时"

    @staticmethod
    def _force_release_stale_lock(lock_path: Path):
        """
        强制清理残留的锁文件

        参数:
            lock_path: 锁文件路径
        """
        try:
            if lock_path.exists():
                # 读取残留信息用于日志
                try:
                    content = lock_path.read_text()
                    logger.info("清理残留锁文件: %s (内容: %s)", lock_path, content.strip())
                except Exception:
                    logger.info("清理残留锁文件: %s", lock_path)
                lock_path.unlink()
        except Exception as e:
            logger.error("清理残留锁文件失败: %s - %s", lock_path, e)

    def _release_lock(self):
        """释放文件锁，删除锁文件"""
        try:
            if hasattr(self, '_lock_fd') and self._lock_fd:
                if _HAS_FCNTL:
                    try:
                        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                    except Exception:
                        pass
                self._lock_fd.close()
                self._lock_fd = None
            # 删除锁文件，允许下次运行获取锁
            lock_path = Path(LOCK_FILE)
            if lock_path.exists():
                lock_path.unlink()
        except Exception as e:
            logger.debug("释放锁异常: %s", e)

    # ---- 流水线主控 ----

    def run_pipeline(self) -> PipelineResult:
        """
        执行完整流水线

        流程:
            1. 获取文件锁（防并发）
            2. 交易日检测（可选跳过）
            3. Step 1: 数据更新
            4. Step 2: 策略运行
            5. Step 3: 通知

        返回:
            PipelineResult: 包含各步骤状态、耗时、摘要
        """
        result = PipelineResult()
        result.start_time = datetime.now()
        logger.info("流水线 %s 开始执行", result.pipeline_id)

        # 获取文件锁
        if not self._acquire_lock():
            result.status = "skipped"
            result.summary = "流水线正在执行中，跳过本次运行"
            return result

        try:
            # 检查交易日（可配置跳过）
            ps_config = self.config.get("pipeline_schedule", {})
            if ps_config.get("detect_trading_day", True):
                if not self.calendar.is_trading_day():
                    logger.info("当前非交易日，跳过流水线")
                    result.status = "skipped"
                    result.summary = "非交易日，流水线已跳过"
                    result.end_time = datetime.now()
                    result.duration_seconds = (
                        result.end_time - result.start_time
                    ).total_seconds()
                    return result

            # Step 1: 数据更新
            step1 = self._step_data_update()
            result.add_step(step1)

            # Step 2: 策略运行
            step2 = self._step_strategy_run()
            result.add_step(step2)

            # Step 3: 通知
            step3 = self._step_notify(result)
            result.add_step(step3)

            # 汇总状态
            statuses = [s.status for s in result.steps]
            if all(s == "success" for s in statuses):
                result.status = "success"
            elif any(s == "failed" for s in statuses):
                # 数据更新失败但有后续结果 = partial_failure
                result.status = "partial_failure"
            else:
                result.status = "success"

            # 生成摘要
            result.summary = self._build_summary(result)

        finally:
            self._release_lock()

        result.end_time = datetime.now()
        result.duration_seconds = (
            result.end_time - result.start_time
        ).total_seconds()
        logger.info("流水线 %s 完成，状态: %s，耗时: %.1f 秒",
                     result.pipeline_id, result.status, result.duration_seconds)

        return result

    # ---- Step 1: 数据更新 ----

    def _step_data_update(self) -> StepResult:
        """
        Step 1: 数据更新

        子步骤:
            - K线增量更新: 补齐缺失交易日的K线数据
            - 除权历史重建: 检测除权事件并重建前复权历史
            - 新股初始化: 检测新上市股票并初始化数据
            - 基础数据同步: 股票列表、名称变更、ST状态

        返回:
            StepResult: 包含各子步骤状态和统计
        """
        step = StepResult(step_name="data_update")
        step.start_time = datetime.now()
        logger.info("Step 1/3: 数据更新开始")

        details = {
            "kline_updated": 0,
            "exdividend_rebuilt": 0,
            "new_stocks_initialized": 0,
            "basic_data_synced": False,
        }
        has_failure = False

        # 获取流水线配置
        ps_config = self.config.get("pipeline_schedule", {})
        du_config = ps_config.get("data_update", {})

        # 获取股票列表
        try:
            from utils.global_db import get_global_db
            db = get_global_db()
            stock_codes = db.list_all_stocks()
        except Exception as e:
            logger.error("获取股票列表失败: %s", e)
            stock_codes = []

        today_str = datetime.now().strftime("%Y%m%d")

        # 子步骤 1.1: K线增量更新
        if du_config.get("update_kline", True) and stock_codes:
            try:
                logger.info("  [1.1] K线增量更新 (%d 只股票)", len(stock_codes))
                # 查找最后更新日期
                last_date = self._find_last_kline_date(stock_codes)
                logger.info("    上次更新日期: %s", last_date)

                kline_result = self.kline_updater.update_kline_data(
                    stock_codes=stock_codes,
                    last_update_date=last_date,
                    target_date=today_str,
                )
                details["kline_updated"] = kline_result.get("updated_count", 0)
                logger.info("    K线更新完成: %d 只", details["kline_updated"])
            except Exception as e:
                logger.error("    K线增量更新失败: %s", e)
                details["kline_updated"] = -1
                has_failure = True

        # 子步骤 1.2: 除权历史重建
        if du_config.get("rebuild_exdividend", True) and stock_codes:
            try:
                logger.info("  [1.2] 除权检测与历史重建")
                exdividend_result = self.kline_updater.check_exdividend_and_rebuild(
                    stock_codes=stock_codes,
                    trade_date=today_str,
                )
                details["exdividend_rebuilt"] = exdividend_result.get("rebuilt_count", 0)
                logger.info("    除权重建完成: %d 只", details["exdividend_rebuilt"])
            except Exception as e:
                logger.error("    除权重建失败: %s", e)
                details["exdividend_rebuilt"] = -1
                has_failure = True

        # 子步骤 1.3: 新股检测与初始化
        if du_config.get("init_new_stocks", True):
            try:
                logger.info("  [1.3] 新股检测与初始化")
                new_stock_result = self.new_stock_detector.detect_and_init_new_stocks()
                details["new_stocks_initialized"] = new_stock_result.get("initialized", 0)
                logger.info("    新股初始化完成: %d 只", details["new_stocks_initialized"])
            except Exception as e:
                logger.error("    新股初始化失败: %s", e)
                details["new_stocks_initialized"] = -1
                has_failure = True

        # 子步骤 1.4: 基础数据同步
        if du_config.get("sync_basic_data", True):
            try:
                logger.info("  [1.4] 基础数据同步")
                self.fetcher.init_full_data(incremental=True)
                details["basic_data_synced"] = True
                logger.info("    基础数据同步完成")
            except Exception as e:
                logger.error("    基础数据同步失败: %s", e)
                details["basic_data_synced"] = False
                has_failure = True

        step.details = details
        step.status = "failed" if has_failure else "success"
        step.end_time = datetime.now()
        step.duration_seconds = (step.end_time - step.start_time).total_seconds()
        logger.info("Step 1 完成: %s (耗时 %.1f 秒)", step.status, step.duration_seconds)
        return step

    # ---- Step 2: 策略运行 ----

    def _step_strategy_run(self) -> StepResult:
        """
        Step 2: 策略运行

        调用 StrategyRunner.run_strategies_batch() 执行选股和信号生成。
        输出 KHunter_signals_{YYYYMMDD}.csv 到 PTrade 目录。

        返回:
            StepResult: 包含信号生成统计
        """
        step = StepResult(step_name="strategy_run")
        step.start_time = datetime.now()
        logger.info("Step 2/3: 策略运行开始")

        details = {
            "buy_signals": 0,
            "sell_signals": 0,
            "signals_generated": 0,
            "signal_file": "",
        }

        try:
            # 构建策略任务配置
            ps_config = self.config.get("pipeline_schedule", {})
            sr_config = ps_config.get("strategy_run", {})

            tasks = [{
                "selection_strategy": sr_config.get(
                    "select_strategy", "immortal_guidance"
                ),
                "timing_strategy": sr_config.get(
                    "timing_strategy", "support"
                ),
            }]

            run_config = {
                "score_threshold": sr_config.get("score_threshold", 60),
                "max_daily_buys": sr_config.get("max_daily_buys", 3),
            }

            logger.info("  任务数: %d, 选股策略: %s, 择时策略: %s",
                         len(tasks),
                         sr_config.get("select_strategy", "immortal_guidance"),
                         sr_config.get("timing_strategy", "support"))

            batch_result = self.strategy_runner.run_strategies_batch(
                tasks=tasks, config=run_config
            )

            # 提取统计数据（从 batch_result["data"] 中获取实际返回格式）
            if batch_result:
                data = batch_result.get("data", {})
                details["buy_signals"] = data.get("buy_signals", 0)
                details["sell_signals"] = data.get("sell_signals", 0)
                details["signals_generated"] = data.get("total_signals", 0)
                details["signal_file"] = data.get("ptrade_csv_file", "")

            step.details = details
            step.status = "success"
            logger.info("  信号: 买入 %d, 卖出 %d", details["buy_signals"], details["sell_signals"])

        except Exception as e:
            logger.error("  策略运行失败: %s", e)
            step.status = "failed"
            step.error = str(e)
            step.details = details

        step.end_time = datetime.now()
        step.duration_seconds = (step.end_time - step.start_time).total_seconds()
        logger.info("Step 2 完成: %s (耗时 %.1f 秒)", step.status, step.duration_seconds)
        return step

    # ---- Step 3: 通知 ----

    def _step_notify(self, result: PipelineResult) -> StepResult:
        """
        Step 3: 飞书通知

        发送流水线运行摘要到飞书群。
        通知失败不影响流水线主流程。

        参数:
            result: 当前流水线执行结果

        返回:
            StepResult: 包含通知发送状态
        """
        step = StepResult(step_name="notification")
        step.start_time = datetime.now()
        logger.info("Step 3/3: 飞书通知开始")

        details = {"feishu_sent": False}

        try:
            # 检查是否启用
            if not self.notifier.enabled:
                logger.info("  飞书通知未启用，跳过")
                step.status = "skipped"
            else:
                # 发送运行摘要
                success = self.notifier.send_summary(result)
                if success:
                    details["feishu_sent"] = True
                    logger.info("  运行摘要已发送")
                else:
                    logger.warning("  运行摘要发送失败（不阻断流水线）")

                # 异常时发送告警
                if result.status in ("partial_failure", "failed"):
                    alert_ok = self.notifier.send_alert(result)
                    if alert_ok:
                        logger.info("  异常告警已发送")

                step.status = "success"

        except Exception as e:
            logger.warning("  飞书通知失败（不阻断流水线）: %s", e)
            step.status = "success"  # 通知失败不标记为失败

        step.details = details
        step.end_time = datetime.now()
        step.duration_seconds = (step.end_time - step.start_time).total_seconds()
        logger.info("Step 3 完成: %s (耗时 %.1f 秒)", step.status, step.duration_seconds)
        return step

    # ---- 辅助方法 ----

    def _find_last_kline_date(self, stock_codes: list) -> str:
        """
        查找最近一个股票的最后K线日期作为增量更新的起点

        抽样检查前 50 只股票，取最大日期。

        参数:
            stock_codes: 股票代码列表

        返回:
            最后更新日期字符串 (YYYYMMDD)
        """
        from utils.global_db import get_global_db
        db = get_global_db()

        max_date = None
        check_count = min(50, len(stock_codes))
        for code in stock_codes[:check_count]:
            try:
                df = db.read_stock(code)
                if df is not None and not df.empty:
                    latest = df.iloc[0]['date']
                    if isinstance(latest, str):
                        latest = latest.replace("-", "")
                    else:
                        latest = latest.strftime("%Y%m%d")
                    if max_date is None or latest > max_date:
                        max_date = latest
            except Exception:
                continue

        if max_date is None:
            # 兜底：使用 10 天前
            from datetime import timedelta
            max_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        return max_date

    def _build_summary(self, result: PipelineResult) -> str:
        """
        构建人类可读的流水线摘要

        参数:
            result: 流水线执行结果

        返回:
            摘要字符串
        """
        parts = [f"流水线 {result.pipeline_id}"]
        for step in result.steps:
            status_icon = "OK" if step.status == "success" else "FAIL"
            parts.append(
                f"  {step.step_name}: {status_icon} "
                f"({step.duration_seconds:.0f}s)"
            )
        parts.append(f"总耗时: {result.duration_seconds:.0f}s")
        return "\n".join(parts)
