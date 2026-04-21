# -*- coding: utf-8 -*-
"""
市场温度计算器

基于四维度指标计算市场温度：
- 涨跌家数比（权重35%）
- 跌停家数（权重35%）
- 昨日涨停表现（权重20%）
- 成交额相对位置（权重10%）
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


class MarketTemperature:
    """市场温度计算器"""
    
    # 权重配置
    WEIGHT_UP_DOWN_RATIO = 0.35       # 涨跌家数比权重
    WEIGHT_LIMIT_DOWN = 0.35          # 跌停家数权重
    WEIGHT_LIMIT_UP_PERFORMANCE = 0.20  # 昨日涨停表现权重
    WEIGHT_VOLUME = 0.10              # 成交额权重
    
    # 温度区间阈值
    STATUS_THRESHOLDS = [
        (80, "活跃", 1.0, "正常执行，可买入多只"),
        (65, "正常", 0.8, "正常执行，控制数量"),
        (50, "偏冷", 0.5, "只买入最高分的前3只"),
        (30, "寒冷", 0.25, "只买入最高分的1只"),
        (15, "冰封", 0.1, "极轻仓试探或暂停"),
        (0, "极端", 0.0, "禁止任何买入")
    ]
    
    def __init__(self, tushare_pro=None):
        """
        初始化市场温度计算器
        
        Args:
            tushare_pro: Tushare Pro API实例，如果为None则使用默认实例
        """
        self.tushare_pro = tushare_pro
        if tushare_pro is None:
            try:
                import tushare as ts
                self.tushare_pro = ts.pro_api()
            except Exception as e:
                logger.warning(f"初始化Tushare失败: {e}")
    
    def calculate(self, trade_date: str, use_cache: bool = True) -> Dict:
        """
        计算指定日期的市场温度
        
        Args:
            trade_date: 交易日期（YYYYMMDD格式）
            use_cache: 是否使用缓存，默认True
        
        Returns:
            市场温度数据字典，包含：
            - trade_date: 交易日期
            - temperature: 综合温度值
            - status: 市场状态
            - position_ratio: 仓位系数
            - action: 狩猎场执行规则
            - 各维度得分和原始数据
        """
        # 尝试从缓存加载
        if use_cache:
            from trading.market_temperature_dao import MarketTemperatureDAO
            dao = MarketTemperatureDAO()
            cached = dao.query_by_date(trade_date)
            if cached:
                logger.info(f"使用缓存的市场温度数据: {trade_date}")
                return cached
        
        # 获取四个维度的数据
        up_down_ratio_data = self.get_up_down_ratio_data(trade_date)
        limit_down_data = self.get_limit_down_data(trade_date)
        limit_up_performance_data = self.get_limit_up_performance_data(trade_date)
        volume_data = self.get_volume_data(trade_date)
        
        # 计算各维度得分
        up_down_ratio_score = self.get_up_down_ratio_score(up_down_ratio_data)
        limit_down_score = self.get_limit_down_score(limit_down_data)
        limit_up_performance_score = self.get_limit_up_performance_score(limit_up_performance_data)
        volume_score = self.get_volume_score(volume_data)
        
        # 计算综合温度
        temperature = (
            up_down_ratio_score * self.WEIGHT_UP_DOWN_RATIO +
            limit_down_score * self.WEIGHT_LIMIT_DOWN +
            limit_up_performance_score * self.WEIGHT_LIMIT_UP_PERFORMANCE +
            volume_score * self.WEIGHT_VOLUME
        )
        
        # 确定市场状态和仓位系数
        status, position_ratio, action = self.get_status_from_temperature(temperature)
        
        result = {
            'trade_date': trade_date,
            'temperature': round(temperature, 1),
            'status': status,
            'position_ratio': position_ratio,
            'action': action,
            'up_down_ratio_score': round(up_down_ratio_score, 1),
            'limit_down_score': round(limit_down_score, 1),
            'limit_up_performance_score': round(limit_up_performance_score, 1),
            'volume_score': round(volume_score, 1),
            'up_count': up_down_ratio_data.get('up_count'),
            'down_count': up_down_ratio_data.get('down_count'),
            'limit_down_count': limit_down_data.get('limit_down_count'),
            'avg_limit_up_change': limit_up_performance_data.get('avg_change'),
            'total_volume': volume_data.get('total_volume'),
            'volume_ma5_ratio': volume_data.get('volume_ma5_ratio')
        }
        
        # 保存到数据库
        if use_cache:
            from trading.market_temperature_dao import MarketTemperatureDAO
            dao = MarketTemperatureDAO()
            dao.save(result)
        
        return result
    
    def get_up_down_ratio_data(self, trade_date: str) -> Dict:
        """
        获取涨跌家数数据
        
        Args:
            trade_date: 交易日期
        
        Returns:
            涨跌数据字典，包含上涨家数、下跌家数、涨跌比
        """
        try:
            if not self.tushare_pro:
                return self._get_mock_up_down_data(trade_date)
            
            # 格式转换
            date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
            
            # 使用daily_basic获取涨跌停家数
            df = self.tushare_pro.daily_basic(ts_code='', trade_date=date_fmt)
            
            if df is None or df.empty:
                return self._get_mock_up_down_data(trade_date)
            
            # 计算涨跌家数（基于close涨跌幅）
            up_count = len(df[df['pct_chg'] > 0])
            down_count = len(df[df['pct_chg'] < 0])
            
            return {
                'up_count': up_count,
                'down_count': down_count
            }
        except Exception as e:
            logger.warning(f"获取涨跌家数数据失败，使用模拟数据: {e}")
            return self._get_mock_up_down_data(trade_date)
    
    def _get_mock_up_down_data(self, trade_date: str) -> Dict:
        """生成模拟涨跌数据（用于测试）"""
        import random
        total = 4500
        up_count = int(total * random.uniform(0.3, 0.7))
        return {
            'up_count': up_count,
            'down_count': total - up_count
        }
    
    def get_limit_down_data(self, trade_date: str) -> Dict:
        """
        获取跌停家数数据
        
        Args:
            trade_date: 交易日期
        
        Returns:
            跌停数据字典，包含跌停家数
        """
        try:
            if not self.tushare_pro:
                return self._get_mock_limit_down_data(trade_date)
            
            # 格式转换
            date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
            
            # 使用limit_list_d获取跌停数据
            df = self.tushare_pro.limit_list_d(trade_date=date_fmt)
            
            if df is None or df.empty:
                return self._get_mock_limit_down_data(trade_date)
            
            # 统计跌停家数
            limit_down_count = len(df[df['limit_type'] == 'D'])
            
            return {
                'limit_down_count': limit_down_count
            }
        except Exception as e:
            logger.warning(f"获取跌停家数数据失败，使用模拟数据: {e}")
            return self._get_mock_limit_down_data(trade_date)
    
    def _get_mock_limit_down_data(self, trade_date: str) -> Dict:
        """生成模拟跌停数据（用于测试）"""
        import random
        return {
            'limit_down_count': int(random.uniform(0, 30))
        }
    
    def get_limit_up_performance_data(self, trade_date: str) -> Dict:
        """
        获取昨日涨停股今日表现数据
        
        Args:
            trade_date: 交易日期
        
        Returns:
            涨停表现数据字典，包含昨日涨停股今日平均涨幅
        """
        try:
            if not self.tushare_pro:
                return self._get_mock_limit_up_performance_data(trade_date)
            
            # 获取前一交易日
            prev_trade_date = self._get_prev_trade_date(trade_date)
            if not prev_trade_date:
                return self._get_mock_limit_up_performance_data(trade_date)
            
            # 格式转换
            prev_date_fmt = f"{prev_trade_date[:4]}-{prev_trade_date[4:6]}-{prev_trade_date[6:]}"
            curr_date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
            
            # 获取前一交易日涨停股
            limit_up_df = self.tushare_pro.limit_list_d(trade_date=prev_date_fmt)
            
            if limit_up_df is None or limit_up_df.empty:
                return self._get_mock_limit_up_performance_data(trade_date)
            
            # 获取这些股票今日表现
            limit_up_codes = limit_up_df[limit_up_df['limit_type'] == 'U']['ts_code'].tolist()
            
            if not limit_up_codes:
                return self._get_mock_limit_up_performance_data(trade_date)
            
            # 获取今日行情
            curr_df = self.tushare_pro.daily(trade_date=curr_date_fmt)
            if curr_df is None or curr_df.empty:
                return self._get_mock_limit_up_performance_data(trade_date)
            
            # 计算涨停股今日平均涨幅
            matched = curr_df[curr_df['ts_code'].isin(limit_up_codes)]
            if matched.empty:
                avg_change = 0
            else:
                avg_change = matched['pct_chg'].mean()
            
            return {
                'avg_change': round(avg_change, 2),
                'stock_count': len(limit_up_codes)
            }
        except Exception as e:
            logger.warning(f"获取涨停表现数据失败，使用模拟数据: {e}")
            return self._get_mock_limit_up_performance_data(trade_date)
    
    def _get_mock_limit_up_performance_data(self, trade_date: str) -> Dict:
        """生成模拟涨停表现数据（用于测试）"""
        import random
        return {
            'avg_change': round(random.uniform(-5, 8), 2),
            'stock_count': int(random.uniform(30, 100))
        }
    
    def get_volume_data(self, trade_date: str) -> Dict:
        """
        获取成交额数据
        
        Args:
            trade_date: 交易日期
        
        Returns:
            成交额数据字典，包含总成交额和相对位置
        """
        try:
            if not self.tushare_pro:
                return self._get_mock_volume_data(trade_date)
            
            # 格式转换
            date_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
            
            # 获取上证和深证的成交额
            total_volume = 0
            for index_code in ['000001.SH', '399001.SZ']:
                df = self.tushare_pro.index_daily(ts_code=index_code, trade_date=date_fmt)
                if df is not None and not df.empty:
                    # 成交额单位为千元，转换为亿元
                    total_volume += df['amount'].iloc[0] / 100000
            
            if total_volume == 0:
                return self._get_mock_volume_data(trade_date)
            
            # 计算5日平均
            ma5_volume = self._get_ma5_volume(trade_date)
            
            return {
                'total_volume': round(total_volume, 2),
                'volume_ma5_ratio': round(total_volume / ma5_volume, 2) if ma5_volume > 0 else 1.0
            }
        except Exception as e:
            logger.warning(f"获取成交额数据失败，使用模拟数据: {e}")
            return self._get_mock_volume_data(trade_date)
    
    def _get_ma5_volume(self, trade_date: str) -> float:
        """计算5日平均成交额"""
        try:
            dates = self._get_trade_dates(trade_date, 6)
            if len(dates) < 2:
                return 10000  # 默认值
            
            volumes = []
            for date in dates[:-1]:  # 不包含今日
                date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:]}"
                for index_code in ['000001.SH', '399001.SZ']:
                    df = self.tushare_pro.index_daily(ts_code=index_code, trade_date=date_fmt)
                    if df is not None and not df.empty:
                        volumes.append(df['amount'].iloc[0] / 100000)
            
            return sum(volumes) / len(volumes) if volumes else 10000
        except:
            return 10000  # 默认值
    
    def _get_mock_volume_data(self, trade_date: str) -> Dict:
        """生成模拟成交额数据（用于测试）"""
        import random
        base_volume = 10000  # 基础成交额1万亿
        return {
            'total_volume': round(base_volume * random.uniform(0.7, 1.3), 2),
            'volume_ma5_ratio': round(random.uniform(0.7, 1.3), 2)
        }
    
    def get_up_down_ratio_score(self, data: Dict) -> float:
        """
        计算涨跌家数比得分
        
        评分规则：
        - 涨跌比 >= 2.5: 100分
        - 涨跌比 1.5-2.5: 80分
        - 涨跌比 0.8-1.5: 50分
        - 涨跌比 0.3-0.8: 20分
        - 涨跌比 < 0.3: 0分
        """
        up_count = data.get('up_count', 0)
        down_count = data.get('down_count', 1)
        
        if down_count == 0:
            return 100 if up_count > 0 else 50
        
        ratio = up_count / down_count
        
        if ratio >= 2.5:
            return 100
        elif ratio >= 1.5:
            return 80
        elif ratio >= 0.8:
            return 50
        elif ratio >= 0.3:
            return 20
        else:
            return 0
    
    def get_limit_down_score(self, data: Dict) -> float:
        """
        计算跌停家数得分
        
        评分规则：
        - 跌停 0-2家: 100分
        - 跌停 3-10家: 70分
        - 跌停 11-20家: 40分
        - 跌停 21-50家: 10分
        - 跌停 > 50家: 0分
        """
        count = data.get('limit_down_count', 0)
        
        if count <= 2:
            return 100
        elif count <= 10:
            return 70
        elif count <= 20:
            return 40
        elif count <= 50:
            return 10
        else:
            return 0
    
    def get_limit_up_performance_score(self, data: Dict) -> float:
        """
        计算昨日涨停表现得分
        
        评分规则：
        - 平均涨幅 >= 5%: 100分
        - 平均涨幅 2-5%: 80分
        - 平均涨幅 0-2%: 50分
        - 平均涨幅 -3-0%: 20分
        - 平均涨幅 < -3%: 0分
        """
        avg_change = data.get('avg_change', 0)
        
        if avg_change >= 5:
            return 100
        elif avg_change >= 2:
            return 80
        elif avg_change >= 0:
            return 50
        elif avg_change >= -3:
            return 20
        else:
            return 0
    
    def get_volume_score(self, data: Dict) -> float:
        """
        计算成交额得分
        
        评分规则：
        - 成交额/5日均值 >= 1.3: 100分
        - 成交额/5日均值 1.1-1.3: 75分
        - 成交额/5日均值 0.9-1.1: 50分
        - 成交额/5日均值 0.7-0.9: 25分
        - 成交额/5日均值 < 0.7: 0分
        """
        ratio = data.get('volume_ma5_ratio', 1.0)
        
        if ratio >= 1.3:
            return 100
        elif ratio >= 1.1:
            return 75
        elif ratio >= 0.9:
            return 50
        elif ratio >= 0.7:
            return 25
        else:
            return 0
    
    def get_status_from_temperature(self, temperature: float) -> Tuple[str, float, str]:
        """
        根据温度值获取市场状态和仓位系数
        
        Args:
            temperature: 温度值（0-100）
        
        Returns:
            (市场状态, 仓位系数, 狩猎场执行规则)
        """
        for threshold, status, position_ratio, action in self.STATUS_THRESHOLDS:
            if temperature >= threshold:
                return status, position_ratio, action
        
        return "极端", 0.0, "禁止任何买入"
    
    def _get_prev_trade_date(self, trade_date: str, days: int = 1) -> Optional[str]:
        """
        获取前一交易日
        
        Args:
            trade_date: 当前日期
            days: 向前多少天
        
        Returns:
            前一交易日期
        """
        try:
            date = datetime.strptime(trade_date, '%Y%m%d')
            dates = self._get_trade_dates(trade_date, days + 5)
            
            # 找到当前日期在列表中的位置
            date_str = trade_date
            if date_str in dates:
                idx = dates.index(date_str)
                if idx >= days:
                    return dates[idx - days]
            
            return None
        except:
            return None
    
    def _get_trade_dates(self, trade_date: str, count: int) -> List[str]:
        """
        获取最近的交易日列表
        
        Args:
            trade_date: 参考日期
            count: 需要的天数
        
        Returns:
            交易日列表
        """
        try:
            if self.tushare_pro:
                df = self.tushare_pro.trade_cal(
                    start_date=self._date_minus_days(trade_date, 30),
                    end_date=trade_date,
                    is_open='1'
                )
                if df is not None and not df.empty:
                    return df['cal_date'].tolist()[-count:]
            return [trade_date]  # 降级处理
        except:
            return [trade_date]
    
    def _date_minus_days(self, date_str: str, days: int) -> str:
        """日期减天数"""
        date = datetime.strptime(date_str, '%Y%m%d')
        prev_date = date - timedelta(days=days)
        return prev_date.strftime('%Y%m%d')
