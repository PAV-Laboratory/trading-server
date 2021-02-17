"""
trading-server is a multi-asset, multi-strategy, event-driven execution
and backtesting platform (OEMS) for trading common markets.

Copyright (C) 2020  Sam Breznikar <sam@sdbgroup.io>

Licensed under GNU General Public License 3.0 or later.

Some rights reserved. See LICENSE.md, AUTHORS.md.
"""

from trade_types import SingleInstrumentTrade, Order, Position, TradeID
from event_types import OrderEvent, FillEvent

import numpy as np
import traceback

import matplotlib
matplotlib.use('qt5agg')

import mplfinance as mpl
import pymongo
import queue
import time
import json
import sys
import os


class Portfolio:
    """
    Portfolio manages the net holdings for all models, issuing order events
    and reacting to fill events to open and close positions as strategies
    dictate.

    Capital allocations to strategies and risk parameters are defined here.
    """

    MAX_SIMULTANEOUS_POSITIONS = 1
    MAX_CORRELATED_POSITIONS = 2
    MAX_ACCEPTED_DRAWDOWN = 25  # Percentage as integer.
    RISK_PER_TRADE = 1          # Percentage as integer OR 'KELLY'
    DEFAULT_STOP = 2            # Default (%) stop distance if none provided.
    SNAPSHOT_SIZE = 100         # Lookback period for trade snapshot images

    def __init__(self, exchanges, logger, db_other, db_client, models,
                 telegram):
        self.exchanges = {i.get_name(): i for i in exchanges}
        self.logger = logger
        self.db_other = db_other
        self.db_client = db_client
        self.models = models
        self.telegram = telegram

        self.trades_save_to_db = queue.Queue(0)
        self.id_gen = TradeID(db_other)
        self.pf = self.load_portfolio()
        self.verify_portfolio_state(self.pf)

    def new_signal(self, events, event):
        """
        Interpret incoming signal events to produce Order Events.

        Args:
            events: event queue object.
            event: new market event.

        Returns:
           None.

        Raises:
            None.
        """

        signal = event.get_signal_dict()
        orders = []

        # Generate sequential trade ID for new trade.
        trade_id = self.id_gen.new_id()

        # Handle single-instrument signals:
        if signal['instrument_count'] == 1:

            stop = self.calculate_stop_price(signal),
            size = self.calculate_position_size(stop[0],
                                                signal['entry_price'])
            # Entry order.
            orders.append(Order(
                self.logger,
                trade_id,               # Parent trade ID.
                None,                   # Order ID as used by venue.
                signal['symbol'],       # Instrument ticker code.
                signal['venue'],        # Venue name.
                signal['direction'],    # LONG or SHORT.
                size,                   # Size in native denomination.
                signal['entry_price'],  # Order price.
                signal['entry_type'],   # LIMIT MARKET STOP_LIMIT/MARKET.
                "ENTRY",                # ENTRY, TAKE_PROFIT, STOP.
                stop[0],                # Order invalidation price.
                False,                  # Trail.
                False,                  # Reduce-only order.
                False))                 # Post-only order.

            # Stop order.
            orders.append(Order(
                self.logger,
                trade_id,
                None,
                signal['symbol'],
                signal['venue'],
                event.inverse_direction(),
                size,
                stop[0],
                "STOP",
                "STOP",
                None,
                signal['trail'],
                True,
                False))

            # Take profit order(s).
            if signal['targets']:

                count = 1
                for target in signal['targets']:

                    # Label final TP order as "FINAL_TAKE_PROFIT".
                    tp_type = "TAKE_PROFIT" if count != len(signal['targets']) else "FINAL_TAKE_PROFIT"
                    count += 1

                    orders.append(Order(
                        self.logger,
                        trade_id,
                        None,
                        signal['symbol'],
                        signal['venue'],
                        event.inverse_direction(),
                        (size / 100) * target[1],
                        target[0],
                        "LIMIT",
                        tp_type,
                        stop[0],
                        False,
                        True,
                        False))

            # Set sequential order ID's, based on trade ID.
            count = 1
            for order in orders:
                order.order_id = str(trade_id) + "-" + str(count)
                count += 1

            # Parent trade object:
            trade = SingleInstrumentTrade(
                self.logger,
                signal['direction'],        # Direction
                signal['venue'],            # Venue name.
                signal['symbol'],           # Instrument ticker code.
                signal['strategy'],         # Model name.
                signal['entry_timestamp'],  # Signal timestamp.
                signal['timeframe'],        # Signal timeframe.
                signal['entry_price'],       # Entry price.
                None,                       # Position object.
                {str(i.get_order_dict()['order_id']): i.get_order_dict() for i in orders})  # noqa

            # Finalise trade object. Must be called to set ID + order count
            trade.set_batch_size_and_id(trade_id)

            # Queue the trade for storage.
            self.trades_save_to_db.put(trade.get_trade_dict())

            # Set order batch size and queue orders for execution.
            batch_size = len(orders)
            for order in orders:
                order.batch_size = batch_size

            within_risk_limits = self.within_risk_limits(signal)

            # Generate static image of trade setup.
            t_dict = trade.get_trade_dict()
            self.generate_trade_setup_image(
                t_dict, signal['op_data'], within_risk_limits)

            # Only raise orders and add to portfilio if within risk limits.
            if within_risk_limits:

                self.pf['trades'][str(trade_id)] = t_dict
                self.save_porfolio(self.pf)
                for order in orders:
                    events.put(OrderEvent(order.get_order_dict()))

        # TODO: handle multi-instrument, multi-venue trades.
        elif signal['instrument_count'] == 2:
            pass

        elif signal['instrument_count'] > 2:
            pass

        self.logger.info("Trade " + str(trade_id) + " registered.")

    def new_fill(self, fill_event):
        """
        Process incoming fill event, update position, trade and order state
        accordingly.

        Args:
            events: event queue object.
            event: new market event.

        Returns:
           None.

        Raises:
            None.
        """

        fill_conf = fill_event.get_order_conf()
        position = Position(fill_conf).get_pos_dict()
        t_id = str(position['trade_id'])

        if fill_conf['metatype'] == "ENTRY":

            # Create a position record and set trade to active.
            self.pf['trades'][t_id]['position'] = position
            self.pf['trades'][t_id]['active'] = True
            self.pf['total_active_trades'] += 1

        elif fill_conf['metatype'] == "STOP":

            # Update the now closed postiion, trade is done.
            size = self.pf['trades'][t_id]['position']['size']
            new_size = size - fill_conf['size']

            # Should be 0
            if new_size > 0:
                raise Exception(new_size)
            # Can be negative if user modifies positions manually
            elif new_size < 0:
                new_size = 0

            self.pf['trades'][t_id]['position']['size'] = new_size
            self.pf['trades'][t_id]['position']['status'] = "CLOSED"

            self.trade_complete(t_id)

        elif fill_conf['metatype'] == "TAKE_PROFIT":

            # Update the modified position.
            size = self.pf['trades'][t_id]['position']['size']
            new_size = size - fill_conf['size']
            self.pf['trades'][t_id]['position']['size'] = new_size

            if new_size == 0:
                self.trade_complete(t_id)
            else:
                self.calculate_pnl_by_trade(t_id)

        elif fill_conf['metatype'] == "FINAL_TAKE_PROFIT":

            # Update the now closed postiion, trade is done.
            size = self.pf['trades'][t_id]['position']['size']
            new_size = size - fill_conf['size']
            self.pf['trades'][t_id]['position']['size'] = new_size
            self.pf['trades'][t_id]['position']['status'] = "CLOSED"

            if new_size != 0:
                raise Exception(
                    "Position close size error:", new_size)

            self.trade_complete(t_id)

        else:
            raise Exception("Order metatype error:", fill_conf['metatype'])

        self.save_porfolio(self.pf)

    def new_order_conf(self, order_confs: list, events):
        """
        Update stored trade and order state to match given order confirmations.

        Args:
            order_confs: list of order dicts containing updated details.
            events:  event queue object.
        Returns:
           None.

        Raises:
            None.
        """

        # Update portfolio state.
        for conf in order_confs:
            t_id = str(conf['trade_id'])
            o_id = str(conf['order_id'])
            self.pf['trades'][t_id]['orders'][o_id] = conf

            # Create a fill event if order already filled (e.g. market orders).
            if conf['status'] == "FILLED":
                events.put(FillEvent(conf))

        self.save_porfolio(self.pf)

    def trade_complete(self, trade_id):
        """
        Check all orders and positions are closed, calculate pnl, run post
        trade checks/analytics.
        """

        # Cancel all orders marching trade ID.
        self.cancel_orders_by_trade_id(trade_id)

        # Close positions, if still open.
        if self.check_position_open(trade_id):
            self.close_position_by_trade_id(trade_id)

        # Calculate trade pnl.
        self.calculate_pnl_by_trade(trade_id)

        # Run post-trade analytics.
        self.run_post_trade_analysis(trade_id)

        # Reduce active trade count by 1.
        self.pf['total_active_trades'] -= 1 if self.pf['total_active_trades'] > 0 else 0

        # Mark trade as inactive
        self.pf['trades'][str(trade_id)]['active'] = False

        # Save updated portfolio state to DB.
        self.save_porfolio(self.pf)

    def cancel_orders_by_trade_id(self, trade_id):
        """
        Cancel all orders matching the given trade ID and update
        local portfolio state.
        """

        t_id = str(trade_id)
        o_ids = self.pf['trades'][t_id]['orders'].keys()
        v_ids = [
            self.pf['trades'][t_id]['orders'][o]['venue_id'] for o in o_ids if
            self.pf['trades'][t_id]['orders'][o]['status'] != "FILLED"]

        venue = self.pf['trades'][t_id]['venue']

        cancel_confs = self.exchanges[venue].cancel_orders(v_ids)

        if cancel_confs:

            # Handle cancellation failure messages
            try:
                if cancel_confs['error']["message"] == 'Not Found':
                    self.pf['trades'][t_id]['active'] = False
                    for o in o_ids:
                        self.pf['trades'][t_id]['orders'][o]['status'] == "FILLED"

                # Handle other error messages here
                else:
                    raise Exception("Unhandled case", cancel_confs['error']["message"])

            # Handle successful cancellation messages
            except KeyError:

                self.pf['trades'][t_id]['active'] = False

                for order_id in o_ids:
                    for venue_id in set(v_ids):

                        # Set order status to cancelled
                        if self.pf['trades'][t_id]['orders'][order_id][
                            'venue_id'] == venue_id and cancel_confs[
                                venue_id] == "SUCCESS":

                            self.pf['trades'][t_id]['orders'][order_id][
                                'status'] = "CANCELLED"

        # No active cancellations ocurred, trade was vetoed
        else:
            pass

    def check_position_open(self, trade_id):
        """
        Return true if position is still open according to local portfolio.
        """

        t_id = str(trade_id)
        if self.pf['trades'][t_id]['position'] is None:
            return False
        if self.pf['trades'][t_id]['position']['status'] == "OPEN":
            return True
        elif self.pf['trades'][t_id]['position']['status'] == "CLOSED":
            return False
        else:
            raise Exception(
                "Position status error:",
                self.pf['trades'][t_id]['position']['status'])

    def close_position_by_trade_id(self, t_id):
        """
        This method will close only the remaining amount for the given trade -
        it will not necessarily close an entire position, unless there is only
        one open position in that particular instrument.

        Then, update local portfolio state.

        Use close_position_absolute() to completely close all positions in
        for specifc instrument at a specific venue.
        """

        close = self.exchanges[
            self.pf['trades'][t_id]['venue']].close_position(
                self.pf['trades'][t_id]['symbol'],
                self.pf['trades'][t_id]['position']['size'],
                self.pf['trades'][t_id]['direction'])

        if close:
            self.pf['trades'][t_id]['position']['size'] = 0
            self.pf['trades'][t_id]['position']['status'] = "CLOSED"

    def close_position_absolute(self, venue, symbol):
        """
        Close ALL units of given instrument symbol indiscriminately.
        """

        return self.exchanges[venue].close_position(symbol)

    def calculate_pnl_by_trade(self, trade_id):
        """
        Calculate pnl for the given trade and update local portfolio state.
        """

        # Match internal order ids with venue ids {venue id: order id}
        t_id = str(trade_id)
        o_ids = self.pf['trades'][t_id]['orders'].keys()
        id_pairs = {self.pf['trades'][t_id]['orders'][i]['venue_id']: i for i in o_ids}

        # Fetch all balance affecting executions.
        executions = self.exchanges[self.pf['trades'][t_id][
            'venue']].get_executions(self.pf['trades'][t_id]['symbol'])

        unique_o_ids = list(set([i['order_id'] for i in executions]))

        # Sort execs {{order_id: [exc1, exc2, exc3, etc]}, ... }
        s_exc = {i: [] for i in unique_o_ids if i in o_ids}
        for exc in executions:
            if exc['order_id'] in o_ids:
                s_exc[exc['order_id']].append(exc)

        # Avg total long and short for the trade.
        avg_long, long_total, avg_short, short_total, total_fee = 0, 0, 0, 0, 0

        for o_id in o_ids:
            try:
                for sub_order in s_exc[o_id]:
                    if sub_order['direction'] == "LONG":
                        avg_long += sub_order['avg_exc_price'] * sub_order['size']
                        long_total += sub_order['size']
                        total_fee += sub_order['total_fee']

                    elif sub_order['direction'] == "SHORT":
                        avg_short += sub_order['avg_exc_price'] * sub_order['size']
                        short_total += sub_order['size']
                        total_fee += sub_order['total_fee']

            # If order ID isnt in executions, order was not executed.
            except KeyError:
                pass

        try:
            if long_total and avg_long:
                avg_long /= long_total
            if short_total and avg_short:
                avg_short /= short_total

        except ZeroDivisionError:
            traceback.print_exc()
            print("long_total", long_total)
            print("avg_long", avg_long)
            print("short_total", short_total)
            print("avg_short", avg_short)
            sys.exit(0)

        if self.pf['trades'][t_id]['direction'] == "LONG":
            pnl = avg_short - avg_long if avg_short and avg_long else 0
        elif self.pf['trades'][t_id]['direction'] == "SHORT":
            pnl = avg_long - avg_short if avg_short and avg_long else 0
        else:
            raise Exception(self.pf['trades'][t_id]['direction'])

        self.pf['current_balance'] += (pnl + total_fee)
        self.pf['balance_history'][str(int(time.time()))] = {
            'amt': pnl + total_fee,
            'trade_id': t_id}

    def run_post_trade_analysis(self, trade_id):
        """
        TODO: conduct post-trade analytics.
        """
        pass

    def verify_portfolio_state(self, portfolio):
        """
        Check stored portfolio data matches actual positions and orders.
        """

        # TODO.

        self.save_porfolio(portfolio)
        self.logger.info("Portfolio verification complete.")

    def load_portfolio(self, ID=1):
        """
        Load portfolio matching ID from database or return empty portfolio.
        """

        portfolio = self.db_other['portfolio'].find_one({"id": ID}, {"_id": 0})

        if portfolio:
            return portfolio

        else:
            default_portfolio = {
                'id': ID,
                'balance_history': {
                    str(int(time.time())): {
                        'amt': 1000,
                        'trade_id': "Initial deposit."}},
                'current_balance': 1000,
                'current_drawdown': 0,
                'avg_r_per_winner': 0,
                'avg_r_per_loser': 0,
                'avg_r_per_trade': 0,
                'total_winning_trades': 0,
                'total_losing_trades': 0,
                'total_consecutive_wins': 0,
                'total_consecutive_losses': 0,
                'win_loss_ratio': 0,
                'gain_to_pain_ratio': 0,
                'risk_per_trade': self.RISK_PER_TRADE,
                'max_correlated_positions': self.MAX_CORRELATED_POSITIONS,
                'max_accepted_drawdown': self.MAX_ACCEPTED_DRAWDOWN,
                'max_simultaneous_positions': self.MAX_SIMULTANEOUS_POSITIONS,
                'default_stop': self.DEFAULT_STOP,
                'model_allocations': {  # Equal allocation by default.
                    i.get_name(): (100 / len(self.models)) for i in self.models},
                'total_active_trades': 0,
                'trades': {}}

            return default_portfolio

    def save_porfolio(self, portfolio):
        """
        Save portfolio state to DB.
        """

        result = self.db_other['portfolio'].replace_one(
            {"id": portfolio['id']}, portfolio, upsert=True)

        if result.acknowledged:
            self.logger.info("Portfolio save successful.")
        else:
            self.logger.info("Portfolio save unsuccessful.")

    def within_risk_limits(self, signal):
        """
        Return true if the new signal would be within risk limits if traded.
        """

        # Position limit check.
        if self.pf['total_active_trades'] < self.pf['max_simultaneous_positions']:

            # Drawdown limit check.
            if (
                (self.pf['current_drawdown'] / self.pf['current_balance'])
                    * 100) >= self.pf['max_accepted_drawdown'] or (
                    self.pf['current_drawdown'] == 0):

                # Correlation check.
                if not self.correlated(signal):
                    self.logger.info(
                        "New trade within risk limits.")
                    return True

                else:
                    self.logger.info(
                        "New trade skipped. Correlated positions limit reached.")
                    return False
            else:
                self.logger.info("New trade skipped. Drawdown limit reached.")
                return False
        else:
            self.logger.info("New trade skipped. Position limit reached.")
            return False

    def calculate_exposure(self, trade):
        """
        Calculate the currect capital at risk for the given trade.
        """
        pass

    def correlated(self, signal):
        """
        Return true if any active trades would be correlated with trades
        produced by the incoming signal.
        """
        return False

    def calculate_stop_price(self, signal):
        """
        Find the stop price for the given signal.
        """

        if signal['stop_price'] is not None:
            return signal['stop_price']

        else:
            if signal['direction'] == "LONG":
                return signal['entry_price'] / 100 * (100 - self.DEFAULT_STOP)

            elif signal['direction'] == "SHORT":
                return signal['entry_price'] / 100 * (100 + self.DEFAULT_STOP)

    def calculate_position_size(self, stop, entry):
        """
        Find appropriate position size for the given parameters.
        """

        # Fixed percentage per trade risk management.
        if isinstance(self.RISK_PER_TRADE, int):

            account_size = self.pf['current_balance']
            risked_amt = (account_size / 1000) * self.RISK_PER_TRADE
            position_size = risked_amt // ((stop - entry) / entry)

            return abs(position_size)

        # TOOD: Kelly criteron risk management.
        elif self.RISK_PER_TRADE.upper() == "KELLY":
            pass

        else:
            raise Exception("RISK_PER_TRADE must be an integer, or 'KELLY': " + self.RISK_PER_TRADE)

    def update_price(self, events, market_event):
        """
        Check price and time updates against existing positions.

        Args:
            events: event queue object.
            event: new market event.

        Returns:
           None.

        Raises:
            None.
        """
        pass

    def save_new_trades_to_db(self):
        """
        Save trades in save-later queue to database.

        Args:
            None.
        Returns:
            None.
        Raises:
            pymongo.errors.DuplicateKeyError.
        """

        count = 0
        while True:

            try:
                trade = self.trades_save_to_db.get(False)

            except queue.Empty:
                if count:
                    self.logger.info(
                        "Wrote " + str(count) + " new trades to database " +
                        str(self.db_other.name) + ".")
                break

            else:
                if trade is not None:
                    count += 1
                    # Store signal in relevant db collection.
                    try:
                        self.db_other['trades'].insert_one(trade)
                    # Skip duplicates if they exist.
                    except pymongo.errors.DuplicateKeyError:
                        continue

                self.trades_save_to_db.task_done()

    def generate_trade_setup_image(self, trade, op_data, within_risk_limits: bool):

        self.logger.info("Creating signal snapshot image")

        # Create the image directory if it doesnt exist
        if not os.path.exists("setup_images"):
            os.mkdir("setup_images")

        # Dump trade data to file for ease of testing next stage
        # Remove from production
        # op_data.to_csv('op_data.csv')
        # with open('trade.json', 'w') as outfile:
        #     json.dump(trade, outfile)

        # Reformat dataframe for mplfinance compatibility
        df = op_data.copy(deep=True)
        df.rename(
            {'open': 'Open', 'high': 'High', 'low': 'Low',
             'close': 'Close', 'volume': 'Volume'}, axis=1,
            inplace=True)
        df = df.tail(self.SNAPSHOT_SIZE)

        # Get markers for trades triggered by the current bar
        entry_marker = [np.nan for i in range(self.SNAPSHOT_SIZE)]
        entry_marker[-1] = trade['entry_price']
        stop = None
        stop_marker = [np.nan for i in range(self.SNAPSHOT_SIZE)]
        for order in trade['orders'].values():
            if order['order_type'] == "STOP":
                stop = order['price']
                stop_marker[-1] = stop

        # TODO: Trades triggered by interaction with historic bars

        # Create plot figures
        adp, hlines = self.create_addplots(df, mpl, stop, entry_marker,
                                           stop_marker)
        mc = mpl.make_marketcolors(up='w', down='black', wick="w", edge='w')
        style = mpl.make_mpf_style(gridstyle='', base_mpf_style='nightclouds',
                                   marketcolors=mc)
        filename = "setup_images/" + str(trade['trade_id']) + "_" + str(trade['signal_timestamp']) + '_' + trade['model'] + "_" + trade['timeframe']

        try:
            plot = mpl.plot(df, type='candle', addplot=adp, style=style, hlines=hlines,
                            title="\n" + trade['model'] + " - " + trade['timeframe'],
                            datetime_format='%d-%m %H:%M', figscale=1, savefig=filename,
                            tight_layout=False)

        except ValueError:
            traceback.print_exc()
            print(df)
            print(df['Open'])
            sys.exit(0)

        message = "Trade " + str(trade['trade_id']) + " - " + trade['model'] + " " + trade['timeframe'] + "\n\nEntry: " + str(trade['entry_price']) + " \nStop: " + str(stop) + "\n"
        options = [[str(trade['trade_id']) + " - Accept", str(trade['trade_id']) + " - Veto"]]

        try:
            self.telegram.send_image(filename + ".png", message)
            if within_risk_limits is True:
                self.telegram.send_option_keyboard(options)
            else:
                self.telegram.send_message("Trade would exceed risk limits.")

        except Exception as ex:
            self.logger.info("Failed to send setup image via telegram.")
            print(ex)
            traceback.print_exc()

    def create_addplots(self, df, mpl, stop, entry_marker, stop_marker):
        """
        Helper method for generate_trade_setup_image.
        Formats plot artifcats for mplfinance.
        """

        adps, hlines = [], {'hlines': [], 'colors': [], 'linestyle': '--',
                            'linewidths': 0.5}

        # Add technical feature data (indicator values, etc).
        for col in list(df):
            if (
                col != "Open" and col != "High" and col != "Low"
                    and col != "Close" and col != "Volume"):
                adps.append(mpl.make_addplot(df[col]))

        # Add entry marker
        adps.append(mpl.make_addplot(
            entry_marker, type='scatter', markersize=500, marker="_",
            color='limegreen'))

        # Add stop marker
        if stop:
            adps.append(mpl.make_addplot(
                stop_marker, type='scatter', markersize=500, marker='_',
                color='crimson'))

        return adps, hlines
