# coding:gbk
'''
日内做T策略 —— ETF 588170
底仓: 57700股

买卖规则（低买高卖）:
  规则2.1: 股价高于开盘+3%后，触发监控，持续记录最高点；
          当股价从最高点下滑1%后，卖出20000股。
  规则2.2: 股价低于开盘-3%后，触发监控，持续记录最低点；
          当股价从最低点上涨1%后，买入20000股。
  规则2.3: 卖出20000股后，记录卖出价；若股价继续下滑2.8%，
          触发监控，持续记录最低点；当股价从最低点上涨0.8%后，买回20000股。
  规则2.4: 买入20000股后，记录买入价；若股价继续上涨2.8%，
          触发监控，持续记录最高点；当股价从最高点下滑0.8%后，卖出20000股。
  规则3:   14:50检查持仓，若不等于底仓57700股，挂单恢复到底仓。
'''

# ============================================================
# 状态枚举
# ============================================================
STATE_IDLE               = 'IDLE'                # 空闲，等待涨3%或跌3%触发
STATE_TRACKING_HIGH      = 'TRACKING_HIGH'       # 涨过3%，正在追踪最高点
STATE_TRACKING_LOW       = 'TRACKING_LOW'        # 跌过3%，正在追踪最低点
STATE_SOLD_TRACKING_LOW  = 'SOLD_TRACKING_LOW'   # 卖出后等继续跌2.8%，追踪最低点等买回
STATE_BOUGHT_TRACKING_HIGH = 'BOUGHT_TRACKING_HIGH'  # 买入后等继续涨2.8%，追踪最高点等卖出


def init(ContextInfo):
    # ---- 策略参数 ----
    ContextInfo.LOTS          = 20000          # 每次做T的股数
    ContextInfo.BASE_HOLDING  = 57700          # 底仓数量
    ContextInfo.TARGET_CODE   = '588170.SH'    # ETF代码
    ContextInfo.accountID     = 'testS'

    # ---- 触发阈值 ----
    ContextInfo.UP_TRIGGER      = 0.03         # 开盘+3%触发追踪高点
    ContextInfo.DOWN_TRIGGER    = 0.03         # 开盘-3%触发追踪低点
    ContextInfo.SELL_PULLBACK   = 0.01         # 规则2.1: 从最高点回落1%卖出
    ContextInfo.BUY_BOUNCE      = 0.01         # 规则2.2: 从最低点反弹1%买入
    ContextInfo.SELL_CONTINUE   = 0.028        # 规则2.3: 卖出后继续跌2.8%触发追踪
    ContextInfo.REBUY_BOUNCE    = 0.008        # 规则2.3: 从最低点反弹0.8%买回
    ContextInfo.BUY_CONTINUE    = 0.028        # 规则2.4: 买入后继续涨2.8%触发追踪
    ContextInfo.RESELL_PULLBACK = 0.008        # 规则2.4: 从最高点下滑0.8%卖出

    # ---- 状态变量 ----
    ContextInfo.state           = STATE_IDLE
    ContextInfo.open_price      = 0.0          # 今日开盘价
    ContextInfo.peak_price      = 0.0          # 记录的最高点
    ContextInfo.trough_price    = 0.0          # 记录的最低点
    ContextInfo.sell_price      = 0.0          # 最近一次卖出价（规则2.3用）
    ContextInfo.buy_price       = 0.0          # 最近一次买入价（规则2.4用）
    ContextInfo.open_recorded   = False        # 是否已记录今日开盘价

    ContextInfo.set_universe([ContextInfo.TARGET_CODE])


def handlebar(ContextInfo):
    # ---- 获取当前K线时间 ----
    d = ContextInfo.barpos
    date = timetag_to_datetime(ContextInfo.get_bar_timetag(d), '%Y-%m-%d %H:%M:%S')
    hhmm = int(date[11:13] + date[14:16])      # 如 0930, 1450

    print('运行时间:', date, '| 状态:', ContextInfo.state)

    # ---- 获取最新行情 ----
    df = ContextInfo.get_market_data(
        ['open', 'high', 'low', 'close'],
        stock_code=ContextInfo.get_universe(),
        start_time=timetag_to_datetime(ContextInfo.get_bar_timetag(d - 1), '%Y%m%d%H%M%S'),
        end_time=timetag_to_datetime(ContextInfo.get_bar_timetag(d), '%Y%m%d%H%M%S'),
        period=ContextInfo.period
    )
    if df.empty:
        return

    current_price = df.iloc[-1]['close']
    if current_price <= 0:
        return

    # ============================================================
    # 第一步：记录今日开盘价（只在第一根K线记录一次）
    # ============================================================
    if not ContextInfo.open_recorded:
        ContextInfo.open_price = df.iloc[-1]['open']
        ContextInfo.open_recorded = True
        print('今日开盘价:', ContextInfo.open_price)

    open_price = ContextInfo.open_price
    if open_price <= 0:
        return

    change_ratio = (current_price - open_price) / open_price   # 相对开盘的涨跌幅

    # ============================================================
    # 第二步：14:50 尾盘归位检查（规则3）
    # ============================================================
    if hhmm >= 1450:
        rebalance_position(ContextInfo, current_price, date)
        return

    # ============================================================
    # 第三步：主体状态机 —— 日内做T逻辑
    # ============================================================
    state = ContextInfo.state

    # ----------------------------------------------------------
    # 状态: IDLE —— 等待涨3%或跌3%触发
    # ----------------------------------------------------------
    if state == STATE_IDLE:
        if change_ratio >= ContextInfo.UP_TRIGGER:
            # 规则2.1入口: 涨幅达到+3%，开始追踪最高点
            ContextInfo.state = STATE_TRACKING_HIGH
            ContextInfo.peak_price = current_price
            print('触发: 涨幅>=+3%, 开始追踪最高点. 当前价:', current_price)

        elif change_ratio <= -ContextInfo.DOWN_TRIGGER:
            # 规则2.2入口: 跌幅达到-3%，开始追踪最低点
            ContextInfo.state = STATE_TRACKING_LOW
            ContextInfo.trough_price = current_price
            print('触发: 跌幅<=-3%, 开始追踪最低点. 当前价:', current_price)

    # ----------------------------------------------------------
    # 状态: TRACKING_HIGH —— 规则2.1: 追踪最高点，回落1%则卖出
    # ----------------------------------------------------------
    elif state == STATE_TRACKING_HIGH:
        if current_price > ContextInfo.peak_price:
            ContextInfo.peak_price = current_price      # 不断更新最高点
            print('更新最高点:', ContextInfo.peak_price)

        pullback = (ContextInfo.peak_price - current_price) / ContextInfo.peak_price
        if pullback >= ContextInfo.SELL_PULLBACK:
            # 从最高点回落1%，执行卖出
            do_sell(ContextInfo, ContextInfo.LOTS, current_price, date)
            ContextInfo.sell_price = current_price
            ContextInfo.state = STATE_SOLD_TRACKING_LOW
            ContextInfo.trough_price = current_price    # 开始追踪卖出后的最低点
            print('卖出信号(规则2.1): 从最高点回落1%. 卖出价:', current_price, '| 转入卖出后追踪')

    # ----------------------------------------------------------
    # 状态: TRACKING_LOW —— 规则2.2: 追踪最低点，反弹1%则买入
    # ----------------------------------------------------------
    elif state == STATE_TRACKING_LOW:
        if current_price < ContextInfo.trough_price:
            ContextInfo.trough_price = current_price    # 不断更新最低点
            print('更新最低点:', ContextInfo.trough_price)

        bounce = (current_price - ContextInfo.trough_price) / ContextInfo.trough_price
        if bounce >= ContextInfo.BUY_BOUNCE:
            # 从最低点反弹1%，执行买入
            do_buy(ContextInfo, ContextInfo.LOTS, current_price, date)
            ContextInfo.buy_price = current_price
            ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
            ContextInfo.peak_price = current_price      # 开始追踪买入后的最高点
            print('买入信号(规则2.2): 从最低点反弹1%. 买入价:', current_price, '| 转入买入后追踪')

    # ----------------------------------------------------------
    # 状态: SOLD_TRACKING_LOW —— 规则2.3: 卖出后等继续跌2.8%，
    #        然后追踪最低点，反弹0.8%则买回
    # ----------------------------------------------------------
    elif state == STATE_SOLD_TRACKING_LOW:
        # 不断更新卖出后的最低点
        if current_price < ContextInfo.trough_price:
            ContextInfo.trough_price = current_price
            print('卖出后更新最低点:', ContextInfo.trough_price)

        # 检查是否已从卖出价继续下跌了2.8%
        drop_from_sell = (ContextInfo.sell_price - current_price) / ContextInfo.sell_price

        if drop_from_sell >= ContextInfo.SELL_CONTINUE:
            # 继续跌了2.8%以后，用0.8%反弹条件判断买回
            bounce_from_low = (current_price - ContextInfo.trough_price) / ContextInfo.trough_price
            if bounce_from_low >= ContextInfo.REBUY_BOUNCE:
                # 从最低点反弹0.8%，买回20000股
                do_buy(ContextInfo, ContextInfo.LOTS, current_price, date)
                ContextInfo.buy_price = current_price
                ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                ContextInfo.peak_price = current_price  # 开始追踪买入后的最高点
                print('买回信号(规则2.3): 卖出后继续跌2.8%+, 从最低点反弹0.8%. 买回价:', current_price, '| 转入买入后追踪')

        # 踏空保护：卖出后没继续跌2.8%而是直接涨回卖出价上方，买回防止踏空
        elif current_price >= ContextInfo.sell_price:
            do_buy(ContextInfo, ContextInfo.LOTS, current_price, date)
            ContextInfo.buy_price = current_price
            ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
            ContextInfo.peak_price = current_price
            print('买回信号(踏空保护): 股价回到卖出价上方. 买回价:', current_price, '| 转入买入后追踪')

    # ----------------------------------------------------------
    # 状态: BOUGHT_TRACKING_HIGH —— 规则2.4: 买入后等继续涨2.8%，
    #        然后追踪最高点，下滑0.8%则卖出
    # ----------------------------------------------------------
    elif state == STATE_BOUGHT_TRACKING_HIGH:
        # 不断更新买入后的最高点
        if current_price > ContextInfo.peak_price:
            ContextInfo.peak_price = current_price
            print('买入后更新最高点:', ContextInfo.peak_price)

        # 检查是否已从买入价继续上涨了2.8%
        rise_from_buy = (current_price - ContextInfo.buy_price) / ContextInfo.buy_price

        if rise_from_buy >= ContextInfo.BUY_CONTINUE:
            # 继续涨了2.8%以后，用0.8%下滑条件判断卖出
            pullback_from_high = (ContextInfo.peak_price - current_price) / ContextInfo.peak_price
            if pullback_from_high >= ContextInfo.RESELL_PULLBACK:
                # 从最高点下滑0.8%，卖出20000股
                do_sell(ContextInfo, ContextInfo.LOTS, current_price, date)
                ContextInfo.sell_price = current_price
                ContextInfo.state = STATE_SOLD_TRACKING_LOW
                ContextInfo.trough_price = current_price  # 开始追踪卖出后的最低点
                print('卖出信号(规则2.4): 买入后继续涨2.8%+, 从最高点下滑0.8%. 卖出价:', current_price, '| 转入卖出后追踪')

        # 止损保护：买入后没继续涨2.8%而是直接跌回买入价下方，卖出防止套牢
        elif current_price <= ContextInfo.buy_price:
            do_sell(ContextInfo, ContextInfo.LOTS, current_price, date)
            ContextInfo.sell_price = current_price
            ContextInfo.state = STATE_SOLD_TRACKING_LOW
            ContextInfo.trough_price = current_price
            print('卖出信号(止损保护): 股价跌回买入价下方. 卖出价:', current_price, '| 转入卖出后追踪')


# ============================================================
# 交易执行函数
# ============================================================
def do_buy(ContextInfo, lots, price, date):
    """买入指定股数"""
    code = ContextInfo.get_universe()[0]
    order_shares(code, lots, 'fix', price, ContextInfo, ContextInfo.accountID)
    print('[%s] 买入 %s  %d股 @ %.3f' % (date, code, lots, price))


def do_sell(ContextInfo, lots, price, date):
    """卖出指定股数"""
    code = ContextInfo.get_universe()[0]
    holding = get_holdings(ContextInfo.accountID, 'STOCK')
    available = holding.get(code, 0)
    if available < lots:
        lots = available
        print('可用持仓不足，调整为:', lots)
    if lots <= 0:
        print('无可用持仓可卖')
        return
    order_shares(code, -lots, 'fix', price, ContextInfo, ContextInfo.accountID)
    print('[%s] 卖出 %s  %d股 @ %.3f' % (date, code, lots, price))


def rebalance_position(ContextInfo, current_price, date):
    """
    14:50 检查持仓，恢复到底仓57700股
    """
    code = ContextInfo.get_universe()[0]
    holding = get_holdings(ContextInfo.accountID, 'STOCK')
    current_holding = holding.get(code, 0)

    diff = current_holding - ContextInfo.BASE_HOLDING
    if diff == 0:
        print('[%s] 14:50检查: 持仓=%d, 底仓=%d, 无需调整' % (date, current_holding, ContextInfo.BASE_HOLDING))
        return

    if diff > 0:
        # 持仓多于底仓，卖出多余
        order_shares(code, -diff, 'fix', current_price, ContextInfo, ContextInfo.accountID)
        print('[%s] 14:50归位: 卖出%d股, 恢复底仓%d' % (date, diff, ContextInfo.BASE_HOLDING))
    else:
        # 持仓少于底仓，买回缺少
        order_shares(code, -diff, 'fix', current_price, ContextInfo, ContextInfo.accountID)
        print('[%s] 14:50归位: 买入%d股, 恢复底仓%d' % (date, -diff, ContextInfo.BASE_HOLDING))


# ============================================================
# 辅助函数 —— 查询账户信息
# ============================================================
def get_avaliable(accountid, datatype):
    """查询可用资金"""
    result = 0
    resultlist = get_trade_detail_data(accountid, datatype, "ACCOUNT")
    for obj in resultlist:
        result = obj.m_dAvailable
    return result


def get_holdings(accountid, datatype):
    """查询持仓字典 {code: 可用数量}"""
    holdinglist = {}
    resultlist = get_trade_detail_data(accountid, datatype, "POSITION")
    for obj in resultlist:
        holdinglist[obj.m_strInstrumentID + "." + obj.m_strExchangeID] = obj.m_nCanUseVolume
    return holdinglist
