# Builtins
import datetime as dt
from datetime import timedelta
import logging
import re
import threading
from logging import critical, error, info, warning, debug
from typing import Any, Dict, List, Tuple
import time

# External libraries
import dateutil.parser as parser
import pandas as pd
import pyotp
import robin_stocks.robinhood as rh
import yaml
import pytz 

# Submodule imports
import harvest.broker._base as base

class RobinhoodBroker(base.BaseBroker):

    def __init__(self, path=None):
        if path == None:
            path = './secret.yaml'
        with open(path, 'r') as stream:
            self.config = yaml.safe_load(stream)

        totp = pyotp.TOTP(self.config['robin_mfa']).now()
        debug(f"Current OTP: {totp}")
        login = rh.login(self.config['robin_username'],
                        self.config['robin_password'], store_session=True, mfa_code=totp)
        debug(login)
    
    def setup_run(self, watch: List[str], interval):
        self.watch = watch
        self.watch_stock = []
        self.watch_crypto = []
        self.watch_crypto_fmt = []

        self.interval = interval

        if interval not in ['1MIN', '5MIN', '15MIN', '30MIN']:
            raise Exception(f'Invalid interval {interval}')

        for s in self.watch:
            if s[0] == '@':
                self.watch_crypto_fmt.append(s[1:])
                self.watch_crypto.append(s)
            else:
                self.watch_stock.append(s)
        if len(self.watch_stock) > 0 and interval == '1MIN':
            raise Exception(f'Interval {interval} is only supported for crypto')
        
        if interval == '1MIN':
            self.interval_fmt = '15second'
            self.fetch_interval='1MIN'
        else:
            self.interval_fmt = '5minute'
            self.fetch_interval = '5MIN'
        
        self.option_cache = {}
         
    def run(self):
        self.cur_sec = -1
        self.cur_min = -1
        val = int(re.sub("[^0-9]", "", self.fetch_interval))
        # RH does not support per minute intervals, so instead 15second intervals are used
        # Note that 1MIN is only supported for crypto
        print("Running...")
        if self.interval == '1MIN':
            while 1:
                cur = dt.datetime.now()
                seconds = cur.second
                if seconds == 1 and seconds != self.cur_sec:
                    self._handler()
                self.cur_sec = seconds
        else:
            while 1:
                cur = dt.datetime.now()
                minutes = cur.minute
                if minutes % val == 0 and minutes != self.cur_min:
                    self._handler()
                self.cur_min = minutes

    def exit(self):
        self.option_cache = {}

    @base.BaseBroker._handler_wrap
    def _handler(self):
        df_dict = {}
        df_dict.update( self.fetch_latest_stock_price() )
        df_dict.update( self.fetch_latest_crypto_price())
      
        debug(df_dict)
        return df_dict
    
    @base.BaseBroker._exception_handler
    def fetch_price_history( self,
        last: dt.datetime, 
        today: dt.datetime, 
        interval: str='1MIN',
        symbol: str = None,
        count: int = 200):

        df = pd.DataFrame()

        if last >= today:
            return df
        
        if interval == '15SEC': # Not used
            if not symbol[0] == '@':
                raise Exception('15SEC interval is only allowed for crypto')
            inter = '15second'
        elif interval == '1MIN':
            if not symbol[0] == '@':
                raise Exception('MIN interval is only allowed for crypto')
            inter = '15second'
        elif interval == '5MIN':
            inter = '5minute'
        elif interval == '15MIN':
            inter = '5minute'
        elif interval == '30MIN':
            inter = '5minute'
        elif interval == '1DAY': 
            inter = 'day'
        else:
            return df
        
        delta = today - last 
        delta = delta.total_seconds()
        delta = delta / 3600
        if interval == 'DAY' and delta < 24:
            return df
        if delta < 1 or interval == '15SEC' or interval == '1MIN':
            span = 'hour'
        elif delta >=1 and delta < 24 or interval == '5MIN' or interval == '15MIN' or interval == '30MIN':
            span = 'day'
        elif delta >=24 and delta < 24 * 28:
            span = 'month'
        elif delta < 24 * 300:
            span = 'year'
        else:
            span='5year'
        
        if symbol[0] == '@':
            ret = rh.get_crypto_historicals(
                symbol[1:], 
                interval=inter, 
                span=span
                )
        else:
            ret = rh.get_stock_historicals(
                symbol, 
                interval=inter, 
                span=span
                )
        
        df = pd.DataFrame.from_dict(ret)
        df = self._format_df(df, [symbol], interval, False)
        debug(df)
        return df.iloc[-count:]

    @base.BaseBroker._exception_handler
    def fetch_latest_stock_price(self):
        df={}
        for s in self.watch_stock:
            ret = rh.get_stock_historicals(
                s,
                interval=self.interval_fmt, 
                span='day', 
                )
            if 'error' in ret:
                continue
            df_tmp = pd.DataFrame.from_dict(ret)
            df_tmp = self._format_df(df_tmp, [s], self.interval, True)
            df[s] = df_tmp
        
        debug(f"Stock df: {df}")
        return df        

    @base.BaseBroker._exception_handler
    def fetch_latest_crypto_price(self):
        df={}
        for s in self.watch_crypto_fmt:
            ret = rh.get_crypto_historicals(
                s, 
                interval=self.interval_fmt, 
                span='hour',
                )
            df_tmp = pd.DataFrame.from_dict(ret)
            df_tmp = self._format_df(df_tmp, ['@'+s], self.interval, True)
            df['@'+s] = df_tmp
        
        debug(f"Crypto df: {df}")
        return df        
    

    @base.BaseBroker._exception_handler
    def fetch_stock_positions(self):
        ret = rh.get_open_stock_positions()
        pos = []
        for r in ret:
            # 0 quantity means the order was not fulfilled yet
            if float(r['quantity']) < 0.0001:
                continue
            sym = rh.get_symbol_by_url(r['instrument'])
            pos.append(
                {
                    "symbol": sym,
                    "avg_price": float(r['average_buy_price']),
                    "quantity": float(r['quantity']),
                } 
            ) 
        return pos

    @base.BaseBroker._exception_handler
    def fetch_option_positions(self):
        ret = rh.get_open_option_positions()
        pos = []
        print(ret)
        for r in ret:
            # Get option data such as expiration date
            data = rh.get_option_instrument_data_by_id(r['option_id'])
            pos.append(
                {
                    "symbol": r['chain_symbol'],
                    "avg_price": float(r['average_price']) / float(r['trade_value_multiplier']),
                    "quantity": float(r['quantity']),
                    "multiplier": float(r['trade_value_multiplier']),
                    "exp_date": data['expiration_date'],
                    "strike_price": float(data['strike_price']),
                    "type":data['type'],
                } 
            )
            date = data['expiration_date']
            date = dt.datetime.strptime(date, '%Y-%m-%d')
            pos[-1]["occ_symbol"] = self.data_to_occ(r['chain_symbol'], date, data['type'], float(data['strike_price']))
    
        return pos 
    
    @base.BaseBroker._exception_handler
    def fetch_crypto_positions(self, key=None):
        ret = rh.get_crypto_positions()
        pos = []
        for r in ret:
            debug(r)
            # Note: It seems Robinhood misspelled 'cost_bases',
            # it should be 'cost_basis'
            if len(r['cost_bases']) <= 0:
                continue
            qty = float(r['cost_bases'][0]['direct_quantity'])
            if qty < 0.0000000001:
                continue
            
            pos.append(
                {
                    "symbol": '@' + r['currency']['code'],
                    "avg_price": float(r['cost_bases'][0]['direct_cost_basis']) / qty,
                    "quantity": qty,
                } 
            )
        debug(f"fetch_crypto: {pos}")
        return pos 
    
    @base.BaseBroker._exception_handler
    def update_option_positions(self, positions: List[Any]):
        debug(f"Option positions: {positions}")
        for r in positions:
            sym, date, type, price = self.occ_to_data(r['occ_symbol'])
            upd = rh.get_option_market_data(
                sym,
                date.strftime('%Y-%m-%d'),
                str(price),
                type
            )
            debug(upd)
            upd = upd[0][0]
            r["current_price"] = float(upd['adjusted_mark_price'])
            r["market_value"] = float(upd['adjusted_mark_price']) * r['quantity']
            r["cost_basis"] = r['avg_price'] * r['quantity']

    @base.BaseBroker._exception_handler
    def fetch_account(self):
        ret = rh.load_phoenix_account()
        ret = {
            "equity": float(ret['equities']['equity']['amount']),
            "cash": float(ret['uninvested_cash']['amount']),
            "buying_power": float(ret['account_buying_power']['amount']),   
            "multiplier": float(-1)
        }
        debug(ret)
        return ret

    @base.BaseBroker._exception_handler
    def fetch_stock_order_status(self, id):
        ret = rh.get_stock_order_info(id)
        return {
            "type":"STOCK",
            "id":ret['id'],
            "symbol":ret['symbol'],
            "quantity":ret['qty'],
            "filled_quantity":ret['filled_qty'],
            "side":ret['side'],
            "time_in_force":ret['time_in_force'],
            "status":ret['status'],
        }
    
    @base.BaseBroker._exception_handler
    def fetch_option_order_status(self, id):
        ret = rh.get_option_order_info(id)
        debug(ret)
        return {
            "type":"OPTION",
            "id":ret['id'],
            "symbol":ret['chain_symbol'],
            "qty":ret['quantity'],
            "filled_qty":ret['processed_quantity'],
            "side":ret['legs'][0]['side'],
            "time_in_force":ret['time_in_force'],
            "status":ret['state'],
        }
    
    @base.BaseBroker._exception_handler
    def fetch_crypto_order_status(self, id):
        ret = rh.get_crypto_order_info(id)
        debug(ret)
        return {
            "type":"CRYPTO",
            "id":ret['id'],
            "qty":float(ret['quantity']),
            "filled_qty":float(ret['cumulative_quantity']),
            "filled_price":float(ret['executions'][0]['effective_price']) if len(ret['executions']) else 0,
            "filled_cost":float(ret['rounded_executed_notional']),
            "side":ret['side'],
            "time_in_force":ret['time_in_force'],
            "status":ret['state'],
        }
    
    @base.BaseBroker._exception_handler
    def fetch_order_queue(self):
        queue = []
        ret = rh.get_all_open_stock_orders()
        for r in ret:
            #print(r)
            sym = rh.get_symbol_by_url(r['instrument'])
            queue.append({
                "type":"STOCK",
                "symbol":sym,
                "quantity":r['quantity'],
                "filled_qty":r['cumulative_quantity'],
                "id":r['id'],
                "time_in_force":r['time_in_force'],
                "status":r['state'],
                
                "side": r['side']
            } )
        
        ret = rh.get_all_open_option_orders()
        for r in ret:
            #print(r)
            legs = []
            for l in r['legs']:
                legs.append({
                    "id": l['id'],
                    "side": l['side']
                })
            queue.append({
                "type":"OPTION",
                "symbol":r['chain_symbol'],
                "quantity":r['quantity'],
                "filled_qty":r['processed_quantity'],
                "id":r['id'],
                "time_in_force":r['time_in_force'],
                "status":r['state'],
                                
                "legs":legs,
            } )
        ret = rh.get_all_open_crypto_orders()
        for r in ret:
            debug(r)
            sym = rh.get_symbol_by_url(r['instrument'])
            queue.append({
                "type":"CRYPTO",
                "symbol":sym,
                "quantity":r['quantity'],
                "filled_qty":r['cumulative_quantity'],
                "id":r['id'],
                "time_in_force":r['time_in_force'],
                "status":r['state'],
                "side": r['side']
            } )
        return queue
    
    @base.BaseBroker._exception_handler
    def fetch_chain_info(self, symbol: str):
        ret = rh.get_chains(symbol)
        return {
            "id": ret["id"], 
            "exp_dates": ret["expiration_dates"],
            "multiplier": ret["trade_value_multiplier"], 
        }    

    @base.BaseBroker._exception_handler
    def fetch_chain_data(self, symbol: str):

        if bool(self.option_cache) and symbol in self.option_cache:
            return self.option_cache[symbol]
        
        ret = rh.find_tradable_options(symbol)
        exp_date = []
        strike = []
        type = []
        id = []
        occ = []
        for entry in ret:
            date = entry["expiration_date"]
            date = dt.datetime.strptime(date, '%Y-%m-%d')
            date = pytz.utc.localize(date)
            exp_date.append(date)
            price = float(entry["strike_price"])
            strike.append(price)
            type.append(entry["type"])
            id.append(entry["id"])    

            occ.append(self.data_to_occ(symbol, date, type[-1], price))

        df = pd.DataFrame({'occ_symbol':occ,'exp_date':exp_date,'strike':strike,'type':type,'id':id})
        df = df.set_index('occ_symbol')

        self.option_cache[symbol] = df
        return df
    
    @base.BaseBroker._exception_handler
    def fetch_option_market_data(self, symbol: str):
      
        sym, date, type, price = self.occ_to_data(symbol)
        debug(f"Converted:{sym},{date},{type},{price}")
        ret = rh.get_option_market_data(
            sym,
            date.strftime('%Y-%m-%d'),
            str(price),
            type
        )
        ret = ret[0][0]
        return {
                'price': float(ret['adjusted_mark_price']),
                'ask': float(ret['ask_price']),
                'bid': float(ret['bid_price'])
            }

    # Order functions are not wrapped in the exception handler to prevent duplicate 
    # orders from being made. 
    def order_limit(self, 
        side: str, 
        symbol: str,
        quantity: float, 
        limit_price: float, 
        in_force: str='gtc', 
        extended: bool=False):
        try:
            if symbol[0] == '@':
                symbol = symbol[1:]
                if side == 'buy':
                    ret = rh.order_buy_crypto_limit(
                        symbol = symbol,
                        quantity = quantity,
                        timeInForce=in_force,
                        limitPrice=limit_price,
                    
                    )
                else:
                    ret = rh.order_sell_crypto_limit(
                        symbol = symbol,
                        quantity = quantity,
                        timeInForce=in_force,
                        limitPrice=limit_price,
                    )
                typ = 'CRYPTO'
            else:
                if side == 'buy':
                    ret = rh.order_buy_limit(
                        symbol = symbol,
                        quantity = quantity,
                        timeInForce=in_force,
                        limitPrice=limit_price,
                    
                    )
                else:
                    ret = rh.order_sell_limit(
                        symbol = symbol,
                        quantity = quantity,
                        timeInForce=in_force,
                        limitPrice=limit_price,
                    
                    )
                typ = 'STOCK'
            debug(ret)
            return {
                "type":typ,
                "id":ret['id'],
                "symbol":symbol,
                }
        except:
            raise Exception("Error while placing order")
    
    def order_option_limit(self, side: str, symbol: str, quantity: int, limit_price: float, option_type, exp_date: dt.datetime, strike, in_force: str='gtc'):
        try:
            if side == 'buy':
                ret = rh.order_buy_option_limit(
                    positionEffect='open',
                    creditOrDebit='debit',
                    price=limit_price,
                    symbol=symbol,
                    quantity=quantity,
                    expirationDate=exp_date.strftime('%Y-%m-%d'),
                    strike=strike,
                    optionType=option_type,
                    timeInForce=in_force,
                )
            else:
                ret = rh.order_sell_option_limit(
                    positionEffect='close',
                    creditOrDebit='credit',
                    price=limit_price,
                    symbol=symbol,
                    quantity=quantity,
                    expirationDate=exp_date.strftime('%Y-%m-%d'),
                    strike=strike,
                    optionType=option_type,
                    timeInForce=in_force,
                )
            debug(ret)
            if 'detail' in ret:
                Exception(f"Robinhood returned the following error:\n{ret['detail']}")
            return {
                "type":'OPTION',
                "id":ret['id'],
                "symbol":symbol,
                }
        except:
            raise Exception("Runtime error while placing order")
    
    def _format_df(self, df: pd.DataFrame, watch: List[str], interval: str, latest: bool=False):
        # Robinhood returns offset-aware timestamps based on timezone GMT-0, or UTC
        df['timestamp'] = pd.to_datetime(df['begins_at'])
        df = df.set_index(['timestamp'])
        df = df.drop(['begins_at'], axis=1)
        df = df.rename(columns={"open_price": "open", "close_price": "close", "high_price" : "high", "low_price" : "low"})
        df = df[["open", "close", "high", "low", "volume"]].astype(float)
    
        if interval == '1MIN':
            if latest:
                while df.index[-1].second != 45:
                    df = df.iloc[:-1]
                df = df[-4:]
            op_dict = {
                'open': 'first',
                'high':'max',
                'low':'min',
                'close':'last',
                'volume':'sum'
            }
            df = df.resample('1T').agg(op_dict)
            df = df[df['open'].notna()]
        elif latest:
            df = df.iloc[[-1]]
        
        df.columns = pd.MultiIndex.from_product([watch, df.columns])

        return df