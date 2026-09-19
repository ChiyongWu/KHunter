# -*- coding: utf-8 -*-
"""
交易日工具模块

提供判断日期是否为交易日的功能。
"""

import logging
from datetime import datetime, timedelta
from functools import lru_cache
from typing import List, Optional

from utils.tushare_client import get_tushare_pro

# 配置日志记录器
logger = logging.getLogger(__name__)


# 交易日历本地缓存（模块级，进程内全局复用）
_trading_calendar_cache: set = None
_CACHE_FILE = None


def _get_cache_file() -> str:
    """获取交易日历缓存文件路径"""
    from pathlib import Path
    global _CACHE_FILE
    if _CACHE_FILE is None:
        _CACHE_FILE = str(Path(__file__).parent.parent / "data" / "trading_calendar_cache.json")
    return _CACHE_FILE


def _load_cache_from_file() -> set:
    """从本地缓存文件加载交易日历到内存"""
    global _trading_calendar_cache
    from pathlib import Path
    import json
    cache_file = Path(_get_cache_file())
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                _trading_calendar_cache = set(data.get("dates", []))
                logger.debug(f"从本地缓存加载交易日历: {len(_trading_calendar_cache)} 日")
                return _trading_calendar_cache
        except Exception as e:
            logger.warning(f"读取交易日历缓存文件失败: {e}")
    return set()


def _ensure_cache_loaded():
    """确保交易日历缓存已加载到内存（懒加载，空集合时自动重试加载）

    关键修复：模块级 _trading_calendar_cache 可能在缓存文件生成前被初始化为
    空集合（Flask 长驻进程场景），此时需要重新从文件加载。
    """
    global _trading_calendar_cache
    # None 时首次加载
    if _trading_calendar_cache is None:
        cached = _load_cache_from_file()
        if not cached:
            _trading_calendar_cache = set()
        return _trading_calendar_cache
    # 空集合时重试加载（回测引擎可能已生成缓存文件）
    if not _trading_calendar_cache:
        cached = _load_cache_from_file()
        if cached:
            _trading_calendar_cache = cached
            logger.info(f"交易日内存缓存已刷新，加载 {len(cached)} 个交易日")
    return _trading_calendar_cache


def refresh_trading_calendar_cache():
    """强制刷新交易日内存缓存（供回测引擎等上游模块写入缓存文件后调用）"""
    global _trading_calendar_cache
    # 清除 @lru_cache 缓存
    is_trading_day.cache_clear()
    get_trading_days.cache_clear()
    # 重新从文件加载
    _trading_calendar_cache = None
    _ensure_cache_loaded()


def is_trading_day(date_str: str) -> bool:
    """
    判断指定日期是否为交易日

    优先使用本地缓存（data/trading_calendar_cache.json）。
    缓存未命中时回退到 Tushare API，成功则更新缓存。
    均已失败时不再使用周末排除，直接报错。

    注意：不使用 @lru_cache，因模块级 _trading_calendar_cache 已做 set 查找，
    且 @lru_cache 会在缓存文件生成前后返回不一致的过期结果。

    参数:
        date_str: 日期字符串，支持 YYYY-MM-DD 或 YYYYMMDD 格式
    返回:
        bool: 是否为交易日
        
    Raises:
        RuntimeError: 缓存和 Tushare 均不可用
    """
    from pathlib import Path
    
    # 统一日期格式
    if '-' in date_str:
        date_str_fmt = date_str.replace('-', '')
        display_str = date_str
    else:
        date_str_fmt = date_str
        display_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    
    # 1. 优先从内存缓存查找（命中直接返回True）
    _ensure_cache_loaded()
    if _trading_calendar_cache and display_str in _trading_calendar_cache:
        return True
    
    # 1.5 周末快速判断：缓存只存交易日，周六/周日永远不会命中缓存，
    # 若直接回退 Tushare 会在离线（网络异常）时抛 RuntimeError 导致任务失败；
    # 周末必然是非交易日，直接返回 False，保证周末/节假日场景完全离线可用
    try:
        check_date = datetime.strptime(date_str_fmt, '%Y%m%d')
        if check_date.weekday() >= 5:  # 5=周六, 6=周日
            logger.debug(f"{display_str} 是周末，直接判定为非交易日")
            return False
    except ValueError:
        # 日期格式异常时交由后续 Tushare 逻辑处理（调用方应保证格式正确）
        pass

    # 2. 缓存未命中且非周末（缓存为空、工作日或节假日），回退到 Tushare 查询
    try:
        # 从配置文件读取 api_key/base_url 并创建pro实例
        pro = get_tushare_pro()
        df = pro.trade_cal(
            start_date=date_str_fmt,
            end_date=date_str_fmt,
            is_open='1'
        )
        if df is not None and not df.empty:
            # Tushare 确认是交易日，更新缓存
            _update_cache_from_tushare(date_str_fmt, date_str_fmt)
            logger.debug(f"Tushare 确认 {display_str} 是交易日")
            return True
        else:
            logger.debug(f"Tushare 确认 {display_str} 不是交易日")
            return False
    except Exception as e:
        # Tushare 不可用且缓存为空 → 报错
        raise RuntimeError(
            f"交易日判断失败: {display_str}\n"
            f"本地缓存文件不存在且 Tushare API 不可用。\n"
            f"缓存路径: {_get_cache_file()}\n"
            f"Tushare 错误: {e}\n"
            f"请在网络正常时先运行一次回测生成缓存文件。"
        )


def _update_cache_from_tushare(start_str: str, end_str: str):
    """从 Tushare 批量获取交易日历并更新本地缓存
    
    Args:
        start_str: 开始日期 (YYYYMMDD)
        end_str: 结束日期 (YYYYMMDD)
    """
    global _trading_calendar_cache
    try:
        # 从配置文件读取 api_key/base_url 并创建pro实例
        pro = get_tushare_pro()
        df = pro.trade_cal(
            exchange='SSE',
            start_date=start_str,
            end_date=end_str,
            is_open='1'
        )
        if df is not None and not df.empty:
            new_dates = set()
            for _, row in df.iterrows():
                cal_date = row['cal_date']
                new_dates.add(f"{cal_date[:4]}-{cal_date[4:6]}-{cal_date[6:8]}")
            
            # 合并到内存缓存
            _ensure_cache_loaded()
            _trading_calendar_cache.update(new_dates)
            
            # 写回文件
            cache_file = Path(_get_cache_file())
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            sorted_dates = sorted(_trading_calendar_cache)
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({"dates": sorted_dates}, f, ensure_ascii=False, indent=2)
            logger.info(f"交易日历缓存已更新: {len(new_dates)} 新日期 → 总计 {len(sorted_dates)} 日")
    except Exception as e:
        logger.debug(f"更新交易日历缓存失败: {e}")


@lru_cache(maxsize=256)
def get_trading_days(start_date: str, end_date: str) -> List[str]:
    """
    获取指定日期范围内的交易日列表（批量优化版 + LRU 缓存 + 本地文件缓存）

    策略：本地缓存 → Tushare API 补充 → 合并缓存。
    不再降级到周末排除模式（节假日不可靠）。

    参数:
        start_date: 开始日期，支持 YYYY-MM-DD 或 YYYYMMDD 格式
        end_date: 结束日期，支持 YYYY-MM-DD 或 YYYYMMDD 格式
    返回:
        List[str]: 交易日列表，格式为 YYYY-MM-DD
        
    Raises:
        RuntimeError: 缓存和 Tushare 均不可用时抛出
    """
    # 统一日期格式
    if '-' in start_date:
        start_str = start_date.replace('-', '')
    else:
        start_str = start_date

    if '-' in end_date:
        end_str = end_date.replace('-', '')
    else:
        end_str = end_date

    start_dt = datetime.strptime(start_str, '%Y%m%d')
    end_dt = datetime.strptime(end_str, '%Y%m%d')

    # 1. 先尝试从 Tushare 批量获取并更新缓存
    tushare_ok = False
    try:
        # 从配置文件读取 api_key/base_url 并创建pro实例
        pro = get_tushare_pro()
        df = pro.trade_cal(
            start_date=start_str,
            end_date=end_str,
            is_open='1'
        )

        if df is not None and not df.empty:
            # 解析并更新缓存
            _update_cache_from_tushare(start_str, end_str)
            tushare_ok = True
            # 直接返回 Tushare 结果（最新数据）
            trading_days = sorted([
                f"{row['cal_date'][:4]}-{row['cal_date'][4:6]}-{row['cal_date'][6:]}"
                for _, row in df.iterrows()
            ])
            logger.info(f"批量获取到 {len(trading_days)} 个交易日 (Tushare)")
            return trading_days

    except Exception as e:
        logger.warning(f"Tushare 批量获取交易日失败: {e}")

    # 2. Tushare 失败，从本地缓存筛选
    _ensure_cache_loaded()
    if _trading_calendar_cache:
        trading_days = []
        for d_str in sorted(_trading_calendar_cache):
            d = datetime.strptime(d_str, '%Y-%m-%d')
            if start_dt <= d <= end_dt:
                trading_days.append(d_str)
        if trading_days:
            logger.info(f"获取到 {len(trading_days)} 个交易日 (本地缓存)")
            return trading_days

    # 3. 缓存也没有 → 报错
    raise RuntimeError(
        f"获取交易日列表失败: {start_date} ~ {end_date}\n"
        f"Tushare API 不可用且本地缓存文件不存在或没有覆盖该日期范围。\n"
        f"缓存路径: {_get_cache_file()}\n"
        f"请在网络正常时先运行一次回测生成缓存文件。"
    )


def get_trading_days_between(start_date: str, end_date: str) -> int:
    """
    计算两个日期之间的交易日天数

    参数:
        start_date: 开始日期，支持 YYYY-MM-DD 格式或 date 对象
        end_date: 结束日期，支持 YYYY-MM-DD 格式或 date 对象
    返回:
        int: 交易日天数
    """
    try:
        # 处理 date 对象
        if hasattr(start_date, 'strftime'):
            start_date_str = start_date.strftime('%Y-%m-%d')
        else:
            start_date_str = start_date
        
        if hasattr(end_date, 'strftime'):
            end_date_str = end_date.strftime('%Y-%m-%d')
        else:
            end_date_str = end_date
        
        # 获取交易日列表并返回长度
        trading_days = get_trading_days(start_date_str, end_date_str)
        return len(trading_days) - 1  # 减去1，因为不包括买入当天
    except Exception as e:
        logger.error(f"计算交易日天数时出错: {e}")
        return 0


def get_previous_trading_day(date_str: str) -> str:
    """
    获取指定日期的前一个交易日

    参数:
        date_str: 日期字符串，支持 YYYY-MM-DD 或 YYYYMMDD 格式
    返回:
        str: 前一个交易日，格式为 YYYY-MM-DD
    """
    try:
        # 解析日期（支持两种格式）
        if '-' in date_str:
            date = datetime.strptime(date_str, '%Y-%m-%d')
        else:
            date = datetime.strptime(date_str, '%Y%m%d')
        
        # 向前查找前一个交易日
        current = date - timedelta(days=1)
        while True:
            current_str = current.strftime('%Y-%m-%d')
            if is_trading_day(current_str):
                logger.debug("{} 的前一个交易日是 {}".format(date_str, current_str))
                return current_str
            current -= timedelta(days=1)
    except Exception as e:
        logger.error("获取前一个交易日时出错: {}".format(e))
        # 返回默认值
        return date_str
