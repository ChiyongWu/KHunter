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
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 配置文件路径（基于项目根目录，与运行时工作目录无关）
TUSHARE_CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config' / 'tushare_config.json'

# tushare SDK 私有属性名（DataApi 通过名称改写保存 API 地址）
_HTTP_URL_ATTR = '_DataApi__http_url'

_lock = threading.Lock()
_pro_cache = {}

# tushare SDK（1.4.29 dataapi 变体）默认 API 地址，base_url 未配置时使用
_DEFAULT_HTTP_URL = 'http://api.waditu.com/dataapi'


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


class TushareHttpClient:
    """
    tushare 轻量 HTTP 客户端（requests.Session 连接复用）

    与 tushare SDK 的 DataApi.query 线上协议完全一致（POST {base_url}/{api_name}），
    但复用底层 TLS 连接。SDK 每次调用用裸 requests.post 重建连接，
    单次多花 1~2 秒握手；本客户端供并发拉取场景使用。

    线程安全性：requests.Session 的连接池（urllib3）本身线程安全，
    并发调用 query 属预期用法。
    """

    def __init__(self, token: str, base_url: str):
        self.token = token
        self.base_url = (base_url or _DEFAULT_HTTP_URL).rstrip('/')
        self.session = requests.Session()
        # 连接池按并发线程数预留，避免高并发下连接频繁重建
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

    def query(self, api_name: str, fields: str = '', **kwargs):
        """
        调用 tushare 接口，返回 DataFrame。请求/响应格式与 SDK DataApi.query 一致，
        失败抛异常（由调用方的限速重试逻辑处理）。
        """
        # 与 SDK 保持一致：params 中附带 ts_type_name（中转站以此识别来源）
        kwargs.setdefault('ts_type_name', self.base_url)
        req_params = {
            'api_name': api_name,
            'token': self.token,
            'params': kwargs,
            'fields': fields,
        }
        res = self.session.post(f"{self.base_url}/{api_name}", json=req_params, timeout=30)
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}: {res.text[:120]}")
        result = res.json()
        if result.get('code') != 0:
            raise RuntimeError(str(result.get('msg')))
        data = result['data']
        import pandas as pd
        return pd.DataFrame(data['items'], columns=data['fields'])


_http_client_cache = {}


def get_tushare_http_client(token: str = None) -> Optional[TushareHttpClient]:
    """
    获取共享的 TushareHttpClient 实例（同 token + base_url 缓存复用）。

    Returns:
        TushareHttpClient 实例；未配置 token 时返回 None（由调用方决定降级逻辑）
    """
    cfg = load_tushare_config()
    token = str(token or cfg['token']).strip()
    if not token:
        return None
    cache_key = (token, cfg['base_url'])
    with _lock:
        client = _http_client_cache.get(cache_key)
        if client is None:
            client = TushareHttpClient(token, cfg['base_url'])
            _http_client_cache[cache_key] = client
    return client
