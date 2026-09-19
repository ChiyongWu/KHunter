# -*- coding: utf-8 -*-
"""
Tushare 客户端工厂

统一从 config/tushare_config.json 读取 base_url 与 api_key，
所有需要 Tushare Pro API 的模块都应通过 get_tushare_pro() 获取实例，
避免各模块自行读取配置和初始化。

配置文件示例（config/tushare_config.json）：
{
    "token": "你的tushare token",
    "base_url": "https://your-relay.example.com"
}

说明：
- token：Tushare token（中转站用户填中转站提供的 key），兼容旧字段名 api_key
- base_url：可选。留空或省略时使用官方 API 地址；配置后请求转发到中转站
"""
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置文件路径（基于项目根目录，与运行时工作目录无关）
TUSHARE_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'tushare_config.json'

# tushare SDK 私有属性名（DataApi 通过名称改写保存 API 地址）
_HTTP_URL_ATTR = '_DataApi__http_url'

_lock = threading.Lock()
_pro_cache = {}


def load_tushare_config() -> dict:
    """
    读取 Tushare 配置文件。

    Returns:
        dict: {'token': str, 'base_url': str}
              token 兼容 token/api_key 两种字段名，读取失败时返回空值
    """
    config = {'token': '', 'base_url': ''}
    try:
        with open(TUSHARE_CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f) or {}
        config['token'] = str(raw.get('token') or raw.get('api_key') or '').strip()
        config['base_url'] = str(raw.get('base_url') or '').strip().rstrip('/')
    except FileNotFoundError:
        logger.warning(f"未找到 Tushare 配置文件: {TUSHARE_CONFIG_PATH}")
    except Exception as e:
        logger.warning(f"读取 Tushare 配置失败: {e}")
    return config


def get_tushare_pro(token: str = None):
    """
    获取 Tushare Pro API 实例（同 token + base_url 的结果缓存复用）。

    Args:
        token: 显式指定的 token；为空时使用配置文件中的 api_key/token

    Returns:
        Tushare Pro API 实例；未配置 token 时返回 None（由调用方决定降级逻辑）
    """
    cfg = load_tushare_config()
    token = str(token or cfg['token']).strip()
    if not token:
        logger.warning("未配置 Tushare api_key，无法初始化 Pro API")
        return None

    base_url = cfg['base_url']
    cache_key = (token, base_url)
    with _lock:
        pro = _pro_cache.get(cache_key)
        if pro is None:
            import tushare as ts
            pro = ts.pro_api(token)
            if base_url:
                # 覆盖 SDK 默认 API 地址，将请求转发到中转站
                setattr(pro, _HTTP_URL_ATTR, base_url)
                logger.info(f"Tushare 使用自定义 API 地址: {base_url}")
            _pro_cache[cache_key] = pro
    return pro
