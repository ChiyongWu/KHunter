"""
KHunter 自动交易策略 (PTrade 云端部署脚本)
============================================
本文件同时维护在:
  1. KHunter 本地:  trading/ptrade/khunter_auto_trade.py  (git 版本控制)
  2. PTrade 云端:   /home/fly/notebook/khunter_auto_trade.py (策略执行)

功能:
  1. 9:25 开盘读取 KHunter 信号文件并提交委托（含开盘涨跌幅过滤）
  2. 15:05 收盘返回执行结果和持仓给 KHunter

处理顺序:
  - KHunter 端保证 CSV 中卖出信号排在买入信号前面
  - PTrade 端按 CSV 行顺序逐条处理，自然实现先卖后买
  - 卖出释放资金后再买入，避免资金不足

买入过滤规则:
  - 开盘涨幅 > 2% → 不买入（追高风险）
  - 开盘跌幅 > 3% → 不买入（强势下跌风险）

在 PTrade 策略模块中配置:
  策略类型: 股票
  运行模式: 交易
  运行时间: 每天
"""

import pandas as pd
from datetime import datetime, timedelta

# ============ 全局常量 ============
# 信号文件（固定文件名，KHunter 每天覆盖上传）
SIGNAL_FILE = "KHunter_signals.csv"

# 买入过滤阈值
MAX_OPEN_GAIN_PCT = 3.0    # 开盘涨幅超过此值不买入
MAX_OPEN_LOSS_PCT = 3.0    # 开盘跌幅超过此值不买入

# PTrade 研究模块 upload_files 目录名（相对研究模块路径）
UPLOAD_DIRNAME = "upload_files"


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
    策略初始化

    在策略启动时调用一次，初始化全局变量
    """
    g.executed_signals = {}         # 当日已提交的信号记录 {signal_id: {...}}


def before_trading_start(context, data):
    """
    PTrade 盘前事件（每个交易日约 9:25 触发一次）

    功能: 读取 KHunter 信号文件并提交委托
    """
    # 重置当日信号记录
    g.executed_signals = {}

    today_str = context.current_dt.strftime('%Y%m%d')
    log.info(f"[KHunter] 盘前开始处理信号, 日期={today_str}")
    process_khunter_signals(context, today_str)


def handle_data(context, data):
    """
    PTrade 盘中事件（9:30-15:00 每分钟触发）

    本策略信号处理在 before_trading_start 完成，
    结果导出在 after_trading_end 完成，
    handle_data 无需额外操作。
    """
    pass


def after_trading_end(context, data):
    """
    PTrade 盘后事件（每个交易日约 15:05 触发一次）

    功能: 导出执行结果和持仓快照到 upload_files/
    """
    today_str = context.current_dt.strftime('%Y%m%d')
    log.info(f"[KHunter] 盘后开始导出结果, 日期={today_str}")
    export_execution_results(context, today_str)


def _normalize_symbol(symbol):
    """
    标准化股票代码为 PTrade 格式（上海 .SS，深圳 .SZ）

    KHunter 信号文件使用 .SH 表示上海，PTrade 需要转为 .SS

    Args:
        symbol: 如 "688147.SH" 或 "301314.SZ" 或 "688147"

    Returns:
        str: PTrade 标准代码，如 "688147.SS" 或 "301314.SZ"
    """
    if symbol.endswith('.SH'):
        return symbol[:-3] + '.SS'
    return symbol  # .SZ 已正确，或无后缀时保留原样


def process_khunter_signals(context, today_str):
    """
    读取 KHunter 信号文件并提交委托

    执行规则:
      1. 开盘涨幅>3% 不买入（用 get_history 获取昨收今开）
      2. 开盘跌幅>3% 不买入
      3. 买入前检查可用资金是否充足
      4. 卖出前检查持仓是否足够

    Args:
        context: PTrade 上下文
        today_str: 当日日期字符串 YYYYMMDD
    """
    # 构造信号文件完整路径（用 get_research_path 获取研究模块路径）
    research_dir = get_research_path()
    file_path = _join_path(research_dir, UPLOAD_DIRNAME, SIGNAL_FILE)
    log.info(f"[KHunter] 查找信号文件: {file_path}")

    # 检查文件是否存在
    if not _file_exists(file_path):
        log.warning(f"[KHunter] 信号文件不存在: {file_path}，跳过今日交易")
        return

    # 读取信号文件
    try:
        df = pd.read_csv(file_path, encoding='utf-8')
        log.info(f"[KHunter] 读取到 {len(df)} 条信号")
    except Exception as e:
        log.error(f"[KHunter] 读取信号文件失败: {e}")
        return

    # 校验信号日期：T日出信号，T+1日执行，signal_date 必须为前一个交易日
    if 'signal_date' in df.columns and len(df) > 0:
        csv_date = str(df.iloc[0]['signal_date']).strip()
        # 字符串直接比较 YYYYMMDD，signal_date 不能 >= 执行日
        if csv_date >= today_str:
            log.error(f"[KHunter] 信号日期 {csv_date} >= 执行日期 {today_str}，"
                      f"T日信号应在T+1日执行，跳过全部信号")
            return
        # 计算前一个交易日（跳过周末）
        try:
            today_dt = datetime.strptime(today_str, '%Y%m%d')
            weekday = today_dt.weekday()  # 0=周一, 6=周日
            # 前一个交易日：周一→上周五(-3)，周日→上周五(-2)，周六→上周五(-1)，其他→昨天(-1)
            if weekday == 0:    # 周一
                prev_trading_day = (today_dt - timedelta(days=3)).strftime('%Y%m%d')
            elif weekday == 6:  # 周日
                prev_trading_day = (today_dt - timedelta(days=2)).strftime('%Y%m%d')
            elif weekday == 5:  # 周六
                prev_trading_day = (today_dt - timedelta(days=1)).strftime('%Y%m%d')
            else:               # 周二~周五
                prev_trading_day = (today_dt - timedelta(days=1)).strftime('%Y%m%d')

            if csv_date != prev_trading_day:
                log.error(f"[KHunter] 信号日期 {csv_date} ≠ 前交易日 {prev_trading_day}，"
                          f"信号可能过期，跳过全部信号")
                return
        except ValueError:
            log.warning(f"[KHunter] 日期格式异常，跳过日期校验")
    else:
        log.warning("[KHunter] 信号文件缺少 signal_date 列，无法校验日期，继续处理（兼容旧格式）")

    # 逐条处理信号
    buy_count = 0
    sell_count = 0
    skip_count = 0

    for idx, row in df.iterrows():
        signal_id = row.get('signal_id', f'unknown_{idx}')
        # 转换 KHunter 格式 → PTrade 格式: .SH → .SS
        symbol = _normalize_symbol(row['symbol'])
        side = row['side']
        volume = int(row['order_volume'])
        price = float(row['order_price'])
        price_type = row.get('price_type', 'limit')

        # 防重复：检查是否已处理
        if signal_id in g.executed_signals:
            log.info(f"[KHunter] 信号 {signal_id} 已处理，跳过")
            continue

        # ---- 买入委托 ----
        if side == 'buy':
            # 规则1: 开盘涨跌幅检查（用 get_history 获取前收和今开）
            # PTrade 使用 get_history 而非 QMT 的 attribute_history
            try:
                # 获取2条日线：前日K线和今日K线
                hist = get_history(2, frequency='1d', field=['close', 'open'],
                                   security_list=symbol, fq=None, include=False)
                if hist is not None and len(hist) >= 2:
                    prev_close = float(hist['close'].iloc[-2])    # 昨收
                    today_open = float(hist['open'].iloc[-1])     # 今开
                    if prev_close > 0 and today_open > 0:
                        open_pct = (today_open - prev_close) / prev_close * 100
                        if open_pct > MAX_OPEN_GAIN_PCT:
                            log.info(f"[KHunter] {symbol} 开盘涨幅 {open_pct:.2f}% > {MAX_OPEN_GAIN_PCT}%，跳过买入")
                            skip_count += 1
                            continue
                        if open_pct < -MAX_OPEN_LOSS_PCT:
                            log.info(f"[KHunter] {symbol} 开盘跌幅 {open_pct:.2f}% < -{MAX_OPEN_LOSS_PCT}%，跳过买入")
                            skip_count += 1
                            continue
            except Exception as e:
                log.warning(f"[KHunter] {symbol} 开盘涨跌检查失败: {e}，跳过过滤继续买入")

            # 规则2: 检查可用资金（PTrade 用 context.portfolio.cash）
            available_cash = context.portfolio.cash
            required_amount = volume * price * 1.001  # 预留手续费
            if available_cash < required_amount:
                log.warning(f"[KHunter] {symbol} 买入需要 {required_amount:.0f}，可用 {available_cash:.0f}，跳过")
                skip_count += 1
                continue

            # 提交限价委托
            order_id = order(symbol, volume, limit_price=price)
            g.executed_signals[signal_id] = {
                'order_id': order_id,
                'symbol': symbol,
                'side': side,
                'volume': volume,
                'price': price,
                'price_type': price_type,
                'signal_id': signal_id,
                'submit_time': context.current_dt.strftime('%H:%M:%S')
            }
            buy_count += 1
            log.info(f"[KHunter] 买入委托: {symbol} {volume}股 @{price:.2f} order_id={order_id}")

        # ---- 卖出委托 ----
        elif side == 'sell':
            # 检查持仓数量
            pos = get_position(symbol)
            if pos is None or pos.enable_amount < volume:
                available = pos.enable_amount if pos else 0
                log.warning(f"[KHunter] {symbol} 持仓不足，可用 {available}，需要 {volume}")
                skip_count += 1
                continue

            # 市价卖出（负数量表示卖出）
            order_id = order(symbol, -volume)
            g.executed_signals[signal_id] = {
                'order_id': order_id,
                'symbol': symbol,
                'side': side,
                'volume': volume,
                'price': 0,  # 市价单不设限价
                'price_type': 'market',
                'signal_id': signal_id,
                'submit_time': context.current_dt.strftime('%H:%M:%S')
            }
            sell_count += 1
            log.info(f"[KHunter] 卖出委托: {symbol} {volume}股 order_id={order_id}")

    log.info(f"[KHunter] 信号处理完成: 买入{buy_count}条, 卖出{sell_count}条, 跳过{skip_count}条")


def export_execution_results(context, today_str):
    """
    收盘后导出执行结果和持仓快照

    生成3个文件（均保存到 PTrade upload_files/）:
      PTrade_trades_{today_str}.csv      - 成交明细
      PTrade_portfolio_{today_str}.csv   - 持仓快照
      PTrade_account_{today_str}.csv     - 账户汇总

    Args:
        context: PTrade 上下文
        today_str: 当日日期 YYYYMMDD
    """
    log.info("[KHunter] 开始导出执行结果...")

    # ---- 1. 成交明细 ----
    trades_data = []
    for signal_id, info in g.executed_signals.items():
        symbol = info['symbol']
        pos = get_position(symbol)

        actual_price = pos.cost_basis if pos else info['price']
        actual_volume = pos.total_amount if pos else 0

        trades_data.append({
            'trade_date': today_str,
            'order_id': info.get('order_id', ''),
            'signal_id': signal_id,
            'symbol': symbol,
            'side': info['side'],
            'order_status': 'filled' if actual_volume > 0 else 'rejected',
            'order_volume': info['volume'],
            'filled_volume': actual_volume,
            'order_price': info['price'],
            'filled_price': round(actual_price, 2),
            'filled_amount': round(actual_price * actual_volume, 2),
            'order_time': info.get('submit_time', ''),
            'filled_time': context.current_dt.strftime('%H:%M:%S'),
            'reject_reason': '' if actual_volume > 0 else '未成交'
        })

    if trades_data:
        df_trades = pd.DataFrame(trades_data)
        trade_file = _join_path(get_research_path(), UPLOAD_DIRNAME,
                                f"PTrade_trades_{today_str}.csv")
        df_trades.to_csv(trade_file, index=False, encoding='utf-8')
        log.info(f"[KHunter] 成交明细已导出: {trade_file} ({len(df_trades)}条)")
    else:
        log.info("[KHunter] 今日无成交")

    # ---- 2. 持仓快照 ----
    all_positions = get_positions()
    portfolio_data = []
    for code, pos in all_positions.items():
        portfolio_data.append({
            'report_date': today_str,
            'symbol': code,
            'stock_name': pos.name if hasattr(pos, 'name') else '',
            'position_volume': pos.total_amount,
            'available_volume': pos.enable_amount,
            'cost_price': round(pos.cost_basis, 2),
            'current_price': round(pos.lastsaleprice, 2),
            'market_value': round(pos.lastsaleprice * pos.total_amount, 2),
            'profit_loss': round(pos.long_pnl, 2) if hasattr(pos, 'long_pnl') else 0,
            'profit_rate': round(pos.long_pnl_rate, 2) if hasattr(pos, 'long_pnl_rate') else 0,
            'holding_days': 0  # PTrade 侧无法精确获取
        })

    if portfolio_data:
        df_portfolio = pd.DataFrame(portfolio_data)
        portfolio_file = _join_path(get_research_path(), UPLOAD_DIRNAME,
                                    f"PTrade_portfolio_{today_str}.csv")
        df_portfolio.to_csv(portfolio_file, index=False, encoding='utf-8')
        log.info(f"[KHunter] 持仓快照已导出: {portfolio_file} ({len(df_portfolio)}只)")

    # ---- 3. 账户汇总 ----
    # PTrade Portfolio 属性: cash=可用资金, portfolio_value=总资产,
    #   positions_value=持仓市值, returns=累计收益率, pnl=浮动盈亏
    account_data = [{
        'report_date': today_str,
        'total_asset': round(context.portfolio.portfolio_value, 2),
        'available_cash': round(context.portfolio.cash, 2),
        'market_value': round(context.portfolio.positions_value, 2),
        'frozen_cash': 0.0,
        'today_profit': round(context.portfolio.pnl, 2),
        'total_profit': round(context.portfolio.returns, 2)
    }]

    if account_data:
        df_account = pd.DataFrame(account_data)
        account_file = _join_path(get_research_path(), UPLOAD_DIRNAME,
                                  f"PTrade_account_{today_str}.csv")
        df_account.to_csv(account_file, index=False, encoding='utf-8')
        log.info(f"[KHunter] 账户汇总已导出: {account_file}")

    log.info(f"[KHunter] 执行结果导出完成 (成交{len(trades_data)}条, 持仓{len(portfolio_data)}只)")
