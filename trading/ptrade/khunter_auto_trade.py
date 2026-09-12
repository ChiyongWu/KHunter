"""
KHunter 自动交易策略 (PTrade 云端部署脚本)
============================================
本文件同时维护在:
  1. ETFHunter 本地:  trading/ptrade/khunter_auto_trade.py  (git 版本控制)
  2. PTrade 云端:   /home/fly/notebook/khunter_auto_trade.py (策略执行)

功能:
  9:31 开盘读取 ETFHunter 信号文件（ETF + 股票）并提交委托（通过 run_daily 定时触发）

双信号文件支持:
  - ETF 信号文件:   ETFHunter_signals_{YYYYMMDD}.csv
  - 股票信号文件:   KHunter_signals_{YYYYMMDD}.csv
  - 两个文件独立可选（任一缺失不影响另一方处理）

处理顺序（多级优先级排序）:
  第一优先级=卖出优于买入; 第二优先级=ETF优于股票
  → ETF卖出 → 股票卖出 → ETF买入 → 股票买入

  阶段一: 收集全部信号，按优先级排序，分类为卖出/买入
  阶段二: 先提交全部卖出委托，轮询等待成交到账
  阶段三: 卖出资金到账后，再处理买入委托（卖出未成交，但是等待时间已过，仍然执行买入）

买入过滤规则:
  - 688 科创板 → 不买入（暂无科创板交易权限）
  - 开盘涨幅 > 3% → 不买入（追高风险）
  - 开盘跌幅 > 3% → 不买入（强势下跌风险）
  - 当前价高于信号价 > 3% → 不买入（追高风险，上行护栏）
  - 买入/加仓委托价 = 信号文件中的价格（滑点由 KHunter 侧处理，PTrade 端不再叠加）
  - 资金三档处理:
    ① 可用资金 < 2000元 → 直接跳过（不足最小买入金额）
    ② 2000元 ≤ 可用资金 < 需要金额 → 按可用资金降级买入（100股取整）
    ③ 可用资金 ≥ 需要金额 → 正常下单

反馈机制:
  - PTrade 原生自动导出 Fund_/Hold_ CSV 文件（不需要策略中手动生成）
  - ETFHunter 端 PTradeFeedbackHandler 读取 Fund_/Hold_ 文件更新 portfolio

在 PTrade 策略模块中配置:
  策略类型: 股票
  运行模式: 交易
  运行时间: 每天
"""

import pandas as pd
import time
from datetime import datetime

# ============ 全局常量 ============
# 信号文件模板（{} 填入执行日期 YYYYMMDD，与 ETFHunter 端命名一致）
SIGNAL_FILE_ETF = "ETFHunter_signals_{}.csv"      # ETF信号文件
SIGNAL_FILE_STOCK = "KHunter_signals_{}.csv"       # 股票信号文件（沿用现有命名）

# 定时触发时间
MORNING_EXEC_TIME = '9:31'    # 开盘信号处理时间（9:31，等行情落地后再执行）

# 买入价格阈值（当前价偏离信号价 ±3% 以内才下单，信号价=昨收）
MAX_PRICE_UP_DEVIATION = 0.03    # 当前价高于信号价3%不买入（追高风险）
# 下行护栏已取消（需求：取消买入时低于-3%的限制），不再因下跌跳过
# 说明：PTrade 端不再处理滑点。
# 滑点已由 KHunter 侧（信号生成端）按买入执行方式计入信号价，
# 委托价直接取信号文件中的价格，本端不再叠加，避免双重滑点。
# （原 BUY_SLIPPAGE 常量已移除）

# 最小买入金额（元）：不足此金额直接跳过，避免碎股
MIN_BUY_AMOUNT = 2000

# PTrade 研究模块 upload_files 目录名（相对研究模块路径）
UPLOAD_DIRNAME = "upload_file"


def _join_path(*parts):
    """
    拼接路径（替代 os.path.join，避免导入 os 模块）

    Args:
        *parts: 路径片段

    Returns:
        str: 以 / 连接的完整路径（保留绝对路径前缀）
    """
    # 记录第一个 part 是否以 / 开头（绝对路径）
    is_absolute = parts and parts[0].startswith("/")
    # 去掉每个 part 的首尾 / 再拼接
    result = "/".join(p.strip("/") for p in parts if p)
    # 恢复绝对路径前缀
    if is_absolute:
        result = "/" + result
    return result


def _file_exists(filepath):
    """
    检查文件是否存在（替代 os.path.exists）

    Args:
        filepath: 文件路径

    Returns:
        bool: 文件是否存在
    """
    try:
        with open(filepath, 'r'):
            return True
    except Exception:
        return False


def initialize(context):
    """
    策略初始化（PTrade 生命周期入口）

    初始化全局变量并注册定时任务：
    - 9:31 run_daily 开盘处理信号（等行情数据到位）
    - PTrade 原生自动导出 Fund_/Hold_ 文件（15:05 后），无需策略处理
    """
    g.executed_signals = {}         # 当日已提交的信号记录 {signal_id: {...}}

    # 注册定时任务（PTrade 仅支持一个 run_daily）
    run_daily(context, morning_event, time=MORNING_EXEC_TIME)
    log.info(f"[KHunter] 策略初始化完成, 开盘处理={MORNING_EXEC_TIME}")


def before_trading_start(context, data):
    """
    PTrade 盘前事件（每个交易日约 9:25 触发一次）

    重置当日状态，信号处理由 run_daily(9:31) 定时触发
    """
    # 重置当日信号记录
    g.executed_signals = {}


def morning_event(context):
    """
    开盘处理事件（run_daily 定时触发，9:31 执行，等第一笔行情落地）

    功能: 读取 KHunter 信号文件，获取当前价，
          检查价格阈值后提交委托（按【信号文件中的价格】下单）

    委托价口径（需求变更）:
      买入/加仓信号一律以信号文件中的价格（order_price）为委托价，
      不再按开盘/当前价上浮滑点，避免与信号生成端的滑点重复叠加。

    参照 ptradesample 的 daily_event 模式:
      - 使用 get_position(sec).last_sale_price 获取当前价（仅用于偏离检查与异常兜底）
      - 使用 order(sec, vol, limit_price=信号价) 按信号价下单
    """
    today_str = context.current_dt.strftime('%Y%m%d')
    log.info(f"[KHunter] 开盘处理开始, 日期={today_str}")
    process_khunter_signals(context, today_str)



def handle_data(context, data):
    """
    PTrade 盘中事件（9:30-15:00 每分钟触发）

    本策略通过 run_daily 定时触发完成所有操作，
    handle_data 无需额外操作。PTrade 原生会在 15:05 后
    自动导出 Fund_/Hold_ 文件供 KHunter 读取。
    """
    pass


# 交易所代码段前缀 → 后缀映射（A股/场内基金）
# 上交所 .SS：50/51/52/55/56/58/59(ETF/基金), 60/68/69(股票/科创板), 90/91(B股)
# 深交所 .SZ：00/30/20(股票), 15/16/18/19(ETF/基金)
_EXCHANGE_PREFIX_SUFFIX = {
    '50': 'SS', '51': 'SS', '52': 'SS', '55': 'SS', '56': 'SS', '58': 'SS', '59': 'SS',
    '60': 'SS', '68': 'SS', '69': 'SS', '90': 'SS', '91': 'SS',
    '00': 'SZ', '30': 'SZ', '20': 'SZ',
    '15': 'SZ', '16': 'SZ', '18': 'SZ', '19': 'SZ',
}


def _infer_exchange_suffix(code):
    """
    根据 6 位代码前缀推断交易所后缀（.SS/.SZ）

    仅依据代码前两位在映射表中查找，命中返回对应后缀，未命中返回空串
    （交由调用方决定如何处理无法推断的代码）。

    Args:
        code: 6 位纯数字证券代码（如 "517380"）

    Returns:
        str: ".SS" / ".SZ"，无法推断时返回 ""
    """
    # 仅取前两位前缀查表，避免越界
    if len(code) >= 2:
        return _EXCHANGE_PREFIX_SUFFIX.get(code[:2], "")
    return ""


def _normalize_symbol(symbol):
    """
    标准化证券代码为 PTrade 格式（上海 .SS，深圳 .SZ）

    KHunter 信号文件约定上海代码带 .SH、深圳带 .SZ；但 CSV 中纯数字代码
    （如 ETF 517380）会被 pandas 解析为 int/float，且部分信号可能缺失交易所
    后缀。本函数统一处理：
      1) 数值类型(int/float) 先转字符串（整型浮点如 517380.0 去 .0）
      2) 已带 .SH → 转 .SS；已带 .SZ/.SS → 保留
      3) 无后缀的 6 位数字代码 → 按交易所前缀自动补全 .SS/.SZ
      4) NaN/None/空串 → 抛 ValueError

    Args:
        symbol: 如 "688147.SH"、"301314.SZ"、517380(int)、"517380"

    Returns:
        str: PTrade 标准代码，如 "688147.SS"、"301314.SZ"、"517380.SS"

    Raises:
        ValueError: 若 symbol 无效（NaN/None/空字符串/类型不可识别）
    """
    # 1) 数值类型先转字符串（CSV 纯数字代码被 pandas 解析为 int/float）
    if isinstance(symbol, (int, float)):
        # NaN 是 float，需先排除再转字符串
        if pd.isna(symbol):
            raise ValueError(f"无效的股票代码: {symbol} (类型: {type(symbol).__name__})")
        # 整型浮点(517380.0)去掉 .0，避免 PTrade 无法识别
        symbol = str(int(symbol)) if isinstance(symbol, float) and symbol == int(symbol) else str(symbol)

    # 2) 防御空值与非字符串
    if symbol is None or not isinstance(symbol, str) or pd.isna(symbol):
        raise ValueError(f"无效的股票代码: {symbol} (类型: {type(symbol).__name__})")

    symbol = symbol.strip()
    if not symbol:
        raise ValueError("股票代码为空字符串")

    # 3) 已带交易所后缀：直接规范化
    if symbol.endswith('.SH'):
        return symbol[:-3] + '.SS'
    if symbol.endswith('.SZ') or symbol.endswith('.SS'):
        return symbol  # 已是 PTrade 标准后缀，原样返回

    # 4) 无后缀的 6 位数字代码：按前缀补全交易所后缀
    if symbol.isdigit() and len(symbol) == 6:
        suffix = _infer_exchange_suffix(symbol)
        if suffix:
            return symbol + '.' + suffix

    # 5) 其他无法识别的格式（如无后缀的非6位/非法字符）：告警并原样返回
    log.warning(f"[KHunter] 代码 {symbol} 无法推断交易所后缀，将尝试原样提交（可能下单失败）")
    return symbol


def _get_pos_symbol(pos):
    """
    获取 PTrade 持仓对象的股票代码，兼容不同版本的属性名

    PTrade 不同版本中持仓对象标识属性名可能是 sid 或 security。
    本函数按优先级尝试获取：sid → security → 抛出异常

    Args:
        pos: PTrade Position 对象

    Returns:
        str: 如 "603341.SS"

    Raises:
        AttributeError: 若两个属性都不存在
    """
    for attr in ('sid', 'security'):
        val = getattr(pos, attr, None)
        if val is not None:
            return val
    raise AttributeError(f"PTrade 持仓对象无 sid/security 属性: {type(pos).__name__}")


def _wait_sell_orders_filled(sell_orders, context, max_wait=120, poll_interval=3):
    """
    等待卖出委托全部成交后再继续处理买入

    PTrade 中卖出委托是异步的，order() 后不会立即成交。
    本函数轮询 get_order() 检查成交状态，确保卖出资金到账后再买入。

    处理逻辑（按优先级）:
      1. 订单终态检查: status 为 canceled/rejected/filled/done 等直接判定
      2. 订单成交数量检查: filled_qty >= volume
      3. 持仓变化兜底: 持仓数量减少则判定成交
      4. 资金变化兜底: 可用现金增加则判定成交
      5. 超时后继续执行买入，依赖 context.portfolio.cash 获取最新可用资金

    Args:
        sell_orders: list of (signal_id, order_id, symbol, volume) 元组
        context: PTrade context 对象（用于获取 portfolio）
        max_wait: 最大等待秒数（默认 120 秒）
        poll_interval: 轮询间隔秒数（默认 3 秒）

    Returns:
        int: 成功成交的订单数
    """
    if not sell_orders:
        return 0

    filled_count = 0
    pending = sell_orders[:]  # 待检查的订单列表
    elapsed = 0

    # 记录初始可用资金和持仓数量，用于兜底判断
    pre_cash = context.portfolio.cash if hasattr(context, 'portfolio') else 0
    pre_positions = {}
    if hasattr(context, 'portfolio') and hasattr(context.portfolio, 'positions'):
        # PTrade 不同版本 pos 标识属性名不同：sid / security，用 getattr 兼容
        pre_positions = {_get_pos_symbol(pos): pos.amount for pos in context.portfolio.positions.values()
                         if hasattr(pos, 'amount') and pos.amount > 0}

    while pending and elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        # 每轮更新当前可用资金和持仓（用于兜底检测）
        cur_cash = context.portfolio.cash if hasattr(context, 'portfolio') else 0
        cur_positions = {}
        if hasattr(context, 'portfolio') and hasattr(context.portfolio, 'positions'):
            cur_positions = {_get_pos_symbol(pos): pos.amount for pos in context.portfolio.positions.values()
                             if hasattr(pos, 'amount') and pos.amount > 0}

        still_pending = []
        for signal_id, order_id, symbol, volume in pending:
            try:
                confirmed = False  # 是否确认完成（不再等待）
                confirm_reason = ''

                # === 优先级1: 通过订单状态/数量判断 ===
                ord_info = get_order(order_id)
                if ord_info is not None:
                    # 获取成交数量和状态（兼容 PTrade API 不同字段名）
                    filled_qty = getattr(ord_info, 'filled', None)
                    if filled_qty is None:
                        filled_qty = getattr(ord_info, 'filled_amount', None)
                    if filled_qty is None:
                        filled_qty = getattr(ord_info, 'filled_quantity', None)
                    status = getattr(ord_info, 'status', None)
                    if status is None:
                        status = getattr(ord_info, 'order_status', None) or ''
                    status_lower = str(status).lower()

                    # 已取消或被拒绝
                    if status_lower in ('canceled', 'rejected', 'cancelled'):
                        actual_filled = filled_qty if filled_qty is not None else 0
                        log.warning(f"[KHunter] 卖出被{status}: {symbol} order_id={order_id} "
                                    f"已成交 {actual_filled}/{volume}")
                        confirmed = True
                        confirm_reason = f'订单状态={status}'
                    # 终态
                    elif status_lower in ('filled', 'done', 'completed', 'success', 'finished', 'all_traded'):
                        actual_filled = filled_qty if filled_qty is not None else volume
                        log.info(f"[KHunter] 卖出成交: {symbol} {actual_filled}/{volume}股 "
                                 f"order_id={order_id} (耗时 {elapsed}s, 状态={status})")
                        confirmed = True
                        confirm_reason = f'订单状态={status}'
                    # 成交数量达标
                    elif filled_qty is not None and filled_qty >= volume:
                        log.info(f"[KHunter] 卖出成交: {symbol} {filled_qty}股 order_id={order_id} "
                                 f"(耗时 {elapsed}s)")
                        confirmed = True
                        confirm_reason = f'成交数量={filled_qty}'
                else:
                    log.warning(f"[KHunter] 订单 {order_id} 查询返回 None")

                # === 优先级2: 持仓变化兜底 ===
                # 同一股票一天只有一个卖出委托，持仓变化量直接与委托量对比即可
                if not confirmed:
                    if symbol in pre_positions and symbol not in cur_positions:
                        # 持仓已清空 → 确认卖出成交
                        log.info(f"[KHunter] 持仓兜底: {symbol} 已从持仓列表清除 "
                                 f"(原 {pre_positions[symbol]}股, 委托 {volume}股, 耗时 {elapsed}s)")
                        confirmed = True
                        confirm_reason = '持仓已清除'
                    elif symbol in pre_positions and symbol in cur_positions:
                        if cur_positions[symbol] < pre_positions[symbol]:
                            qty_reduced = pre_positions[symbol] - cur_positions[symbol]
                            if qty_reduced >= volume:
                                log.info(f"[KHunter] 持仓兜底: {symbol} 持仓减少 {qty_reduced}股 "
                                         f"({pre_positions[symbol]} → {cur_positions[symbol]}, "
                                         f"委托 {volume}股, 耗时 {elapsed}s)")
                                confirmed = True
                                confirm_reason = f'持仓减少{qty_reduced}股'

                # === 优先级3: 资金变化兜底 ===
                if not confirmed:
                    cash_increased = cur_cash - pre_cash
                    if cash_increased > 0:
                        log.info(f"[KHunter] 资金兜底: 可用资金增加 +{cash_increased:.0f}, "
                                 f"推测 {symbol} 已成交 (耗时 {elapsed}s)")
                        confirmed = True
                        confirm_reason = f'资金增加+{cash_increased:.0f}'

                if confirmed:
                    filled_count += 1
                else:
                    still_pending.append((signal_id, order_id, symbol, volume))

            except Exception as e:
                log.warning(f"[KHunter] 查询订单 {order_id} 失败: {e}，继续等待")
                still_pending.append((signal_id, order_id, symbol, volume))

        pending = still_pending
        if pending:
            symbols_left = [s for _, _, s, _ in pending]
            log.info(f"[KHunter] 等待 {len(pending)} 笔卖出成交: {symbols_left} (已等待 {elapsed}s)")

    if pending:
        log.warning(f"[KHunter] 等待超时 ({max_wait}s)，{len(pending)} 笔卖出未完全成交，"
                     f"将继续处理买入（可用资金以 context.portfolio.cash 为准）")

    return filled_count


def _read_signal_csv(file_path, today_str):
    """
    读取单个信号 CSV 文件，返回 DataFrame 或 None

    自动尝试多种编码（UTF-8/GBK），校验 exec_date 列与当日的匹配性。

    Args:
        file_path: 信号 CSV 文件的完整路径
        today_str: 当日日期字符串 YYYYMMDD

    Returns:
        pd.DataFrame 或 None（文件不存在/解析失败/日期不匹配）
    """
    # 读取信号文件（容错多种编码，优先 UTF-8）
    df = None
    for enc in ['utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'latin-1']:
        try:
            df = pd.read_csv(file_path, encoding=enc)
            log.info(f"[KHunter] 读取 {len(df)} 条信号 (编码: {enc}) 文件: {file_path}")
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception as e:
            log.error(f"[KHunter] 编码 {enc} 读取失败: {e}")
            continue
    if df is None:
        log.error(f"[KHunter] 所有编码尝试均失败，无法读取信号文件: {file_path}")
        return None

    # 防御：清理列名首尾空格（CSV 生成方可能引入尾随空格）
    df.columns = df.columns.str.strip()

    # 校验执行日期：exec_date 必须与当日相同
    if 'exec_date' in df.columns and len(df) > 0:
        csv_exec_date = str(df.iloc[0]['exec_date']).strip().replace('-', '')
        if csv_exec_date != today_str:
            log.error(f"[KHunter] 信号执行日期 {csv_exec_date} ≠ 当日 {today_str}，"
                      f"信号不属于当日，跳过文件")
            return None
        log.info(f"[KHunter] 信号执行日期校验通过: {csv_exec_date} == {today_str}")
    else:
        log.warning("[KHunter] 信号文件缺少 exec_date 列，无法校验日期，继续处理（兼容旧格式）")
    return df


def process_khunter_signals(context, today_str):
    """
    读取 KHunter 信号文件（ETF + 股票）并提交委托

    支持双信号文件:
      - ETFHunter_signals_{YYYYMMDD}.csv  (ETF信号)
      - stock_signals_{YYYYMMDD}.csv      (股票信号)
      任一文件缺失不影响另一方处理。

    多级优先级排序:
      ① 卖出优先于买入
      ② ETF优先于股票
      → ETF卖出 → 股票卖出 → ETF买入 → 股票买入

    三阶段处理:
      阶段一: 收集全部信号，按优先级排序
      阶段二: 先提交全部卖出委托，等待成交到账
      阶段三: 卖出资金到账后，再处理买入委托

    买入规则:
      1. 获取当前价
      2. 当前价偏离信号价 ±3% 不买入
      3. 买入前检查可用资金是否充足（此时已包含卖出回款）
      4. 买入时按当前价下单

    Args:
        context: PTrade 上下文
        today_str: 当日日期字符串 YYYYMMDD
    """
    # 构造信号文件完整路径（用 get_research_path 获取研究模块路径）
    research_dir = get_research_path()

    # 定义信号文件列表: (文件名模板, 信号来源标识)
    signal_sources = [
        (SIGNAL_FILE_ETF, 'etf'),
        (SIGNAL_FILE_STOCK, 'stock'),
    ]

    # 读取所有可用的信号文件，合并为带来源标记的 DataFrame
    all_dfs = []  # 收集各来源的 DataFrame
    source_count = {'etf': 0, 'stock': 0}
    for file_template, source_type in signal_sources:
        file_name = file_template.format(today_str)
        file_path = _join_path(research_dir, UPLOAD_DIRNAME, file_name)
        log.info(f"[KHunter] 查找{source_type}信号文件: {file_path}")

        if not _file_exists(file_path):
            log.info(f"[KHunter] {source_type}信号文件不存在: {file_path}，跳过")
            continue

        df = _read_signal_csv(file_path, today_str)
        if df is None or len(df) == 0:
            continue

        # 标记来源类型并加入合并列表
        df['_source_type'] = source_type
        all_dfs.append(df)
        source_count[source_type] = len(df)

    # 无可用信号时退出
    if not all_dfs:
        log.warning("[KHunter] 所有信号文件均不可用，跳过今日交易")
        return

    # 合并所有信号
    df = pd.concat(all_dfs, ignore_index=True)
    log.info(f"[KHunter] 信号合并完成: ETF {source_count['etf']}条, 股票 {source_count['stock']}条, "
             f"合计 {len(df)} 条")

    # 按优先级排序: (side_priority, source_priority)
    # sell=0 优先于 buy=1; etf=0 优先于 stock=1
    def _signal_sort_key(row):
        side = str(row.get('side', '')).strip().lower()
        side_priority = 0 if side == 'sell' else 1
        source_priority = 0 if row.get('_source_type', 'etf') == 'etf' else 1
        return (side_priority, source_priority)

    # 转换为可排序的列表并按优先级排序
    all_signals = []
    for idx, row in df.iterrows():
        all_signals.append((_signal_sort_key(row), idx, row))
    all_signals.sort(key=lambda x: x[0])

    # 重新构建有序 DataFrame
    sorted_rows = [item[2] for item in all_signals]
    df = pd.DataFrame(sorted_rows).reset_index(drop=True)

    # ========== 阶段一：收集所有信号，分类为卖出/买入 ==========
    sell_signals = []   # (idx, signal_id, symbol, volume, source_type) - 卖出信号
    buy_signals = []    # (idx, signal_id, symbol, volume, price, source_type) - 买入信号
    parse_skip_count = 0
    star_market_skip_count = 0  # 科创板跳过计数
    # 按来源分类统计
    sell_source_count = {'etf': 0, 'stock': 0}
    buy_source_count = {'etf': 0, 'stock': 0}

    for idx, row in df.iterrows():
        try:
            signal_id = row.get('signal_id', f'unknown_{idx}')
            # 转换 KHunter 格式 → PTrade 格式: .SH → .SS
            symbol = _normalize_symbol(row['symbol'])
            side = row['side']
            volume = int(row['order_volume'])
            price = float(row['order_price'])
            src = row.get('_source_type', 'etf')  # 信号来源: etf/stock
        except (KeyError, ValueError, TypeError) as e:
            log.error(f"[KHunter] 信号行#{idx} 数据解析失败: {e}，跳过该信号")
            parse_skip_count += 1
            continue

        # 防重复：检查是否已处理
        if signal_id in g.executed_signals:
            log.info(f"[KHunter] 信号 {signal_id} 已处理，跳过")
            continue

        if side == 'sell':
            sell_signals.append((idx, signal_id, symbol, volume, src))
            sell_source_count[src] = sell_source_count.get(src, 0) + 1
        elif side == 'buy':
            # 跳过无效信号: volume <= 0 或 price <= 0 无实际交易意义
            if volume <= 0 or price <= 0:
                log.warning(f"[KHunter] {symbol} 买入信号无效 "
                           f"(volume={volume}, price={price:.2f})，跳过")
                parse_skip_count += 1
                continue
            # 科创板权限检查: 688 开头跳过（暂无科创板交易权限）
            if symbol.startswith('688'):
                log.info(f"[KHunter] {symbol} 科创板暂无交易权限，跳过买入信号")
                star_market_skip_count += 1
                continue
            buy_signals.append((idx, signal_id, symbol, volume, price, src))
            buy_source_count[src] = buy_source_count.get(src, 0) + 1

    log.info(f"[KHunter] 信号分类: 卖出{len(sell_signals)}条(ETF {sell_source_count['etf']}/股票 {sell_source_count['stock']}), "
             f"买入{len(buy_signals)}条(ETF {buy_source_count['etf']}/股票 {buy_source_count['stock']}), "
             f"解析跳过{parse_skip_count}条, 科创板跳过{star_market_skip_count}条")

    # ========== 阶段二：先提交全部卖出委托 ==========
    sell_count = 0
    sell_skip_count = 0
    sell_orders = []  # (signal_id, order_id, symbol, volume) - 用于后续等待成交

    for idx, signal_id, symbol, volume, src in sell_signals:
        # 检查持仓数量
        pos = get_position(symbol)
        if pos is None or pos.enable_amount < volume:
            available = pos.enable_amount if pos else 0
            log.warning(f"[KHunter] {symbol} 持仓不足，可用 {available}，需要 {volume}")
            sell_skip_count += 1
            continue

        # 市价卖出（负数量表示卖出）
        order_id = order(symbol, -volume)
        g.executed_signals[signal_id] = {
            'order_id': order_id,
            'symbol': symbol,
            'side': 'sell',
            'volume': volume,
            'price': 0,  # 市价单不设限价
            'price_type': 'market',
            'signal_id': signal_id,
            'source_type': src,
            'submit_time': context.current_dt.strftime('%H:%M:%S')
        }
        sell_orders.append((signal_id, order_id, symbol, volume))
        sell_count += 1
        log.info(f"[KHunter] 卖出委托[{src}]: {symbol} {volume}股 order_id={order_id}")

    log.info(f"[KHunter] 卖出阶段完成: 提交{sell_count}条, 跳过{sell_skip_count}条")

    # ========== 阶段三：等待卖出成交到账 ==========
    if sell_orders:
        # 识别运行模式：回测中 order() 由回测引擎同步撮合，无需轮询等待成交
        # 实盘模式仍需轮询 get_order 确认资金到账后再买入，避免资金不足
        # PTrade 不同定制版/运行入口下 run_type 标识不统一，兼容多种回测取值
        _BACKTEST_TYPES = ('backtest', 'backtesting', 'history', 'simulate', 'sim', 'paper')
        run_type = getattr(context, 'run_type', None)
        is_backtest = run_type is not None and str(run_type).lower() in _BACKTEST_TYPES
        if is_backtest:
            # 跳过无效等待，直接确认全部卖出已按当前价撮合成交
            log.info(f"[KHunter] 回测模式，跳过卖出成交等待（回测引擎已同步撮合 {len(sell_orders)} 笔）")
            filled = len(sell_orders)
            post_sell_cash = context.portfolio.cash
            log.info(f"[KHunter] 卖出成交完成: {filled}/{len(sell_orders)} 笔, "
                     f"可用资金: {post_sell_cash:.0f}")
        else:
            # 实盘：轮询等待成交到账后再继续买入
            log.info(f"[KHunter] 等待 {len(sell_orders)} 笔卖出成交后继续买入...")
            pre_sell_cash = context.portfolio.cash
            filled = _wait_sell_orders_filled(sell_orders, context)
            post_sell_cash = context.portfolio.cash
            cash_change = post_sell_cash - pre_sell_cash
            log.info(f"[KHunter] 卖出成交完成: {filled}/{len(sell_orders)} 笔, "
                     f"资金变动: {pre_sell_cash:.0f} → {post_sell_cash:.0f} (+{cash_change:.0f})")

    # ========== 阶段四：处理买入委托（此时可用资金已包含卖出回款）==========
    # 独立追踪可用资金快照，每笔买入后从中扣除。
    # 不直接依赖 context.portfolio.cash - reserved_cash，因为：
    #   - 回测模式：order() 后 portfolio.cash 立即扣减，reserved_cash 再减 = 双重扣减
    #   - 实盘模式：order() 后 portfolio.cash 不立即扣减，需要 reserved_cash 手动跟踪
    # 使用 tracked_cash 自追踪，两种模式下行为一致。
    buy_count = 0
    buy_skip_count = 0
    tracked_cash = context.portfolio.cash  # 可用资金快照（跟随买入递减）
    reserved_cash = 0.0  # 已占用的资金（仅用于日志累计）

    for idx, signal_id, symbol, volume, price, src in buy_signals:
        # 规则1: 获取当前价（参照 ptradesample 使用 get_position(symbol).last_sale_price）
        try:
            current_pos = get_position(symbol)
            current_price = current_pos.last_sale_price if current_pos and current_pos.last_sale_price > 0 else price
        except Exception as e:
            log.warning(f"[KHunter] {symbol} 获取当前价失败: {e}，使用信号价 {price:.2f}")
            current_price = price

        # 规则2: 当前价偏离信号价（昨收）阈值检查，仅保留上行护栏（+3% 追高风险跳过）
        # 下行护栏已取消（需求：取消买入时低于-3%的限制）
        if price > 0 and current_price > 0:
            price_deviation = (current_price - price) / price
            # 当前价过高（追高风险）
            if price_deviation > MAX_PRICE_UP_DEVIATION:
                log.info(f"[KHunter] {symbol} 当前价 {current_price:.2f} "
                         f"高于信号价 {price:.2f} ({price_deviation:.1%}) "
                         f"> {MAX_PRICE_UP_DEVIATION:.0%}，跳过买入")
                buy_skip_count += 1
                continue

        # 规则3: 检查可用资金（使用自追踪快照，兼容回测/实盘双模式）
        # 资金预占用按上浮委托价计算，避免实盘成交价略高于当前价导致资金不足
        # ETF保留3位小数(0.001)，股票保留2位小数(0.01)，与A股价格最小变动单位一致
        price_precision = 3 if src == 'etf' else 2
        # 委托价以【信号文件中的价格】为准，不再按当前价上浮滑点。
        # 信号价由 ETFHunter 按买入执行方式生成（收盘价 或 五日线×滑点），
        # PTrade 端若再叠加 BUY_SLIPPAGE 会造成双重滑点。
        if price and price > 0:
            limit_price = round(price, price_precision)
        else:
            # 信号价异常（缺失/为0）时回退当前价（同样不叠加滑点）
            limit_price = round(current_price, price_precision)
        available_cash = tracked_cash
        required_amount = volume * limit_price * 1.001  # 以委托价计算，预留手续费
        if available_cash < required_amount:
            # 可用资金本身已不足最小买入金额，直接跳过，无需尝试调整
            if available_cash < MIN_BUY_AMOUNT:
                log.warning(f"[KHunter] {symbol} 买入需要 {required_amount:.0f}，"
                           f"可用 {available_cash:.0f}，"
                           f"不足{MIN_BUY_AMOUNT}元，跳过")
                buy_skip_count += 1
                continue
            # 可用资金不足但 >= MIN_BUY_AMOUNT，按实际资金调整买入数量（100股取整）
            adjusted_volume = int(available_cash / (limit_price * 1.001) / 100) * 100
            adjusted_amount = adjusted_volume * limit_price * 1.001
            if adjusted_amount < MIN_BUY_AMOUNT:
                log.warning(f"[KHunter] {symbol} 买入需要 {required_amount:.0f}，"
                           f"可用 {available_cash:.0f}，"
                           f"调整后 {adjusted_amount:.0f} 不足{MIN_BUY_AMOUNT}元，跳过")
                buy_skip_count += 1
                continue
            # 按可用资金调整委托量
            log.info(f"[KHunter] {symbol} 资金不足，按可用资金调整: "
                     f"{volume}股 → {adjusted_volume}股 "
                     f"(需要 {required_amount:.0f}, 可用 {available_cash:.0f})")
            volume = adjusted_volume
            required_amount = volume * limit_price * 1.001  # 更新实际占用金额

        # 提交委托：按【信号文件中的价格】下单（限价单）
        order_id = order(symbol, volume, limit_price=limit_price)
        g.executed_signals[signal_id] = {
            'order_id': order_id,
            'symbol': symbol,
            'side': 'buy',
            'volume': volume,
            'price': limit_price,          # 记录实际委托价（= 信号文件中的价格）
            'signal_price': price,         # 保留原始信号价供参考
            'price_type': 'signal',        # 标记为按信号价下单
            'signal_id': signal_id,
            'source_type': src,
            'submit_time': context.current_dt.strftime('%H:%M:%S')
        }
        buy_count += 1
        # 从追踪资金中扣除预估占用，确保后续订单不重复使用
        tracked_cash -= required_amount
        reserved_cash += required_amount  # 日志累计

        # 偏离计算仅在 price > 0 时有效，防御零除异常
        deviation_str = ""
        if price > 0:
            deviation_str = f"偏离={(current_price/price-1)*100:+.2f}% "
        log.info(f"[KHunter] 买入委托[{src}]: {symbol} {volume}股 "
                 f"信号价={price:.2f} 当前价={current_price:.2f} "
                 f"{deviation_str}"
                 f"占用 {required_amount:.0f} (累计占用 {reserved_cash:.0f}) order_id={order_id}")

    log.info(f"[KHunter] 信号处理完成: 卖出{sell_count}条, 买入{buy_count}条, "
             f"跳过{parse_skip_count + sell_skip_count + buy_skip_count}条")