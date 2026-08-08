# coding:gbk
'''
Intraday T+0 Strategy for ETF 588170.SH
Base holding: 57700 shares

Trading Rules (buy low, sell high):
  Rule 2.1: Price >= open+3%, track peak; pullback 1% from peak -> sell 20000
  Rule 2.2: Price <= open-3%, track trough; bounce 1% from trough -> buy 20000
  Rule 2.3: After sell, if price drops 2.8% from sell price,
            track trough; bounce 0.8% -> buy 20000
  Rule 2.4: After buy, if price rises 2.8% from buy price,
            track peak; pullback 0.8% -> sell 20000
  Rule 3:   14:50 rebalance to base holding 57700

Fixes & Improvements:
  [Bug1] do_buy/do_sell return True/False; state advances only on success
  [Bug2] do_buy checks available funds before ordering
  [Bug3] Peak/trough tracked with intrabar high/low (more accurate)
  [Bug4] Robust holding key lookup via get_holding_amount()
  [Imp1] Max 2 complete trade cycles per day (4 individual trades)
  [Imp2] Silent tracking from market open; trade execution starts at 09:50
'''

# ============================================================
# State Enum
# ============================================================
STATE_IDLE                 = 'IDLE'                 # Waiting for +/-3%
STATE_TRACKING_HIGH        = 'TRACKING_HIGH'        # Above +3%, tracking peak
STATE_TRACKING_LOW         = 'TRACKING_LOW'         # Below -3%, tracking trough
STATE_SOLD_TRACKING_LOW    = 'SOLD_TRACKING_LOW'    # Sold, tracking trough
STATE_BOUGHT_TRACKING_HIGH = 'BOUGHT_TRACKING_HIGH' # Bought, tracking peak


def init(ContextInfo):
    # ---- Strategy Parameters ----
    ContextInfo.LOTS          = 20000           # Shares per T operation
    ContextInfo.BASE_HOLDING  = 57700           # Base position
    ContextInfo.TARGET_CODE   = '588170.SH'     # ETF code
    ContextInfo.accountID     = 'testS'

    # ---- Thresholds ----
    ContextInfo.UP_TRIGGER      = 0.03           # +3% trigger
    ContextInfo.DOWN_TRIGGER    = 0.03           # -3% trigger
    ContextInfo.SELL_PULLBACK   = 0.01           # Rule 2.1: 1% pullback to sell
    ContextInfo.BUY_BOUNCE      = 0.01           # Rule 2.2: 1% bounce to buy
    ContextInfo.SELL_CONTINUE   = 0.028          # Rule 2.3: 2.8% continue drop
    ContextInfo.REBUY_BOUNCE    = 0.008          # Rule 2.3: 0.8% bounce to rebuy
    ContextInfo.BUY_CONTINUE    = 0.028          # Rule 2.4: 2.8% continue rise
    ContextInfo.RESELL_PULLBACK = 0.008          # Rule 2.4: 0.8% pullback to resell

    # ---- Trade Limits ----
    ContextInfo.MAX_TRADES      = 4              # 2 cycles x (1 buy + 1 sell)
    ContextInfo.START_TIME      = 950            # 09:50 activation
    ContextInfo.REBALANCE_TIME  = 1450           # 14:50 rebalance

    # ---- State Variables ----
    ContextInfo.state           = STATE_IDLE
    ContextInfo.open_price      = 0.0
    ContextInfo.peak_price      = 0.0
    ContextInfo.trough_price    = 0.0
    ContextInfo.sell_price      = 0.0
    ContextInfo.buy_price       = 0.0
    ContextInfo.open_recorded   = False
    ContextInfo.trade_count     = 0              # Individual trades today
    ContextInfo.intraday_high   = 0.0            # Day high (silent tracking)
    ContextInfo.intraday_low    = 0.0            # Day low (silent tracking)

    ContextInfo.set_universe([ContextInfo.TARGET_CODE])


def handlebar(ContextInfo):
    # ---- Get current bar time ----
    d = ContextInfo.barpos
    date = timetag_to_datetime(ContextInfo.get_bar_timetag(d), '%Y-%m-%d %H:%M:%S')
    hhmm = int(date[11:13] + date[14:16])        # e.g. 0930, 1450

    print('Time:', date, '| State:', ContextInfo.state,
          '| Trades:', ContextInfo.trade_count, '/', ContextInfo.MAX_TRADES)

    # ---- Get market data ----
    df = ContextInfo.get_market_data(
        ['open', 'high', 'low', 'close'],
        stock_code=ContextInfo.get_universe(),
        start_time=timetag_to_datetime(ContextInfo.get_bar_timetag(d - 1), '%Y%m%d%H%M%S'),
        end_time=timetag_to_datetime(ContextInfo.get_bar_timetag(d), '%Y%m%d%H%M%S'),
        period=ContextInfo.period
    )
    if df.empty:
        return

    current_close = df.iloc[-1]['close']
    current_high  = df.iloc[-1]['high']          # [Bug3] use intrabar high
    current_low   = df.iloc[-1]['low']           # [Bug3] use intrabar low
    if current_close <= 0 or current_high <= 0 or current_low <= 0:
        return

    # ============================================================
    # Step 1: Record open price (first bar only)
    # ============================================================
    if not ContextInfo.open_recorded:
        ContextInfo.open_price = df.iloc[-1]['open']
        ContextInfo.open_recorded = True
        print('Open price recorded:', ContextInfo.open_price)

    open_price = ContextInfo.open_price
    if open_price <= 0:
        return

    change_ratio = (current_close - open_price) / open_price

    # ============================================================
    # Step 2: 14:50 rebalance (Rule 3) -- always runs
    # ============================================================
    if hhmm >= ContextInfo.REBALANCE_TIME:
        rebalance_position(ContextInfo, current_close, date)
        return

    # ============================================================
    # Step 3: Determine if trading is allowed [Imp2: silent tracking]
    #   Before 09:50: track peaks/troughs but DO NOT trade
    #   After  09:50: trade normally with accurate peak/trough data
    # ============================================================
    can_trade = (hhmm >= ContextInfo.START_TIME)
    if not can_trade:
        print('[SILENT] Before 09:50, tracking only. change: %.2f%%' % (change_ratio * 100))

    # ============================================================
    # Step 4: Max trades reached -- stop trading [Imp1]
    #   (still allow peak/trough tracking)
    # ============================================================
    max_reached = (ContextInfo.trade_count >= ContextInfo.MAX_TRADES)
    if max_reached and can_trade:
        print('Max trades (%d) reached, waiting for 14:50 rebalance' % ContextInfo.MAX_TRADES)
        return

    # ============================================================
    # Step 5: State machine -- core T+0 logic
    #   - IDLE: always tracks intraday high/low; enters tracking
    #           states only after 09:50 with accurate peak/trough
    #   - Other states: only reachable after 09:50 (via trade)
    # ============================================================
    state = ContextInfo.state

    # ----------------------------------------------------------
    # IDLE: wait for +/-3% trigger
    #   Always track intraday high/low for later use
    #   Only enter tracking states after 09:50
    # ----------------------------------------------------------
    if state == STATE_IDLE:
        # Silent tracking: record intraday extremes (always)
        if current_high > ContextInfo.intraday_high:
            ContextInfo.intraday_high = current_high
        if ContextInfo.intraday_low <= 0 or current_low < ContextInfo.intraday_low:
            ContextInfo.intraday_low = current_low

        # Only enter tracking states when trading is allowed
        if can_trade and change_ratio >= ContextInfo.UP_TRIGGER:
            ContextInfo.state = STATE_TRACKING_HIGH
            # Use accurate intraday high as initial peak
            ContextInfo.peak_price = max(ContextInfo.intraday_high, current_high)
            print('TRIGGER UP: change >= +3%. Peak (with intraday):', ContextInfo.peak_price)

        elif can_trade and change_ratio <= -ContextInfo.DOWN_TRIGGER:
            ContextInfo.state = STATE_TRACKING_LOW
            # Use accurate intraday low as initial trough
            ContextInfo.trough_price = min(ContextInfo.intraday_low, current_low)
            print('TRIGGER DOWN: change <= -3%. Trough (with intraday):', ContextInfo.trough_price)

    # ----------------------------------------------------------
    # TRACKING_HIGH: Rule 2.1 -- track peak, sell on 1% pullback
    # ----------------------------------------------------------
    elif state == STATE_TRACKING_HIGH:
        if current_high > ContextInfo.peak_price:
            ContextInfo.peak_price = current_high    # [Bug3] update with high
            print('New peak:', ContextInfo.peak_price)

        pullback = (ContextInfo.peak_price - current_close) / ContextInfo.peak_price
        if pullback >= ContextInfo.SELL_PULLBACK:
            if do_sell(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.sell_price = current_close
                ContextInfo.state = STATE_SOLD_TRACKING_LOW
                ContextInfo.trough_price = current_low
                ContextInfo.trade_count += 1
                print('SELL (Rule 2.1): pullback %.2f%% from peak. Price: %.3f' %
                      (pullback * 100, current_close))

    # ----------------------------------------------------------
    # TRACKING_LOW: Rule 2.2 -- track trough, buy on 1% bounce
    # ----------------------------------------------------------
    elif state == STATE_TRACKING_LOW:
        if 0 < current_low < ContextInfo.trough_price:
            ContextInfo.trough_price = current_low   # [Bug3] update with low
            print('New trough:', ContextInfo.trough_price)

        bounce = (current_close - ContextInfo.trough_price) / ContextInfo.trough_price
        if bounce >= ContextInfo.BUY_BOUNCE:
            if do_buy(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.buy_price = current_close
                ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                ContextInfo.peak_price = current_high
                ContextInfo.trade_count += 1
                print('BUY (Rule 2.2): bounce %.2f%% from trough. Price: %.3f' %
                      (bounce * 100, current_close))

    # ----------------------------------------------------------
    # SOLD_TRACKING_LOW: Rule 2.3 -- wait 2.8% drop, buy on 0.8% bounce
    #                    Also: breakeven protection if price >= sell_price
    # ----------------------------------------------------------
    elif state == STATE_SOLD_TRACKING_LOW:
        if 0 < current_low < ContextInfo.trough_price:
            ContextInfo.trough_price = current_low
            print('New trough (post-sell):', ContextInfo.trough_price)

        drop_from_sell = (ContextInfo.sell_price - current_close) / ContextInfo.sell_price

        if drop_from_sell >= ContextInfo.SELL_CONTINUE:
            # Price dropped 2.8%+ from sell price, now watch for 0.8% bounce
            bounce_from_low = (current_close - ContextInfo.trough_price) / ContextInfo.trough_price
            if bounce_from_low >= ContextInfo.REBUY_BOUNCE:
                if do_buy(ContextInfo, ContextInfo.LOTS, current_close, date):
                    ContextInfo.buy_price = current_close
                    ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                    ContextInfo.peak_price = current_high
                    ContextInfo.trade_count += 1
                    print('BUY (Rule 2.3): dropped 2.8%%+, bounced 0.8%%. Price: %.3f' % current_close)

        elif current_close >= ContextInfo.sell_price:
            # Breakeven protection: price recovered above sell price
            if do_buy(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.buy_price = current_close
                ContextInfo.state = STATE_BOUGHT_TRACKING_HIGH
                ContextInfo.peak_price = current_high
                ContextInfo.trade_count += 1
                print('BUY (breakeven): price back above sell price. Price: %.3f' % current_close)

    # ----------------------------------------------------------
    # BOUGHT_TRACKING_HIGH: Rule 2.4 -- wait 2.8% rise, sell on 0.8% pullback
    #                       Also: stop-loss if price <= buy_price
    # ----------------------------------------------------------
    elif state == STATE_BOUGHT_TRACKING_HIGH:
        if current_high > ContextInfo.peak_price:
            ContextInfo.peak_price = current_high
            print('New peak (post-buy):', ContextInfo.peak_price)

        rise_from_buy = (current_close - ContextInfo.buy_price) / ContextInfo.buy_price

        if rise_from_buy >= ContextInfo.BUY_CONTINUE:
            # Price risen 2.8%+ from buy price, now watch for 0.8% pullback
            pullback_from_high = (ContextInfo.peak_price - current_close) / ContextInfo.peak_price
            if pullback_from_high >= ContextInfo.RESELL_PULLBACK:
                if do_sell(ContextInfo, ContextInfo.LOTS, current_close, date):
                    ContextInfo.sell_price = current_close
                    ContextInfo.state = STATE_SOLD_TRACKING_LOW
                    ContextInfo.trough_price = current_low
                    ContextInfo.trade_count += 1
                    print('SELL (Rule 2.4): risen 2.8%%+, pulled back 0.8%%. Price: %.3f' % current_close)

        elif current_close <= ContextInfo.buy_price:
            # Stop-loss protection: price dropped to/below buy price
            if do_sell(ContextInfo, ContextInfo.LOTS, current_close, date):
                ContextInfo.sell_price = current_close
                ContextInfo.state = STATE_SOLD_TRACKING_LOW
                ContextInfo.trough_price = current_low
                ContextInfo.trade_count += 1
                print('SELL (stop-loss): price dropped below buy price. Price: %.3f' % current_close)


# ============================================================
# Trade execution functions
# ============================================================
def do_buy(ContextInfo, lots, price, date):
    """
    Execute buy order.
    [Bug2] Checks available funds before ordering.
    Returns True on success, False on failure.
    """
    code = ContextInfo.get_universe()[0]

    # [Bug2] Check available funds
    available_funds = get_avaliable(ContextInfo.accountID, 'STOCK')
    cost = lots * price
    if available_funds < cost:
        print('[FAIL BUY] Insufficient funds: need %.2f, available %.2f' % (cost, available_funds))
        return False

    order_shares(code, lots, 'fix', price, ContextInfo, ContextInfo.accountID)
    print('[%s] >>> BUY  %s  %d shares @ %.3f' % (date, code, lots, price))
    return True


def do_sell(ContextInfo, lots, price, date):
    """
    Execute sell order.
    [Bug1] Returns True on success, False on failure.
    """
    code = ContextInfo.get_universe()[0]
    holding = get_holdings(ContextInfo.accountID, 'STOCK')
    available = get_holding_amount(holding, code)    # [Bug4] robust lookup

    if available < lots:
        lots = available
        print('Available holdings insufficient, adjusted to:', lots)
    if lots <= 0:
        print('[FAIL SELL] No available holdings to sell')
        return False

    order_shares(code, -lots, 'fix', price, ContextInfo, ContextInfo.accountID)
    print('[%s] >>> SELL %s  %d shares @ %.3f' % (date, code, lots, price))
    return True


def rebalance_position(ContextInfo, current_price, date):
    """14:50 rebalance to restore base holding."""
    code = ContextInfo.get_universe()[0]
    holding = get_holdings(ContextInfo.accountID, 'STOCK')
    current_holding = get_holding_amount(holding, code)  # [Bug4] robust lookup

    diff = current_holding - ContextInfo.BASE_HOLDING
    if diff == 0:
        print('[%s] 14:50 check: holding=%d, base=%d, balanced' %
              (date, current_holding, ContextInfo.BASE_HOLDING))
        return

    if diff > 0:
        # Over base, sell excess
        order_shares(code, -diff, 'fix', current_price, ContextInfo, ContextInfo.accountID)
        print('[%s] 14:50 REBALANCE: SELL %d shares, restore to %d' %
              (date, diff, ContextInfo.BASE_HOLDING))
    else:
        # Under base, buy deficit (-diff is positive)
        order_shares(code, -diff, 'fix', current_price, ContextInfo, ContextInfo.accountID)
        print('[%s] 14:50 REBALANCE: BUY %d shares, restore to %d' %
              (date, -diff, ContextInfo.BASE_HOLDING))


# ============================================================
# Utility functions
# ============================================================
def get_holding_amount(holding_dict, code):
    """
    [Bug4] Get holding amount from dict.
    Compatible with multiple key formats:
      '588170.SH', '588170.SHSE', '588170.SSE', '588170', etc.
    """
    # 1) Direct match
    if code in holding_dict:
        return holding_dict[code]

    # 2) Fuzzy match by instrument number
    code_num = code.split('.')[0]
    for key, val in holding_dict.items():
        if key.split('.')[0] == code_num:
            return val

    # 3) Substring match as last resort
    for key, val in holding_dict.items():
        if code_num in key:
            return val

    return 0


def get_avaliable(accountid, datatype):
    """Query available cash."""
    result = 0
    resultlist = get_trade_detail_data(accountid, datatype, "ACCOUNT")
    for obj in resultlist:
        result = obj.m_dAvailable
    return result


def get_holdings(accountid, datatype):
    """Query holdings. Returns {code: available_volume}."""
    holdinglist = {}
    resultlist = get_trade_detail_data(accountid, datatype, "POSITION")
    for obj in resultlist:
        holdinglist[obj.m_strInstrumentID + "." + obj.m_strExchangeID] = obj.m_nCanUseVolume
    return holdinglist
