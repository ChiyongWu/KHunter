"""
策略基类定义
"""
from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    """策略抽象基类"""
    
    def __init__(self, name, params=None):
        """
        初始化策略
        :param name: 策略名称
        :param params: 参数字典
        """
        self.name = name
        self.params = params or {}
    
    def quick_filter(self, df):
        """
        快速过滤 - 由子类实现
        
        目的：提前过滤不符合条件的股票，避免不必要的指标计算
        原则：只基于价格，不涉及复杂指标
        
        :param df: 股票数据DataFrame
        :return: True表示通过快速过滤，False表示未通过
        """
        # 默认实现：不进行快速过滤
        return True
    
    def _validate_data(self, df) -> bool:
        """
        通用数据验证：检查数据完整性和长度
        
        注意：退市/停牌检查已统一移到 _is_suspended，通过当日K线有无来判断，
        无需逐只调用 Tushare API，且能同时覆盖退市和停牌两种情况。
        
        :param df: 股票数据DataFrame（倒序，最新在前）
        :return: True表示数据有效，False表示数据无效
        """
        if df is None or df.empty:
            return False
        
        # 检查最小数据长度
        if len(df) < 20:
            return False
        
        # 检查必要字段
        required_fields = ['date', 'open', 'high', 'low', 'close', 'volume']
        for field in required_fields:
            if field not in df.columns:
                return False
        
        return True
    
    def _is_suspended(self, df, selection_date):
        """
        检查股票是否停牌

        逻辑：如果选股日期当天的数据不存在（最新数据日期 < 选股日期），则认为是停牌

        :param df: 股票数据DataFrame（倒序，最新在前）
        :param selection_date: 选股日期（YYYY-MM-DD格式）
        :return: True表示停牌，False表示正常
        """
        if df is None or df.empty or not selection_date:
            return True

        try:
            latest_date = str(df.iloc[0]['date']).split()[0]
            if latest_date < selection_date:
                return True
        except Exception:
            return True

        return False

    def _validate_stock_name(self, stock_name: str) -> bool:
        """
        验证股票名称：过滤ST/退市股票

        :param stock_name: 股票名称
        :return: True表示股票名称有效，False表示应该被过滤
        """
        if not stock_name:
            return True

        # 过滤退市/异常股票
        invalid_keywords = ['退', '未知', '退市', '已退']
        if any(kw in stock_name for kw in invalid_keywords):
            return False

        # 过滤 ST/*ST 股票
        if stock_name.startswith('ST') or stock_name.startswith('*ST'):
            return False

        return True
    
    @abstractmethod
    def calculate_indicators(self, df) -> pd.DataFrame:
        """
        计算技术指标
        :param df: 股票数据DataFrame
        :return: 添加了指标列的DataFrame
        """
        pass
    
    @abstractmethod
    def select_stocks(self, df, stock_name='') -> list:
        """
        选股逻辑
        :param df: 包含指标的股票数据
        :param stock_name: 股票名称，用于过滤退市股票
        :return: 选股信号列表，每个元素为字典包含信号详情
        """
        pass
    
    def get_selection_criteria(self):
        """
        获取选股条件描述
        :return: 选股条件描述列表
        """
        return []

    def execute_selection(self, df, stock_code='', stock_name='', selection_date=None):
        """
        标准化的选股执行过程

        执行流程：
            1. 数据验证（完整性、长度、必要字段）
            2. 当日K线检查（退市/停牌股票一并过滤，无Tushare API调用）
            3. 快速过滤（优先使用带lookback的版本）
            4. 计算指标
            5-N. 选股条件检查

        :param df: 股票数据DataFrame（倒序，最新在前）
        :param stock_code: 股票代码
        :param stock_name: 股票名称
        :param selection_date: 选股日期（YYYY-MM-DD格式），如果为None则跳过当日K线检查
        :return: 选股信号列表
        """
        if not self._validate_data(df):
            return []

        # 检查当日是否有K线数据（退市/停牌股票一并过滤）
        # selection_date 为 None 时跳过，避免非交易日（周末/节假日）误判
        if selection_date and self._is_suspended(df, selection_date):
            return []

        if hasattr(self, '_quick_filter_with_lookback'):
            if not self._quick_filter_with_lookback(df):
                return []
        elif not self.quick_filter(df):
            return []

        try:
            df = self.calculate_indicators(df)
        except Exception:
            return []

        if selection_date is None:
            from datetime import datetime
            selection_date = datetime.now().strftime('%Y-%m-%d')
            if hasattr(self, '_has_kline_data'):
                if not self._has_kline_data(selection_date):
                    if hasattr(self, '_get_previous_date_with_kline_data'):
                        selection_date = self._get_previous_date_with_kline_data(selection_date)

        return self.select_stocks(df, stock_name)
    
    def analyze_stock(self, stock_code, stock_name, df, selection_date=None):
        """
        分析单只股票 - 专注于流程处理
        
        :param stock_code: 股票代码
        :param stock_name: 股票名称
        :param df: 股票数据DataFrame
        :param selection_date: 选股日期，用于当日K线检查（退市/停牌过滤）
        :return: 标准化的选股结果或None
        """
        try:
            # 使用标准化的选股执行过程，传入 selection_date 以启用当日K线检查
            signals = self.execute_selection(df, stock_code, stock_name, selection_date=selection_date)
            
            # 结果过滤和标准化
            if signals:
                return {
                    'code': stock_code,
                    'name': stock_name,
                    'signals': signals
                }
            return None
            
        except Exception as e:
            # 错误处理
            import logging
            logger = logging.getLogger(__name__)
            logger.debug(f"分析股票 {stock_code} 失败: {str(e)}")
            return None
