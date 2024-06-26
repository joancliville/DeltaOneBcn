from datetime import datetime
from bisect import bisect
import itertools
import asyncio
import math
import time

from typing import List, Tuple

from ready_trader_go import (
    BaseAutoTrader,
    Instrument,
    Lifespan,
    MAXIMUM_ASK,
    MINIMUM_BID,
    Side,
)


LOT_SIZE = 10
POSITION_LIMIT = 100
TICK_SIZE_IN_CENTS = 100
MIN_BID_NEAREST_TICK = (
    (MINIMUM_BID + TICK_SIZE_IN_CENTS) // TICK_SIZE_IN_CENTS * TICK_SIZE_IN_CENTS
)
MAX_ASK_NEAREST_TICK = MAXIMUM_ASK // TICK_SIZE_IN_CENTS * TICK_SIZE_IN_CENTS


class AutoTrader(BaseAutoTrader):
    def __init__(self, loop: asyncio.AbstractEventLoop, team_name: str, secret: str):
        """Initialise a new instance of the AutoTrader class."""

        super().__init__(loop, team_name, secret)
        self.order_ids = itertools.count(1)
        self.ask_id = (
            self.ask_price
        ) = self.bid_id = self.bid_price = self.position = self.size = 0

        self.bid_volume = self.ask_volume = self.new_bid_price = self.new_ask_price = 0

        self.last_bid_id = self.last_ask_id = 0

        self.abs_position = 0
        self.theo_price = 0

        self.bids = set()
        self.asks = set()

        self.start_second = 0
        self.current_time = 0
        self.time_diff = 0
        self.action_count = 0

        self.risk_factor = 4
        self.position_interval = 0
        self.position_step = (-94, -90, -81, -64, -44, 0, 1, 45, 65, 82, 91, 95)
        self.quote_ladder = (
            (95, 97),
            (95, 95),
            (85, 90),
            (65, 82),
            (45, 70),
            (49 - self.risk_factor, 49 - self.risk_factor),
            (30, 30),
            (49 - self.risk_factor, 49 - self.risk_factor),
            (70 - self.risk_factor, 45 - self.risk_factor),
            (81 - self.risk_factor, 65),
            (90 - self.risk_factor, 85),
            (95 - self.risk_factor, 95),
            (97 - self.risk_factor, 95),
        )

        self.l0_w = 0.35
        self.l1_w = 0.65

        self._abs = abs
        self._int = int
        self._bisect = bisect
        self._print = print
        self._next = next

    def on_error_message(self, client_order_id: int, error_message: bytes) -> None:
        """Called when the exchange detects an error."""

        self._print(
            "error with order",
            client_order_id,
            error_message.decode(),
            "Position:",
            self.position,
            "Bid vol:",
            self.bid_volume,
            "Ask_vol:",
            self.ask_volume,
        )
        self.logger.warning(
            "error with order %d: %s", client_order_id, error_message.decode()
        )
        self.on_order_status_message(client_order_id, 0, 0, 0)

    def on_order_book_update_message(
        self,
        instrument: int,
        sequence_number: int,
        ask_prices: List[int],
        ask_volumes: List[int],
        bid_prices: List[int],
        bid_volumes: List[int],
    ) -> None:
        """Called periodically to report the status of an order book."""

        if instrument == Instrument.FUTURE:

            # Count action
            if self.action_count == 0:
                self.start_second = time.time()

            # Theoretical price = weighted average orderbook levels 1, 2 and 3.
            if 0 not in bid_prices and self.action_count <= 16:
                self.theo_price = (
                    self._int(
                        (
                            (
                                (
                                    ((bid_prices[0] * bid_volumes[0]) * self.l0_w)
                                    + ((bid_prices[1] * bid_volumes[1]) * self.l1_w)
                                    + ((ask_prices[0] * ask_volumes[0]) * self.l0_w)
                                    + ((ask_prices[1] * ask_volumes[1]) * self.l1_w)
                                )
                                / (
                                    (bid_volumes[0] * self.l0_w)
                                    + (bid_volumes[1] * self.l1_w)
                                    + (
                                        ask_volumes[0] * self.l0_w
                                        + (ask_volumes[1] * self.l1_w)
                                    )
                                )
                            )
                        )
                        / 100
                    )
                    * 100
                )

                # Use theo price += $1 for bid/ask.
                self.new_bid_price = self.theo_price - 100 if bid_prices[0] != 0 else 0
                self.new_ask_price = self.theo_price + 100 if ask_prices[0] != 0 else 0

            # Place new orders if within action limit.
            if self.action_count <= 16:

                # Cancel existing quotes if they differ from new quotes.
                if self.bid_id != 0 and self.new_bid_price not in (self.bid_price, 0):
                    self.send_cancel_order(self.bid_id)
                    self.bid_id = 0
                    self.action_count += 1
                if self.ask_id != 0 and self.new_ask_price not in (self.ask_price, 0):
                    self.send_cancel_order(self.ask_id)
                    self.ask_id = 0
                    self.action_count += 1

            # Place new orders if within action limit.
            if self.action_count <= 14:

                self.position_interval = self._bisect(self.position_step, self.position)

                self.bid_volume, self.ask_volume = self.quote_ladder[
                    self.position_interval
                ]

                if self.position >= 0:
                    self.bid_volume -= self.position
                else:
                    self.ask_volume -= self._abs(self.position)

                # Correct negative quote volumes to prevent exchange error.
                self.bid_volume = 0 if self.bid_volume < 0 else self.bid_volume
                self.ask_volume = 0 if self.ask_volume < 0 else self.ask_volume

                # Place bid quote.
                if (
                    self.bid_id == 0
                    and self.new_bid_price != 0
                    and self.bid_volume != 0
                ):
                    self.bid_id = self._next(self.order_ids)
                    self.bid_price = self.new_bid_price
                    self.send_insert_order(
                        self.bid_id,
                        Side.BUY,
                        self.new_bid_price,
                        self.bid_volume,
                        Lifespan.GOOD_FOR_DAY,
                    )
                    self.bids.add(self.bid_id)
                    self.action_count += 1

                # Place ask quote.
                if (
                    self.ask_id == 0
                    and self.new_ask_price != 0
                    and self.ask_volume != 0
                ):
                    self.ask_id = self._next(self.order_ids)
                    self.ask_price = self.new_ask_price
                    self.send_insert_order(
                        self.ask_id,
                        Side.SELL,
                        self.new_ask_price,
                        self.ask_volume,
                        Lifespan.GOOD_FOR_DAY,
                    )
                    self.asks.add(self.ask_id)
                    self.action_count += 1

            # Reset action count if within time constraints.
            elif self.action_count >= 14:
                self.current_time = time.time()
                self.time_diff = self.current_time - self.start_second

                # print("Time diff:", self.time_diff, "Action count:", self.action_count)

                # Sleep for remainder of second if necessary.
                if self.time_diff < 1:
                    time.sleep(1.01 - self.time_diff)

                self.action_count = 0

    def on_order_status_message(
        self, client_order_id: int, fill_volume: int, remaining_volume: int, fees: int
    ) -> None:
        """Called when the status of one of your orders changes."""

        # Set order ID's for orders just filled.
        if remaining_volume == 0:
            if client_order_id == self.bid_id:
                self.bid_id = 0
            elif client_order_id == self.ask_id:
                self.ask_id = 0

            # It could be either a bid or an ask
            self.bids.discard(client_order_id)
            self.asks.discard(client_order_id)

    def on_order_filled_message(
        self, client_order_id: int, price: int, volume: int
    ) -> None:
        """Called when one of your orders is filled, partially or fully.

        The price is the price at which the order was (partially) filled,
        which may be better than the order's limit price. The volume is
        the number of lots filled at that price.
        """
        self.logger.info(
            "received order filled for order %d with price %d and volume %d",
            client_order_id,
            price,
            volume,
        )
        if client_order_id in self.bids:
            self.position += volume
            self.send_hedge_order(
                next(self.order_ids), Side.ASK, MIN_BID_NEAREST_TICK, volume
            )
        elif client_order_id in self.asks:
            self.position -= volume
            self.send_hedge_order(
                next(self.order_ids), Side.BID, MAX_ASK_NEAREST_TICK, volume
            )

    def on_trade_ticks_message(
        self,
        instrument: int,
        sequence_number: int,
        ask_prices: List[int],
        ask_volumes: List[int],
        bid_prices: List[int],
        bid_volumes: List[int],
    ) -> None:
        """Called periodically when there is trading activity on the market.

        The five best ask (i.e. sell) and bid (i.e. buy) prices at which there
        has been trading activity are reported along with the aggregated volume
        traded at each of those price levels.

        If there are less than five prices on a side, then zeros will appear at
        the end of both the prices and volumes arrays.
        """
        self.logger.info(
            "received trade ticks for instrument %d with sequence number %d",
            instrument,
            sequence_number,
        )
