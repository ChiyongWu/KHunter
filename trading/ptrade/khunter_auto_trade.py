"""
KHunter 自动交易策略 (PTrade 云端部署脚本)
============================================
本文件同时维护在:
  1. KHunter 本地:  trading/ptrade/khunter_auto_trade.py  (git 版本控制)
  2. PTrade 云端:   /home/fly/notebook/khunter_auto_trade.py (策略执行)

功能:
  9:31 开盘读取 KHunter 信号文件并提交委托（通过 run_daily 定时触发，等第一笔行情落地）

处理顺序（两阶段）:
  阶段一: 收集全部信号，分类为卖出/买入
  阶段二: 先提交全部卖出委托，轮询等待成交到账
  阶段三: 卖出资金到账后，再处理买入委托（卖出未成交，但是等待时间已过，仍然执行买入）
  - 卖出未成交时买入不会被执行，确保资金到位后再买

买入过滤规则:
  - 688 科创板 → 不买入（暂无科创板交易权限）
  - 开盘涨幅 > 3% → 不买入（追高风险）
  - 开盘跌幅 > 3% → 不买入（强势下跌风险）
  - 当前价偏离信号价 > 3% → 不买入（价格波动风险）
  - 买入时按当前价下单（limit_price = 当前价）
  - 资金三档处理:
    ① 可用资金 < 2000元 → 直接跳过（不足最小买入金额）
    ② 2000元 ≤ 可用资金 < 需要金额 → 按可用资金降级买入（100股取整）
    ③ 可用资金 ≥ 需要金额 → 正常下单

反馈机制:
  - PTrade 原生自动导出 Fund_/Hold_ CSV 文件（不需要策略中手动生成）
  - KHunter 端 PTradeFeedbackHandler 读取 Fund_/Hold_ 文件更新 portfolio

在 PTrade 策略模块中配置:
  策略类型: 股票
  运行模式: 交易
  运行时间: 每天
"""

import pandas as pd
import time
from datetime import datetime

# ============ 全局常量 ============
# 信号文件模板（{} 填入执行日期 YYYYMMDD，与 KHunter 端命名一致）
SIGNAL_FILE = "KHunter_signals_{}.csv"

# 定时触发时间
MORNING_EXEC_TIME = '9:31'    # 开盘信号处理时间（9:31，等行情落地后再执行）

# 买入价格阈值（当前价偏离信号价 ±3% 以内才下单，信号价=昨收）
MAX_PRICE_UP_DEVIATION = 0.03    # 当前价高于信号价3%不买入（追高风险）
MAX_PRICE_DOWN_DEVIATION = 0.03  # 当前价低于信号价3%不买入（强势下跌风险）

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
          检查价格阈值后提交委托（按当前价下单）

    参照 ptradesample 的 daily_event 模式:
      - 使用 get_position(sec).last_sale_price 获取当前价
      - 使用 order(sec, vol, limit_price=current_price) 按当前价下单
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


def _normalize_symbol(symbol):
    """
    标准化股票代码为 PTrade 格式（上海 .SS，深圳 .SZ）

    KHunter 信号文件使用 .SH 表示上海，PTrade 需要转为 .SS

    Args:
        symbol: 如 "688147.SH" 或 "301314.SZ" 或 "688147"

    Returns:
        str: PTrade 标准代码，如 "688147.SS" 或 "301314.SZ"

    Raises:
        ValueError: 若 symbol 无效（NaN/None/空字符串/非字符串类型）
    """
    # 防御：NaN 是 float 类型，不是 str
    if symbol is None or not isinstance(symbol, str) or pd.isna(symbol):
        raise ValueError(f"无效的股票代码: {symbol} (类型: {type(symbol).__name__})")
    symbol = str(symbol).strip()
    if not symbol:
        raise ValueError("股票代码为空字符串")
    if symbol.endswith('.SH'):
        return symbol[:-3] + '.SS'
    return symbol  # .SZ 已正确，或无后缀时保留原样


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
        pre_positions = {pos.security: pos.amount for pos in context.portfolio.positions.values()
                         if hasattr(pos, 'amount') and pos.amount > 0}

    while pending and elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        # 每轮更新当前可用资金和持仓（用于兜底检测）
        cur_cash = context.portfolio.cash if hasattr(context, 'portfolio') else 0
        cur_positions = {}
        if hasattr(context, 'portfolio') and hasattr(context.portfolio, 'positions'):
            cur_positions = {pos.security: pos.amount for pos in context.portfolio.positions.values()
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


def process_khunter_signals(context, today_str):
    """
    读取 KHunter 信号文件并提交委托（两阶段处理：先卖后买）

    两阶段处理流程:
      阶段一: 收集所有信号，先提交全部卖出委托，等待成交到账
      阶段二: 卖出资金到账后，再处理买入委托

    买入规则:
      1. 获取当前价
      2. 当前价偏离信号价（昨收）±3% 不买入
      3. 买入前检查可用资金是否充足（此时已包含卖出回款）
      4. 买入时按当前价下单

    Args:
        context: PTrade 上下文
        today_str: 当日日期字符串 YYYYMMDD
    """
    # 构造信号文件完整路径（用 get_research_path 获取研究模块路径）
    # 按执行日期匹配文件: KHunter_signals_YYYYMMDD.csv
    research_dir = get_research_path()
    signal_filename = SIGNAL_FILE.format(today_str)
    file_path = _join_path(research_dir, UPLOAD_DIRNAME, signal_filename)
    log.info(f"[KHunter] 查找信号文件: {file_path}")

    # 检查文件是否存在
    if not _file_exists(file_path):
        log.warning(f"[KHunter] 信号文件不存在: {file_path}，跳过今日交易")
        return

    # 读取信号文件（容错多种编码，优先 UTF-8）
    df = None
    # 尝试编码列表：UTF-8 优先，GBK/GB18030 作为回退（Windows 生成中文文件常见）
    for enc in ['utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'latin-1']:
        try:
            df = pd.read_csv(file_path, encoding=enc)
            log.info(f"[KHunter] 读取到 {len(df)} 条信号 (编码: {enc})")
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception as e:
            log.error(f"[KHunter] 编码 {enc} 读取失败: {e}")
            continue
    if df is None:
        log.error(f"[KHunter] 所有编码尝试均失败，无法读取信号文件")
        return

    # 防御：清理列名首尾空格（CSV 生成方可能引入尾随空格）
    df.columns = df.columns.str.strip()

    # 校验执行日期：exec_date 必须与当日相同（KHunter端已计算好T+1交易日）
    # 防御处理：去掉连字符（兼容 2026-06-16 和 20260616 两种格式）
    if 'exec_date' in df.columns and len(df) > 0:
        csv_exec_date = str(df.iloc[0]['exec_date']).strip().replace('-', '')
        if csv_exec_date != today_str:
            log.error(f"[KHunter] 信号执行日期 {csv_exec_date} ≠ 当日 {today_str}，"
                      f"信号不属于当日，跳过全部信号")
            return
        log.info(f"[KHunter] 信号执行日期校验通过: {csv_exec_date} == {today_str}")
    else:
        log.warning("[KHunter] 信号文件缺少 exec_date 列，无法校验日期，继续处理（兼容旧格式）")

    # ========== 阶段一：收集所有信号，分类为卖出/买入 ==========
    sell_signals = []   # (idx, signal_id, symbol, volume) - 卖出信号
    buy_signals = []    # (idx, signal_id, symbol, volume, price) - 买入信号
    parse_skip_count = 0
    star_market_skip_count = 0  # 科创板跳过计数

    for idx, row in df.iterrows():
        try:
            signal_id = row.get('signal_id', f'unknown_{idx}')
            # 转换 KHunter 格式 → PTrade 格式: .SH → .SS
            symbol = _normalize_symbol(row['symbol'])
            side = row['side']
            volume = int(row['order_volume'])
            price = float(row['order_price'])
        except (KeyError, ValueError, TypeError) as e:
            log.error(f"[KHunter] 信号行#{idx} 数据解析失败: {e}，跳过该信号")
            parse_skip_count += 1
            continue

        # 防重复：检查是否已处理
        if signal_id in g.executed_signals:
            log.info(f"[KHunter] 信号 {signal_id} 已处理，跳过")
            continue

        if side == 'sell':
            sell_signals.append((idx, signal_id, symbol, volume))
        elif side == 'buy':
            # 科创板权限检查: 688 开头跳过（暂无科创板交易权限）
            if symbol.startswith('688'):
                log.info(f"[KHunter] {symbol} 科创板暂无交易权限，跳过买入信号")
                star_market_skip_count += 1
                continue
            buy_signals.append((idx, signal_id, symbol, volume, price))

    log.info(f"[KHunter] 信号分类: 卖出{len(sell_signals)}条, 买入{len(buy_signals)}条, "
             f"解析跳过{parse_skip_count}条, 科创板跳过{star_market_skip_count}条")

    # ========== 阶段二：先提交全部卖出委托 ==========
    sell_count = 0
    sell_skip_count = 0
    sell_orders = []  # (signal_id, order_id, symbol, volume) - 用于后续等待成交

    for idx, signal_id, symbol, volume in sell_signals:
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
            'submit_time': context.current_dt.strftime('%H:%M:%S')
        }
        sell_orders.append((signal_id, order_id, symbol, volume))
        sell_count += 1
        log.info(f"[KHunter] 卖出委托: {symbol} {volume}股 order_id={order_id}")

    log.info(f"[KHunter] 卖出阶段完成: 提交{sell_count}条, 跳过{sell_skip_count}条")

    # ========== 阶段三：等待卖出成交到账 ==========
    if sell_orders:
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

    for idx, signal_id, symbol, volume, price in buy_signals:
        # 规则1: 获取当前价（参照 ptradesample 使用 get_position(symbol).last_sale_price）
        try:
            current_pos = get_position(symbol)
            current_price = current_pos.last_sale_price if current_pos and current_pos.last_sale_price > 0 else price
        except Exception as e:
            log.warning(f"[KHunter] {symbol} 获取当前价失败: {e}，使用信号价 {price:.2f}")
            current_price = price

        # 规则2: 当前价偏离信号价（昨收）阈值检查，-3% ~ +3% 内才下单
        if price > 0 and current_price > 0:
            price_deviation = (current_price - price) / price
            # 当前价过高（追高风险）
            if price_deviation > MAX_PRICE_UP_DEVIATION:
                log.info(f"[KHunter] {symbol} 当前价 {current_price:.2f} "
                         f"高于信号价 {price:.2f} ({price_deviation:.1%}) "
                         f"> {MAX_PRICE_UP_DEVIATION:.0%}，跳过买入")
                buy_skip_count += 1
                continue
            # 当前价过低（强势下跌风险）
            if price_deviation < -MAX_PRICE_DOWN_DEVIATION:
                log.info(f"[KHunter] {symbol} 当前价 {current_price:.2f} "
                         f"低于信号价 {price:.2f} ({price_deviation:.1%}) "
                         f"< -{MAX_PRICE_DOWN_DEVIATION:.0%}，跳过买入")
                buy_skip_count += 1
                continue

        # 规则3: 检查可用资金（使用自追踪快照，兼容回测/实盘双模式）
        available_cash = tracked_cash
        required_amount = volume * current_price * 1.001  # 以当前价计算，预留手续费
        if available_cash < required_amount:
            # 可用资金本身已不足最小买入金额，直接跳过，无需尝试调整
            if available_cash < MIN_BUY_AMOUNT:
                log.warning(f"[KHunter] {symbol} 买入需要 {required_amount:.0f}，"
                           f"可用 {available_cash:.0f}，"
                           f"不足{MIN_BUY_AMOUNT}元，跳过")
                buy_skip_count += 1
                continue
            # 可用资金不足但 >= MIN_BUY_AMOUNT，按实际资金调整买入数量（100股取整）
            adjusted_volume = int(available_cash / (current_price * 1.001) / 100) * 100
            adjusted_amount = adjusted_volume * current_price * 1.001
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
            required_amount = volume * current_price * 1.001  # 更新实际占用金额

        # 提交委托：按当前价下单
        order_id = order(symbol, volume, limit_price=current_price)
        g.executed_signals[signal_id] = {
            'order_id': order_id,
            'symbol': symbol,
            'side': 'buy',
            'volume': volume,
            'price': current_price,        # 记录实际下单价格
            'signal_price': price,         # 保留原始信号价供参考
            'price_type': 'current',       # 标记为按当前价下单
            'signal_id': signal_id,
            'submit_time': context.current_dt.strftime('%H:%M:%S')
        }
        buy_count += 1
        reserved_cash += required_amount  # 日志累计：保持预估值供参考

        # 从订单获取实际成交金额（回测中订单立即成交，实盘中未成交）
        # 避免 required_amount 的 1.001 估算费率与 PTrade 实际扣减不一致，
        # 导致 tracked_cash 与 portfolio.cash 产生偏差（例：估算 18820 vs 实际 18807）
        try:
            order_obj = get_order(order_id)
            # 回测模式：订单立即成交，context.portfolio.cash 已被 PTrade 更新为精确值
            if order_obj is not None and getattr(order_obj, 'filled', 0) > 0:
                tracked_cash = context.portfolio.cash  # 直接从 portfolio 同步精确剩余
            else:
                # 实盘模式：订单未成交，手动扣减预估值
                tracked_cash -= required_amount
        except Exception:
            # 查询失败时回退到预估扣减
            tracked_cash -= required_amount

        log.info(f"[KHunter] 买入委托: {symbol} {volume}股 "
                 f"信号价={price:.2f} 当前价={current_price:.2f} "
                 f"偏离={(current_price/price-1)*100:+.2f}% "
                 f"占用 {required_amount:.0f} (累计占用 {reserved_cash:.0f}) order_id={order_id}")

    log.info(f"[KHunter] 信号处理完成: 卖出{sell_count}条, 买入{buy_count}条, "
             f"跳过{parse_skip_count + sell_skip_count + buy_skip_count}条")