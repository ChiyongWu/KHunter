"""
交易时间验证模块

用于验证当前时间是否允许进行数据更新，以及确定目标更新日期。
"""

from datetime import datetime, timedelta
from typing import Tuple, Dict, Any
import logging
import json
import os

logger = logging.getLogger(__name__)

# 本地日志目录
UPDATE_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'logs', 'update_log')


class TradingTimeValidator:
    """
    交易时间验证器
    
    用于检查当前时间是否在交易时间内，以及确定目标更新日期。
    """
    
    # 交易时间配置
    TRADING_START_HOUR = 9
    TRADING_START_MINUTE = 30
    TRADING_END_HOUR = 15
    TRADING_END_MINUTE = 0
    
    def __init__(self):
        """
        初始化交易时间验证器

        所有 update_log 数据仅存储在本地文件中，
        不再依赖数据库。
        """

    @staticmethod
    def _save_update_log_to_file(update_date: str, data: Dict[str, Any]):
        """
        将 update_log 记录写入本地 JSON 文件

        每次写入一个以日期命名的独立文件，
        同时追加到 data/logs/update_log/_all.jsonl 汇总文件中。

        参数：
            update_date: 更新日期 YYYY-MM-DD
            data:       update_log 字段字典
        """
        try:
            os.makedirs(UPDATE_LOG_DIR, exist_ok=True)
            # 单日独立文件
            day_file = os.path.join(UPDATE_LOG_DIR, f'update_log_{update_date}.json')
            with open(day_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # 追加到汇总文件（JSONL 格式，每行一条）
            all_file = os.path.join(UPDATE_LOG_DIR, '_all.jsonl')
            with open(all_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(data, ensure_ascii=False) + '\n')
            logger.debug(f"update_log 已写入本地文件: {day_file}")
        except Exception as e:
            logger.warning(f"写入 update_log 本地文件失败: {e}")

    @staticmethod
    def _read_update_log_file(update_date: str) -> Dict[str, Any]:
        """
        读取指定日期的 update_log 本地文件

        参数：
            update_date: 更新日期 YYYY-MM-DD
        返回：
            update_log 字典，不存在则返回 {}
        """
        try:
            day_file = os.path.join(UPDATE_LOG_DIR, f'update_log_{update_date}.json')
            if not os.path.exists(day_file):
                return {}
            with open(day_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"读取 update_log 本地文件失败: {e}")
            return {}

    @staticmethod
    def _get_last_completed_from_files() -> str:
        """
        从本地文件中获取最后一次 completed 状态的更新日期

        返回：
            最后成功更新的日期 YYYY-MM-DD，没有则返回空字符串
        """
        try:
            if not os.path.isdir(UPDATE_LOG_DIR):
                return ""
            # 遍历目录下所有 json 文件（排除 _all.jsonl）
            latest_date = ""
            for fname in os.listdir(UPDATE_LOG_DIR):
                if fname.startswith('update_log_') and fname.endswith('.json'):
                    fpath = os.path.join(UPDATE_LOG_DIR, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        if data.get('status') == 'completed':
                            date_str = data.get('update_date', '')
                            if date_str > latest_date:
                                latest_date = date_str
                    except Exception:
                        continue
            return latest_date
        except Exception as e:
            logger.warning(f"从本地文件获取最后更新日期失败: {e}")
            return ""

    def validate_update_time(self) -> Tuple[bool, str, str]:
        """
        验证当前时间是否允许更新
        
        返回值:
            (is_valid, error_message, target_date)
            - is_valid: 是否允许更新
            - error_message: 错误信息（如果不允许更新）
            - target_date: 目标更新日期（YYYY-MM-DD格式）
        """
        # 获取当前时间
        now = datetime.now()
        current_hour = now.hour
        current_minute = now.minute
        current_time_minutes = current_hour * 60 + current_minute
        
        # 先判断今天是否为交易日
        today_str = now.strftime("%Y-%m-%d")
        is_today_trading_day = self._is_trading_day(today_str)
        
        # 计算交易时间的分钟数
        trading_start_minutes = self.TRADING_START_HOUR * 60 + self.TRADING_START_MINUTE
        trading_end_minutes = self.TRADING_END_HOUR * 60 + self.TRADING_END_MINUTE
        
        # 判断当前时间段（仅交易日才限制交易时段内不可更新）
        if is_today_trading_day and trading_start_minutes <= current_time_minutes < trading_end_minutes:
            # 在交易时间内，不允许更新
            return False, "交易时间不允许更新", ""
        
        # 确定目标更新日期
        if is_today_trading_day:
            # 交易日：按时间划分
            if current_time_minutes < trading_start_minutes:
                # 交易前（00:00-09:30），更新到前一天数据
                target_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                # 收盘后（15:00-23:59），更新到当天数据
                target_date = today_str
        else:
            # 非交易日：目标日期为最近一个交易日
            target_date = self._get_last_trading_day(today_str)
            if not target_date:
                return False, "无法确定有效的目标更新日期", ""
            logger.info(f"今天非交易日，目标更新日期为最近交易日: {target_date}")
            # 非交易日跳过已更新检查，直接允许
            return True, "", target_date
        
        # 检查目标日期是否为交易日（交易日，目标日期可能不是交易日如凌晨时段）
        if not self._is_trading_day(target_date):
            # 如果目标日期不是交易日，找到最近的一个交易日
            target_date = self._get_last_trading_day(target_date)
            if not target_date:
                return False, "无法确定有效的目标更新日期", ""
            logger.info(f"目标日期非交易日，调整目标更新日期为: {target_date}")
        
        # 检查是否已在目标日期更新过
        is_updated, error_msg = self._check_if_updated(target_date)
        if is_updated:
            return False, error_msg, target_date
        
        return True, "", target_date
    
    def _is_trading_day(self, date_str: str) -> bool:
        """
        判断指定日期是否为交易日
        
        Args:
            date_str: 日期字符串，格式 YYYY-MM-DD
        
        Returns:
            bool: 是否为交易日
        """
        try:
            from utils.trade_date_utils import is_trading_day
            return is_trading_day(date_str)
        except Exception as e:
            logger.warning(f"调用 is_trading_day 失败，使用简单判断: {str(e)}")
            # 回退到简单的周末判断
            try:
                date = datetime.strptime(date_str, '%Y-%m-%d')
                # 周末不是交易日
                if date.weekday() >= 5:
                    return False
                return True
            except Exception as ex:
                logger.error(f"日期解析失败: {str(ex)}")
                return True  # 默认认为是交易日
    
    def _get_last_trading_day(self, date_str: str) -> str:
        """
        获取指定日期之前最近的一个交易日
        
        Args:
            date_str: 日期字符串，格式 YYYY-MM-DD
        
        Returns:
            str: 最近的交易日日期（YYYY-MM-DD格式），如果找不到则返回空字符串
        """
        try:
            date = datetime.strptime(date_str, '%Y-%m-%d')
            # 最多向前查找7天
            for i in range(1, 8):
                prev_date = date - timedelta(days=i)
                prev_date_str = prev_date.strftime('%Y-%m-%d')
                if self._is_trading_day(prev_date_str):
                    return prev_date_str
            return ""
        except Exception as e:
            logger.error(f"获取最近交易日失败: {str(e)}")
            return ""
    
    def _check_if_updated(self, target_date: str) -> Tuple[bool, str]:
        """
        检查是否已在目标日期更新过（从本地文件读取）

        检查逻辑：
        1. 读取本地文件 data/logs/update_log/update_log_{date}.json
        2. 如果文件存在且 status 为 'completed'，则已更新

        Args:
            target_date: 目标更新日期（YYYY-MM-DD格式）

        返回值:
            (is_updated, error_message)
        """
        try:
            log_data = self._read_update_log_file(target_date)
            if log_data and log_data.get('status') == 'completed':
                return True, f"目标日期 {target_date} 已更新过"
            return False, ""
        except Exception as e:
            logger.error(f"检查更新日志失败: {str(e)}")
            return False, ""
    
    def record_update_start(self, target_date: str) -> str:
        """
        记录更新开始（已弃用）
        
        注意：此方法已弃用。应该只在更新成功完成后才记录。
        
        Args:
            target_date: 目标更新日期（YYYY-MM-DD格式）
        
        返回值:
            update_log 表的 ID
        """
        # 此方法已弃用，不再在更新开始时创建记录
        # 改为在 record_update_complete 中创建记录
        logger.warning(f"record_update_start 已弃用，应该只在更新成功后才记录")
        return ""
    
    def record_update_complete(self, target_date: str, stats: Dict[str, Any]) -> bool:
        """
        记录更新完成（仅写入本地文件，不再使用数据库）

        Args:
            target_date: 目标更新日期（YYYY-MM-DD格式）
            stats: 更新统计信息

        返回值:
            是否成功记录
        """
        try:
            log_data = {
                'update_date': target_date,
                'update_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'status': 'completed',
                'new_stock_detected': stats.get('new_stock_detected', 0),
                'new_stock_initialized': stats.get('new_stock_initialized', 0),
                'kline_added': stats.get('kline_added', 0),
                'kline_updated': stats.get('kline_updated', 0),
                'fund_flow_added': stats.get('fund_flow_added', 0),
                'fund_flow_updated': stats.get('fund_flow_updated', 0),
                'market_cap_updated': stats.get('market_cap_updated', 0),
                'market_cap_failed': stats.get('market_cap_failed', 0),
            }
            self._save_update_log_to_file(target_date, log_data)
            return True
        except Exception as e:
            logger.error(f"记录更新完成失败: {str(e)}")
            return False
    
    def record_update_failed(self, target_date: str, error_message: str) -> bool:
        """
        记录更新失败（仅写入本地文件，不再使用数据库）

        Args:
            target_date: 目标更新日期（YYYY-MM-DD格式）
            error_message: 错误信息

        返回值:
            是否成功处理
        """
        try:
            log_data = {
                'update_date': target_date,
                'update_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'status': 'failed',
                'error_message': error_message,
                'new_stock_detected': 0,
                'new_stock_initialized': 0,
                'kline_added': 0,
                'kline_updated': 0,
                'fund_flow_added': 0,
                'fund_flow_updated': 0,
            }
            self._save_update_log_to_file(target_date, log_data)
            logger.info(f"更新失败已记录: {target_date}，错误: {error_message}")
            return True
        except Exception as e:
            logger.error(f"处理更新失败失败: {str(e)}")
            return False
    
    @staticmethod
    def get_last_update_date() -> str:
        """
        获取上次成功更新的日期（仅从本地文件读取）

        从本地 data/logs/update_log/ 目录中获取最后 completed 状态的更新日期。

        返回值:
            上次更新日期（YYYY-MM-DD格式），如果没有则返回空字符串
        """
        try:
            # 从本地 update_log 文件获取最后成功更新的日期
            last_date = TradingTimeValidator._get_last_completed_from_files()

            if last_date:
                logger.debug(f"从本地 update_log 文件获取最后更新日期: {last_date}")
                return last_date

            logger.warning("无法获取最后更新日期：本地 update_log 无记录")
            return ""

        except Exception as e:
            logger.error(f"获取上次更新日期失败: {str(e)}")
            return ""


def is_market_closed() -> bool:
    """
    检查当前是否已收盘

    返回值:
        bool: 是否已收盘
    """
    # 获取当前时间
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute
    current_time_minutes = current_hour * 60 + current_minute
    
    # 交易时间结束时间（15:00）
    trading_end_minutes = 15 * 60 + 0
    
    # 判断是否已收盘
    if current_time_minutes >= trading_end_minutes:
        return True
    else:
        return False
