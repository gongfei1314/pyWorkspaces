# coding:gbk
'''
日内做T策略 - ETF 588170.SH
底仓: 57700股

买卖规则（低买高卖）:
  规则2.1: 股价 >= 开盘+3%, 追踪最高点; 从最高点回撤1% -> 卖出20000股
  规则2.2: 股价 <= 开盘-3%, 追踪最低点; 从最低点反弹1% -> 买入20000股
  规则2.3: 卖出后, 若股价继续下跌2%,
           追踪最低点; 反弹0.8% -> 买入20000股
  规则2.4: 买入后, 若股价继续上涨2%,
           追踪最高点; 回撤0.8% -> 卖出20000股
  规则3:   14:50 恢复底仓至57700股

修复与改进:
  [Bug1] do_buy/do_sell 返回 True/False; 状态仅在成功时推进
  [Bug2] do_buy 下单前检查可用资金
  [Bug3] 峰值/谷值使用K线最高/最低值追踪（更准确）
  [Bug4] 持仓key兼容查找 get_holding_amount()
  [改进1] 每日最多2次完整循环（4笔交易）
  [改进2] 09:30起静默追踪日内极值; 09:45后才开始执行交易
  [接口升级] 行情接口get_market_data -> get_market_data_ex;
             init()用download_history_data补充昨日及以前历史数据
'''

# ============================================================
# 状态枚举
# ============================================================
STATE_IDLE                 = 'IDLE'                 # 等待 ±3% 触发
STATE_TRACKING_HIGH        = 'TRACKING_HIGH'        # 冲高+3%, 追踪峰值
STATE_TRACKING_LOW         = 'TRACKING_LOW'         # 回落-3%, 追踪谷值
STATE_SOLD_TRACKING_LOW    = 'SOLD_TRACKING_LOW'    # 已卖出, 追踪谷值待回补
STATE_BOUGHT_TRACKING_HIGH = 'BOUGHT_TRACKING_HIGH' # 已买入, 追踪峰值待再卖


def init(ContextInfo):
    # ---- 策略参数 ----
    ContextInfo.LOTS          = 20000           # 每次做T股数
    ContextInfo.BASE_HOLDING  = 57700           # 底仓股数
    ContextInfo.TARGET_CODE   = '588170.SH'     # ETF代码
    ContextInfo.accountID     = '8890523082'

    # ---- 阈值参数 ----
    ContextInfo.UP_TRIGGER      = 0.03           # 冲高+3%触发
    ContextInfo.DOWN_TRIGGER    = 0.03           # 回落-3%触发
    ContextInfo.SELL_PULLBACK   = 0.012           # 规则2.1: 回撤1%卖出
    ContextInfo.BUY_BOUNCE      = 0.012           # 规则2.2: 反弹1%买入
    ContextInfo.SELL_CONTINUE   = 0.02          # 规则2.3: 继续下跌2%触发(旧)
    ContextInfo.REBUY_BOUNCE    = 0.005          # 规则2.3: 反弹0.5%回补(旧)
    ContextInfo.BUY_CONTINUE    = 0.02          # 规则2.4: 继续上涨2%触发(旧)
    ContextInfo.RESELL_PULLBACK = 0.005          # 规则2.4: 回调0.5%再卖(旧)
    ContextInfo.SELL_TRACK_TRIGGER = 0.015          # 规则2.3: 触发阈值(跌1.5%)
    ContextInfo.BUY_TRACK_TRIGGER  = 0.015          # 规则2.4: 触发阈值(涨1.5%)
    ContextInfo.REBUY_THRESH       = 0.004          # 规则2.3: 回补阈值(反弹0.4%)
    ContextInfo.RESELL_THRESH      = 0.004          # 规则2.4: 再卖阈值(回调0.4%)
    ContextInfo.SELL_CONTINUE_ALT  = 0.006        # 规则2.3: 备选触发(0.6%+10分钟)
    ContextInfo.BUY_CONTINUE_ALT   = 0.006        # 规则2.4: 备选触发(0.6%+10分钟)
    ContextInfo.CONTINUE_WAIT_BARS = 10           # 备选触发需等待K线数(10分钟)
    ContextInfo.PROTECTION_BUF  = 0.003          # 止损/平价保护缓冲阈值0.5%
    ContextInfo.BREAKEVEN_BUF_EARLY = 0.013      # 平价保护(第一次卖出后trade_count>1): 涨超卖出价1.3%
    ContextInfo.STOPLOSS_BUF_EARLY  = 0.012       # 止损保护(第一次买入后trade_count<3): 跌魄买入价1.2%

    # ---- 交易限制 ----
    ContextInfo.MAX_TRADES      = 4              # 最多2次循环 × (1买+1卖) = 4笔
    ContextInfo.START_TIME      = 945            # 09:45 策略激活
    ContextInfo.REBALANCE_TIME  = 1450           # 14:50 尾盘平仓
    ContextInfo.STOP_FLAT_TIME   = 1315           # 13:15 后持仓=底仓, 停止当日交易

    # ---- 状态变量 ----
    ContextInfo.state           = STATE_IDLE
    ContextInfo.open_price      = 0.0            # 当日开盘价
    ContextInfo.peak_price      = 0.0            # 追踪中的最高点
    ContextInfo.trough_price    = 0.0            # 追踪中的最低点
    ContextInfo.sell_price      = 0.0            # 上一次卖出价（规则2.3用）
    ContextInfo.buy_price       = 0.0            # 上一次买入价（规则2.4用）
    ContextInfo.open_recorded   = False          # 是否已记录开盘价
    ContextInfo.trade_count     = 0              # 今日交易笔数
    ContextInfo.intraday_high   = 0.0            # 日内最高价（静默追踪）
    ContextInfo.intraday_low    = 0.0            # 日内最低价（静默追踪）
    ContextInfo.current_holding = ContextInfo.BASE_HOLDING  # 内部记录当前持仓（模拟模式回退用）
    ContextInfo.base_bought = False                        # 底仓买入标志
    ContextInfo.last_trade_date = ''                        # 上一个交易日（检测新日用）
    ContextInfo.rebalanced = False                         # 尾盘平仓是否已执行
    ContextInfo.sell_drop_achieved = False                 # Rule 2.3: sell price dropped 2% from sell_price
    ContextInfo.buy_rise_achieved = False                  # Rule 2.4: buy price risen 2% from buy_price
    ContextInfo.prev_close      = 0.0            # 前一日收盘价(昨收价), 触发参考价
    ContextInfo.last_bar_close  = 0.0            # 上一根K线收盘(跨日传递昨收)
    ContextInfo.bars_since_sell = 0              # 卖出后经过的K线数
    ContextInfo.bars_since_buy  = 0              # 买入后经过的K线数

    # ---- 历史数据补充: 昨日及以前(供get_market_data_ex查询昨收价/历史K线) ----
    try:
        download_history_data(ContextInfo.TARGET_CODE, '1d', '20200101', '')          # 日线: 昨收价
        download_history_data(ContextInfo.TARGET_CODE, ContextInfo.period, '', '')    # 本周期: 增量补充
    except Exception as e:
        print('[warn] 历史数据补充失败(可在客户端手动补充):', e)

    ContextInfo.set_universe([ContextInfo.TARGET_CODE])


def handlebar(ContextInfo):
    # ---- 获取当前K线时间 ----
    d = ContextInfo.barpos
    date = timetag_to_datetime(ContextInfo.get_bar_timetag(d), '%Y-%m-%d %H:%M:%S')
    hhmm = int(date[11:13] + date[14:16])        # 如 0930, 1450

    print('时间:', date, '| 状态:', ContextInfo.state,
          '| 交易笔数:', ContextInfo.trade_count, '/', ContextInfo.MAX_TRADES)

    # ---- 获取行情数据 ----
    # [接口升级] get_market_data_ex: 返回 {股票代码: DataFrame}
    data = ContextInfo.get_market_data_ex(
        ['open', 'high', 'low', 'close'],
        stock_code=ContextInfo.get_universe(),
        start_time=timetag_to_datetime(ContextInfo.get_bar_timetag(d - 1), '%Y%m%d%H%M%S'),
        end_time=timetag_to_datetime(ContextInfo.get_bar_timetag(d), '%Y%m%d%H%M%S'),
        period=ContextInfo.period
    )
    df = data.get(ContextInfo.TARGET_CODE) if data else None
    if df is None and data:                     # 兜底: 返回key与代码格式不一致时取第一个
        df = next(iter(data.values()))
    if df is None or df.empty:
        return

    current_close = df.iloc[-1]['close']          # 当前收盘价
    current_high  = df.iloc[-1]['high']           # 当前K线最高值 [Bug3]
    current_low   = df.iloc[-1]['low']            # 当前K线最低值 [Bug3]
    if current_close <= 0 or current_high <= 0 or current_low <= 0:
        return

    # ============================================================
    # 第一步: 记录开盘价（仅首根K线记录一次）
    # ============================================================
    # 第零步: 检测新交易日，重置日内状态
    # ============================================================
    current_date = date[:10]
    if ContextInfo.last_trade_date != current_date:
        if ContextInfo.last_bar_close > 0:
            ContextInfo.prev_close = ContextInfo.last_bar_close
        else:
            # 首次运行: 从已下载的历史日线获取真实昨收价
            ContextInfo.prev_close = get_prev_close_from_history(ContextInfo, current_date)
        if ContextInfo.last_trade_date != '':
            print('========== 新交易日 %s: 状态重置 ==========' % current_date)
        ContextInfo.last_trade_date = current_date
        ContextInfo.state = STATE_IDLE
        ContextInfo.trade_count = 0
        ContextInfo.open_recorded = False
        ContextInfo.open_price = 0.0
        ContextInfo.intraday_high = 0.0
        ContextInfo.intraday_low = 0.0
        ContextInfo.peak_price = 0.0
        ContextInfo.trough_price = 0.0
        ContextInfo.sell_price = 0.0
        ContextInfo.buy_price = 0.0
        ContextInfo.rebalanced = False
        ContextInfo.sell_drop_achieved = False
        ContextInfo.buy_rise_achieved = False

    ContextInfo.last_bar_close = current_close

    if not ContextInfo.open_recorded:
        ContextInfo.open_price = df.iloc[-1]['open']
        ContextInfo.open_recorded = True
        print('当日开盘价:', ContextInfo.open_price, '| 昨收价:', ContextInfo.prev_close)

        # 底仓处理: 实盘已有持仓则跳过，回测/模拟则买入
        if not ContextInfo.base_bought:
            code = ContextInfo.get_universe()[0]
            holding = get_holdings(ContextInfo.accountID, 'STOCK')
            existing = get_holding_amount(holding, code)
            if existing > 0:
                ContextInfo.current_holding = existing
                print('[底仓] 已有持仓: %d股' % existing)
            else:
                order_shares(code, ContextInfo.BASE_HOLDING, 'fix', current_close, ContextInfo, ContextInfo.accountID)
                ContextInfo.current_holding = ContextInfo.BASE_HOLDING
                print('[底仓] 买入底仓: %d股 @ %.3f' % (ContextInfo.BASE_HOLDING, current_close))
            ContextInfo.base_bought = True

    open_price = ContextInfo.open_price
    if open_price <= 0:
        return

    ref_price = ContextInfo.prev_close if ContextInfo.prev_close > 0 else open_price
    change_ratio = (current_close - ref_price) / ref_price   # 相对昨收价的涨跌幅

    # ============================================================
    # 第二步: 14:50 尾盘平仓（规则3，始终执行）
    # ============================================================
    if hhmm >= ContextInfo.REBALANCE_TIME:
        rebalance_position(ContextInfo, current_close, date)
        return

    # ============================================================
    # 第三步: 判断是否允许交易 [改进2: 静默追踪]
    #   09:45前: 记录日内高低点，但不执行交易
    #   09:45后: 使用准确的日内极值，正常交易
    # ============================================================
    can_trade = (hhmm >= ContextInfo.START_TIME)
    if not can_trade:
        print('[静默] 09:45前仅追踪, 涨跌: %.2f%%' % (change_ratio * 100))

    # ============================================================
    # 第四步: 达到最大交易笔数则停止 [改进1]
    #   （仍允许峰值/谷值追踪）
    # ============================================================
    max_reached = (ContextInfo.trade_count >= ContextInfo.MAX_TRADES)
    if max_reached and can_trade:
        print('已达最大交易笔数(%d), 等待14:50平仓' % ContextInfo.MAX_TRADES)
        return

    # ---- 13:15 后持仓已平, 停止当日交易 ----
    if can_trade and hhmm >= ContextInfo.STOP_FLAT_TIME and ContextInfo.current_holding == ContextInfo.BASE_HOLDING:
        print('[%s] 13:15 后持仓已平, 停止当日交易' % date)
        return

    # ============================================================
    # 第五步: 状态机 - 做T核心逻辑
    #   - IDLE: 始终追踪日内极值; 仅09:45后进入追踪状态
    #   - 其他状态: 仅在09:45后通过交易进入
    # ============================================================
    state = ContextInfo.state

    # ----------------------------------------------------------
    # IDLE: 等待 ±3% 触发
    #   始终记录日内高低点; 仅09:45后才进入追踪状态
    # ----------------------------------------------------------
    if state == STATE_IDLE:
        # 静默追踪: 记录日内极值（始终执行）
        if current_high > ContextInfo.intraday_high:
            ContextInfo.intraday_high = current_high
        if ContextInfo.intraday_low <= 0 or current_low < ContextInfo.intraday_low:
            ContextInfo.intraday_low = current_low

        # 仅在允许交易时才进入追踪状态
        if can_trade and change_ratio >= ContextInfo.UP_TRIGGER:
            ContextInfo.state = STATE_TRACKING_HIGH
            # 用日内极值作为初始峰值（包含09:45前的数据）
            ContextInfo.peak_price = max(ContextInfo.intraday_high, current_high)
            print('触发上涨: 涨幅>=+3%%. 初始峰值(含日内):', ContextInfo.peak_price)

        elif can_trade and change_ratio <= -ContextInfo.DOWN_TRIGGER:
            ContextInfo.state = STATE_TRACKING_LOW
            # 用日内极值作为初始谷值（包含09:45前的数据）
            ContextInfo.trough_price = min(ContextInfo.intraday_low, current_low)
            print('触发下跌: 跌幅<=-3%%. 初始谷值(含日内):', ContextInfo.trough_price)

    # ----------------------------------------------------------
    # TRACKING_HIGH: 规则2.1 -- 追踪峰值, 回撤1%卖出
    # ----------------------------------------------------------
    elif state == STATE_TRACKING_HIGH:
        if current_high > ContextInfo.peak_price:
            ContextInfo.peak_price = current_high    # 用K线最高值更新峰值
            print('更新峰值:', ContextInfo.peak_price)

        pullback = (ContextInfo.peak_price - current_close) / ContextInfo.peak_price
        if pullback >= ContextInfo.SELL_PULLBACK:
            # [Bug1] 仅卖出成功才推进状态
            if do_sell(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.sell_price = current_close
                ContextInfo.state = STATE_SOLD_TRACKING_LOW
                ContextInfo.sell_drop_achieved = False
                ContextInfo.trough_price = current_low
                ContextInfo.bars_since_sell = 0
                ContextInfo.trade_count += 1
                print('卖出(规则2.1): 从峰值回撤%.2f%%. 卖出价: %.3f' %
                      (pullback * 100, current_close))

    # ----------------------------------------------------------
    # TRACKING_LOW: 规则2.2 -- 追踪谷值, 反弹1%买入
    # ----------------------------------------------------------
    elif state == STATE_TRACKING_LOW:
        if 0 < current_low < ContextInfo.trough_price:
            ContextInfo.trough_price = current_low   # 用K线最低值更新谷值
            print('更新谷值:', ContextInfo.trough_price)

        bounce = (current_close - ContextInfo.trough_price) / ContextInfo.trough_price
        if bounce >= ContextInfo.BUY_BOUNCE:
            # [Bug1] 仅买入成功才推进状态
            if do_buy(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.buy_price = current_close
                ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                ContextInfo.buy_rise_achieved = False
                ContextInfo.peak_price = current_high
                ContextInfo.bars_since_buy = 0
                ContextInfo.trade_count += 1
                print('买入(规则2.2): 从谷值反弹%.2f%%. 买入价: %.3f' %
                      (bounce * 100, current_close))

    # ----------------------------------------------------------
    # SOLD_TRACKING_LOW: 规则2.3 -- 触发: 跌1.5% 或 (0.6%+10分钟), 回补: 反弹0.4%
    #                    附加: 平价保护, 股价涨回卖出价上方则买回
    # ----------------------------------------------------------
    elif state == STATE_SOLD_TRACKING_LOW:
        ContextInfo.bars_since_sell += 1

        if 0 < current_low < ContextInfo.trough_price:
            ContextInfo.trough_price = current_low
            print('[rule2.3] update trough:', ContextInfo.trough_price)

        drop_from_sell = (ContextInfo.sell_price - current_close) / ContextInfo.sell_price

        # --- Phase 1: 触发条件 -- 跌1.5% 或 (0.6%且10分钟) ---
        if not ContextInfo.sell_drop_achieved:
            if ContextInfo.trade_count >= 3:
                # 第3笔起: 跌1.5% 或 (0.6%且10分钟)
                if drop_from_sell >= ContextInfo.SELL_TRACK_TRIGGER:
                    ContextInfo.sell_drop_achieved = True
                    print('[rule2.3] 1.5% drop triggered! Monitoring 0.4% bounce...')
                elif (drop_from_sell >= ContextInfo.SELL_CONTINUE_ALT and
                      ContextInfo.bars_since_sell >= ContextInfo.CONTINUE_WAIT_BARS):
                    ContextInfo.sell_drop_achieved = True
                    print('[rule2.3] 0.6%+10min triggered! Monitoring 0.4% bounce...')
            else:
                # 前2笔: 跌2%
                if drop_from_sell >= ContextInfo.SELL_CONTINUE:
                    ContextInfo.sell_drop_achieved = True
                    print('[rule2.3] 2% drop achieved! Monitoring 0.5% bounce...')

        # --- Phase 2a: 触发后 -- 从新底反弹0.4%买入 ---
        if ContextInfo.sell_drop_achieved:
            bounce_from_low = (current_close - ContextInfo.trough_price) / ContextInfo.trough_price
            print('[rule2.3] drop=%.2f%% bounce=%.2f%% trough=%.3f' %
                  (drop_from_sell * 100, bounce_from_low * 100, ContextInfo.trough_price))
            rebuy_thresh = ContextInfo.REBUY_THRESH if ContextInfo.trade_count >= 3 else ContextInfo.REBUY_BOUNCE
            if bounce_from_low >= rebuy_thresh:
                if do_buy(ContextInfo, ContextInfo.LOTS, current_close, date):
                    ContextInfo.buy_price = current_close
                    ContextInfo.sell_drop_achieved = False
                    ContextInfo.buy_rise_achieved = False
                    ContextInfo.bars_since_buy = 0
                    ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                    ContextInfo.peak_price = current_high
                    ContextInfo.trade_count += 1
                    print('[BUY rule2.3] bounce>=%.1f%%. rebuy: %.3f' % (rebuy_thresh * 100, current_close))

        # --- Phase 2b: 平价保护 ---
        elif ContextInfo.trade_count >= 3 and current_close >= ContextInfo.sell_price * (1 + ContextInfo.PROTECTION_BUF):
            if do_buy(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.buy_price = current_close
                ContextInfo.sell_drop_achieved = False
                ContextInfo.buy_rise_achieved = False
                ContextInfo.bars_since_buy = 0
                ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                ContextInfo.peak_price = current_high
                ContextInfo.trade_count += 1
                print('[BUY breakeven] trade#%d, price rose above sell+0.3%%. rebuy: %.3f' %
                      (ContextInfo.trade_count, current_close))
        # trade_count < 3 (第一次卖出后): 宽保护 +1.3%
        elif ContextInfo.trade_count < 3 and current_close >= ContextInfo.sell_price * (1 + ContextInfo.BREAKEVEN_BUF_EARLY):
            if do_buy(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.buy_price = current_close
                ContextInfo.sell_drop_achieved = False
                ContextInfo.buy_rise_achieved = False
                ContextInfo.bars_since_buy = 0
                ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                ContextInfo.peak_price = current_high
                ContextInfo.trade_count += 1
                print('[BUY breakeven_early] trade#%d, price rose above sell+1.3%%. rebuy: %.3f' %
                      (ContextInfo.trade_count, current_close))

    elif state == STATE_BOUGHT_TRACKING_HIGH:
        ContextInfo.bars_since_buy += 1

        if current_high > ContextInfo.peak_price:
            ContextInfo.peak_price = current_high
            print('[rule2.4] update peak:', ContextInfo.peak_price)

        rise_from_buy = (current_close - ContextInfo.buy_price) / ContextInfo.buy_price

        # --- Phase 1: 触发条件 -- 涨1.5% 或 (0.6%且10分钟) ---
        if not ContextInfo.buy_rise_achieved:
            if ContextInfo.trade_count >= 3:
                # 第3笔起: 涨1.5% 或 (0.6%且10分钟)
                if rise_from_buy >= ContextInfo.BUY_TRACK_TRIGGER:
                    ContextInfo.buy_rise_achieved = True
                    print('[rule2.4] 1.5% rise triggered! Monitoring 0.4% pullback...')
                elif (rise_from_buy >= ContextInfo.BUY_CONTINUE_ALT and
                      ContextInfo.bars_since_buy >= ContextInfo.CONTINUE_WAIT_BARS):
                    ContextInfo.buy_rise_achieved = True
                    print('[rule2.4] 0.6%+10min triggered! Monitoring 0.4% pullback...')
            else:
                # 前2笔: 涨2%
                if rise_from_buy >= ContextInfo.BUY_CONTINUE:
                    ContextInfo.buy_rise_achieved = True
                    print('[rule2.4] 2% rise achieved! Monitoring 0.5% pullback...')

        # --- Phase 2a: 触发后 -- 从新高回调0.4%卖出 ---
        if ContextInfo.buy_rise_achieved:
            pullback_from_high = (ContextInfo.peak_price - current_close) / ContextInfo.peak_price
            print('[rule2.4] rise=%.2f%% pullback=%.2f%% peak=%.3f' %
                  (rise_from_buy * 100, pullback_from_high * 100, ContextInfo.peak_price))
            resell_thresh = ContextInfo.RESELL_THRESH if ContextInfo.trade_count >= 3 else ContextInfo.RESELL_PULLBACK
            if pullback_from_high >= resell_thresh:
                if do_sell(ContextInfo, ContextInfo.LOTS, current_close, date):
                    ContextInfo.sell_price = current_close
                    ContextInfo.buy_rise_achieved = False
                    ContextInfo.sell_drop_achieved = False
                    ContextInfo.bars_since_sell = 0
                    ContextInfo.state = STATE_SOLD_TRACKING_LOW
                    ContextInfo.trough_price = current_low
                    ContextInfo.trade_count += 1
                    print('[SELL rule2.4] pullback>=%.1f%%. resell: %.3f' % (resell_thresh * 100, current_close))

        # --- Phase 2b: 止损保护 ---
        elif ContextInfo.trade_count >= 3 and current_close <= ContextInfo.buy_price * (1 - ContextInfo.PROTECTION_BUF):
            if do_sell(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.sell_price = current_close
                ContextInfo.buy_rise_achieved = False
                ContextInfo.sell_drop_achieved = False
                ContextInfo.bars_since_sell = 0
                ContextInfo.state = STATE_SOLD_TRACKING_LOW
                ContextInfo.trough_price = current_low
                ContextInfo.trade_count += 1
                print('[SELL stoploss] trade#%d, price fell below buy-0.3%%. resell: %.3f' %
                      (ContextInfo.trade_count, current_close))
        # trade_count < 3 (第一次买入后): 宽止损 -1.2%
        elif ContextInfo.trade_count < 3 and current_close <= ContextInfo.buy_price * (1 - ContextInfo.STOPLOSS_BUF_EARLY):
            if do_sell(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.sell_price = current_close
                ContextInfo.buy_rise_achieved = False
                ContextInfo.sell_drop_achieved = False
                ContextInfo.bars_since_sell = 0
                ContextInfo.state = STATE_SOLD_TRACKING_LOW
                ContextInfo.trough_price = current_low
                ContextInfo.trade_count += 1
                print('[SELL stoploss_early] trade#%d, price fell below buy-1.2%%. resell: %.3f' %
                      (ContextInfo.trade_count, current_close))


# ============================================================
# 交易执行函数
# ============================================================
def do_buy(ContextInfo, lots, price, date):
    """
    执行买入下单。
    [Bug2] 下单前检查可用资金是否充足。
    返回 True 表示成功, False 表示失败。
    """
    code = ContextInfo.get_universe()[0]

    # [Bug2] 检查可用资金
    available_funds = get_avaliable(ContextInfo.accountID, 'STOCK')
    cost = lots * price
    if 0 < available_funds < cost:  # 模拟模式返回0时不阻塞
        print('[买入失败] 资金不足: 需要 %.2f, 可用 %.2f' % (cost, available_funds))
        return False

    order_shares(code, lots, 'fix', price, ContextInfo, ContextInfo.accountID)
    ContextInfo.current_holding += lots  # 更新内部持仓记录
    print('[%s] >>> 买入 %s  %d股 @ %.3f' % (date, code, lots, price))
    return True


def do_sell(ContextInfo, lots, price, date):
    """
    执行卖出下单。
    [Bug1] 返回 True 表示成功, False 表示失败。
    """
    code = ContextInfo.get_universe()[0]
    holding = get_holdings(ContextInfo.accountID, 'STOCK')
    available = get_holding_amount(holding, code)    # [Bug4] 兼容多种key格式
    if available <= 0:  # 模拟模式回退到内部持仓
        available = ContextInfo.current_holding

    if available < lots:
        lots = available
        print('可卖持仓不足, 调整为:', lots)
    if lots <= 0:
        print('[卖出失败] 无可卖持仓')
        return False

    order_shares(code, -lots, 'fix', price, ContextInfo, ContextInfo.accountID)
    ContextInfo.current_holding -= lots  # 更新内部持仓记录
    print('[%s] >>> 卖出 %s  %d股 @ %.3f' % (date, code, lots, price))
    return True


def rebalance_position(ContextInfo, current_price, date):
    if ContextInfo.rebalanced:
        return
    """14:50 恢复底仓至 BASE_HOLDING 股。"""
    code = ContextInfo.get_universe()[0]
    holding = get_holdings(ContextInfo.accountID, 'STOCK')
    current_holding = get_holding_amount(holding, code)  # [Bug4] 兼容多种key格式
    if current_holding <= 0:  # 模拟模式回退到内部持仓
        current_holding = ContextInfo.current_holding

    diff = current_holding - ContextInfo.BASE_HOLDING
    if diff == 0:
        print('[%s] 14:50检查: 持仓=%d, 底仓=%d, 已平衡' %
              (date, current_holding, ContextInfo.BASE_HOLDING))
        ContextInfo.rebalanced = True
        return

    if diff > 0:
        # 持仓多于底仓, 卖出多余部分
        order_shares(code, -diff, 'fix', current_price, ContextInfo, ContextInfo.accountID)
        print('[%s] 14:50平仓: 卖出%d股, 恢复底仓%d' %
              (date, diff, ContextInfo.BASE_HOLDING))
        ContextInfo.current_holding = ContextInfo.BASE_HOLDING
        ContextInfo.rebalanced = True
    else:
        # 持仓少于底仓, 买入缺口部分（-diff为正数）
        order_shares(code, -diff, 'fix', current_price, ContextInfo, ContextInfo.accountID)
        print('[%s] 14:50平仓: 买入%d股, 恢复底仓%d' %
              (date, -diff, ContextInfo.BASE_HOLDING))
        ContextInfo.current_holding = ContextInfo.BASE_HOLDING
        ContextInfo.rebalanced = True


# ============================================================
# 工具函数
# ============================================================
def get_holding_amount(holding_dict, code):
    """
    [Bug4] 从持仓字典中获取持仓数量。
    兼容多种key格式: '588170.SH', '588170.SHSE', '588170.SSE', '588170' 等。
    """
    # 1) 精确匹配
    if code in holding_dict:
        return holding_dict[code]

    # 2) 按代码编号匹配
    code_num = code.split('.')[0]
    for key, val in holding_dict.items():
        if key.split('.')[0] == code_num:
            return val

    # 3) 子串匹配（最后手段）
    for key, val in holding_dict.items():
        if code_num in key:
            return val

    return 0


def get_prev_close_from_history(ContextInfo, current_date):
    """
    [接口升级] 从本地已下载的日K线中, 获取 current_date 之前最近一个交易日的收盘价。
    依赖 init() 中 download_history_data(..., '1d', ...) 补充的历史数据。
    获取失败返回 0.0, 由调用方回退到当日开盘价作为参考价。
    """
    try:
        data = ContextInfo.get_market_data_ex(
            ['close'], [ContextInfo.TARGET_CODE],
            start_time='20200101', end_time=current_date.replace('-', ''), period='1d'
        )
        df = data.get(ContextInfo.TARGET_CODE) if data else None
        if df is None or df.empty:
            return 0.0
        today = current_date.replace('-', '')
        rows = [(str(idx), row['close']) for idx, row in df.iterrows()]
        prev = [(ts, c) for ts, c in rows if ts[:8] < today]
        if not prev:
            return 0.0
        print('[昨收价] 取自历史日线 %s: %.3f' % (prev[-1][0][:8], float(prev[-1][1])))
        return float(prev[-1][1])
    except Exception as e:
        print('[warn] 获取历史昨收价失败, 将回退到开盘价:', e)
        return 0.0


def get_avaliable(accountid, datatype):
    """查询可用资金。"""
    result = 0
    resultlist = get_trade_detail_data(accountid, datatype, "ACCOUNT")
    for obj in resultlist:
        result = obj.m_dAvailable
    return result


def get_holdings(accountid, datatype):
    """查询持仓, 返回 {代码: 可用数量} 字典。"""
    holdinglist = {}
    resultlist = get_trade_detail_data(accountid, datatype, "POSITION")
    for obj in resultlist:
        holdinglist[obj.m_strInstrumentID + "." + obj.m_strExchangeID] = obj.m_nVolume
    return holdinglist
