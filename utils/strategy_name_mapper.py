#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
策略名称映射工具

将策略的英文类名转换为中文名称，反之亦然
"""

# 策略名称映射表（英文类名 -> 中文名称）
STRATEGY_NAME_MAP = {
    'ContinuousRisingWithVolumeStrategyV2': '连阳回调策略',
    'ResistanceBreakoutStrategy': '阻力位突破策略',
    'TrendAccelerationInflectionStrategy': '趋势加速拐点',
    'MorningStarStrategy': '启明星策略',
    'MultiGoldenCrossStrategy': '多金叉共振',
    'MultiPartyCannonStrategy': '多方炮策略',
    'BottomTrendInflectionStrategy': '底部趋势拐点',
    'LimitUpPullbackStrategy': '涨停回马枪策略',
    'LimitUpSidewaysStrategy': '涨停横盘策略',
    'StrongWashWeakToStrongStrategy': '强势洗盘弱转强',
    'TrendResonanceReversalStrategy': '趋势共振反转策略',
    'WBottomStrategy': 'W底策略',
    'ImmortalGuidanceStrategy': '仙人指路策略',
    'MA20MA60Strategy': '520560策略',
}

# 反向映射表（中文名称 -> 英文类名）
STRATEGY_NAME_REVERSE_MAP = {v: k for k, v in STRATEGY_NAME_MAP.items()}


def get_chinese_name(english_name: str) -> str:
    """
    将英文策略名称转换为中文名称
    
    Args:
        english_name: 英文策略名称（类名）
        
    Returns:
        中文策略名称，如果不存在则返回原名称
    """
    return STRATEGY_NAME_MAP.get(english_name, english_name)


def get_english_name(chinese_name: str) -> str:
    """
    将中文策略名称转换为英文名称
    
    Args:
        chinese_name: 中文策略名称
        
    Returns:
        英文策略名称（类名），如果不存在则返回原名称
    """
    return STRATEGY_NAME_REVERSE_MAP.get(chinese_name, chinese_name)


def is_english_name(name: str) -> bool:
    """
    判断是否为英文策略名称
    
    Args:
        name: 策略名称
        
    Returns:
        True 如果是英文名称，False 如果是中文名称
    """
    return name in STRATEGY_NAME_MAP


def is_chinese_name(name: str) -> bool:
    """
    判断是否为中文策略名称
    
    Args:
        name: 策略名称
        
    Returns:
        True 如果是中文名称，False 如果是英文名称
    """
    return name in STRATEGY_NAME_REVERSE_MAP
