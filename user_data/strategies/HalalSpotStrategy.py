import logging
import re
from datetime import datetime

import pandas as pd
from pandas import DataFrame
import talib.abstract as ta

from freqtrade.exceptions import OperationalException
from freqtrade.persistence import Trade
from freqtrade.exchange import timeframe_to_prev_date, timeframe_to_minutes
from freqtrade.strategy import (
    IStrategy,
    merge_informative_pair,
    stoploss_from_absolute,
    DecimalParameter,
    IntParameter,
)
from technical import qtpylib


logger = logging.getLogger(__name__)


class HalalSpotStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    informative_timeframe = "4h"

    # Spot long positions only.
    can_short = False

    # ROI exits disabled. Profitable exits are handled by the ATR stop, the
    # partial take-profit, and indicator exits.
    minimal_roi = {}

    # Hard fallback. The custom ATR stop normally keeps risk tighter.
    stoploss = -0.06

    # Use an ATR-based stop which adapts to each pair's volatility.
    use_custom_stoploss = True

    # Allow indicator exits to protect against trend reversals,
    # including on losing trades.
    use_exit_signal = True
    exit_profit_only = False

    # Enable partial exits (scale-out). Additional entries into an open trade
    # are forbidden, so a trade can never exceed the 1R risk it was sized for.
    position_adjustment_enable = True
    max_entry_position_adjustment = 0

    process_only_new_candles = True
    # EMA indicators need more history than their nominal period to converge.
    # Verify with freqtrade recursive-analysis after backtesting.
    startup_candle_count = 800

    # --- Halal guardrails ---
    # Base assets whose core business is lending/borrowing at interest,
    # interest-bearing or yield-bearing synthetic stablecoins, leveraged
    # derivatives venues, or gambling. Enforced in confirm_trade_entry() and at
    # startup, so a hand-edited whitelist cannot quietly reintroduce them.
    # This is a technical filter chosen by the operator, not a religious ruling.
    non_compliant_bases = frozenset(
        {
            # Lending / borrowing / interest protocols
            "AAVE", "COMP", "MKR", "SKY", "CREAM", "VENUS", "XVS", "ALPACA",
            "JST", "QI", "BIFI", "RDNT", "EUL", "GHO", "MORPHO", "SPELL",
            "MIM", "ALPHA", "LQTY", "STG",
            # Yield-bearing / interest-linked synthetic stables
            "ENA", "USDE", "SUSDE", "USDD", "USDX",
            # Perpetual / leveraged-derivatives venues
            "DYDX", "GMX", "GNS", "PERP", "LEVER",
            # Liquid staking and yield tokenisation
            "LDO", "RPL", "JTO", "SD", "PENDLE",
            # Interest-adjacent DeFi yield aggregators
            "CRV", "CVX", "YFI", "FXS", "SNX",
            # Gambling
            "FUN", "WINK", "WIN", "EDG",
        }
    )
    # Synthetic leverage tokens: BTCUP, ETHDOWN, BTC3L, ETH5S, ...
    # The length prefixes keep short legitimate tickers (JUP, WIN) from
    # matching by accident.
    leveraged_token_patterns = (
        r".{3,}(UP|DOWN|BULL|BEAR)",
        r".{2,}[35][LS]",
    )

    # --- Hyperoptable entry thresholds ---
    # Kept as parameters (rather than plain constants) so `freqtrade hyperopt`
    # can retune them against fresh data instead of hand-picked guesses.
    rsi_pullback_trigger = IntParameter(30, 40, default=35, space="buy", optimize=True)
    # How far below the trigger RSI must actually have dipped in the last five
    # candles. At 0 the "was oversold" check is a no-op, because crossed_above()
    # already guarantees the previous candle sat at or below the trigger.
    rsi_pullback_depth = IntParameter(0, 10, default=4, space="buy", optimize=True)
    rsi_pullback_max = IntParameter(44, 55, default=48, space="buy", optimize=True)
    rsi_continuation_trigger = IntParameter(45, 55, default=50, space="buy", optimize=True)
    rsi_continuation_max = IntParameter(58, 68, default=62, space="buy", optimize=True)
    atr_ratio_min = DecimalParameter(0.0015, 0.0040, default=0.0025, decimals=4, space="buy", optimize=True)
    atr_ratio_max = DecimalParameter(0.0150, 0.0350, default=0.0250, decimals=3, space="buy", optimize=True)
    # Minimum 4h trend slope, as a fraction of the 4h SMA over 3 candles.
    # 0.0 means "the higher-timeframe trend is not rolling over"; the low end
    # of the range effectively disables the gate, so hyperopt can measure what
    # it costs instead of inheriting it as an assumption. It is the most
    # expensive of the regime filters in terms of signals removed.
    htf_slope_min = DecimalParameter(-0.020, 0.005, default=0.0, decimals=4, space="buy", optimize=True)
    # Trend-strength floor. Filters the sideways chop where both entry branches
    # fire signals that immediately mean-revert.
    adx_min = IntParameter(10, 30, default=18, space="buy", optimize=True)
    # Refuse to chase: skip entries where price already sits this many ATRs
    # above the fast moving average.
    max_entry_extension_atr = DecimalParameter(1.0, 4.0, default=2.5, decimals=1, space="buy", optimize=True)

    # --- Hyperoptable risk/exit thresholds ---
    initial_atr_multiplier = DecimalParameter(1.5, 3.5, default=2.5, decimals=2, space="sell", optimize=True)
    trailing_atr_multiplier = DecimalParameter(1.2, 3.0, default=2.0, decimals=2, space="sell", optimize=True)
    max_initial_loss = DecimalParameter(0.03, 0.08, default=0.06, decimals=3, space="sell", optimize=True)
    # Scale out part of the position at this R multiple. The remainder rides
    # the trailing stop, so a winner can still run while the trade is already
    # banked above breakeven.
    partial_exit_r = DecimalParameter(1.0, 3.0, default=1.5, decimals=1, space="sell", optimize=True)
    partial_exit_fraction = DecimalParameter(0.25, 0.60, default=0.50, decimals=2, space="sell", optimize=True)
    breakeven_profit_buffer = 0.0025

    # Risk sized as a fraction of total wallet value per new trade. Actual
    # stake also respects exchange min/max and the config stake allocation.
    risk_per_trade = 0.01
    # Safety ceiling so a very tight ATR stop can't turn "1% risk" into an
    # oversized stake; caps stake at this multiple of freqtrade's own
    # proposed (config-driven) stake. With stake_amount "unlimited" the
    # proposed stake is roughly wallet/max_open_trades, so 1.5 keeps a single
    # low-volatility entry from crowding out the other two slots.
    max_stake_multiplier = 1.5

    # Reject an entry whose fill price has already run this many ATRs above the
    # close of the candle that produced the signal.
    max_entry_slippage_atr = 0.5

    # A trade that has neither hit its stop nor made meaningful progress
    # after this many candles is treated as dead capital and closed.
    stale_trade_candles = 96  # 24h on the 15m timeframe
    stale_trade_profit_ceiling = 0.005

    plot_config = {
        "main_plot": {
            "sma_fast": {"color": "orange"},
            "sma_slow": {"color": "grey"},
            "ema_trend": {"color": "blue"},
            "sma_trend_4h": {"color": "purple"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "red"}},
            "ADX": {"adx": {"color": "green"}},
            "ATR ratio": {"atr_ratio": {"color": "brown"}},
        },
    }

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 4,
            },
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 48,
                "trade_limit": 4,
                "stop_duration_candles": 12,
                "max_allowed_drawdown": 0.10,
            },
            {
                # Pulls a pair out of rotation after repeated marginal closes,
                # even if none of them individually tripped the stoploss
                # guard. Reduces death-by-a-thousand-cuts from choppy pairs.
                "method": "LowProfitPairs",
                "lookback_period_candles": 120,
                "trade_limit": 3,
                "stop_duration_candles": 24,
                "required_profit": 0.005,
            },
        ]

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------

    @staticmethod
    def _config_value(raw) -> str:
        # trading_mode / margin_mode arrive as freqtrade enums whose str()
        # representation differs between Python versions.
        return str(getattr(raw, "value", raw) or "").lower()

    def bot_start(self, **kwargs) -> None:
        # Fail loudly at startup rather than discovering at 3am that a config
        # edit quietly turned this into a margin bot.
        problems = []

        trading_mode = self._config_value(self.config.get("trading_mode", "spot"))
        if trading_mode != "spot":
            problems.append(f"trading_mode is '{trading_mode}', expected 'spot'")

        margin_mode = self._config_value(self.config.get("margin_mode", ""))
        if margin_mode not in ("", "none"):
            problems.append(f"margin_mode is '{margin_mode}', expected unset")

        if self.can_short:
            problems.append("can_short is True; this strategy is long-only")

        for pair in self.config.get("exchange", {}).get("pair_whitelist", []):
            if not self._is_compliant_pair(pair):
                problems.append(f"whitelisted pair '{pair}' is on the non-compliant list")

        if problems:
            raise OperationalException(
                "HalalSpotStrategy refused to start: " + "; ".join(problems)
            )

        if not self.config.get("dry_run", True):
            logger.warning(
                "HalalSpotStrategy is running with dry_run disabled - real funds are at risk."
            )

    def _is_compliant_pair(self, pair: str) -> bool:
        base = pair.split("/")[0].split(":")[0].upper()

        if base in self.non_compliant_bases:
            return False

        return not any(
            re.fullmatch(pattern, base) for pattern in self.leveraged_token_patterns
        )

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        # Never borrow. Unused on spot, pinned so a mis-set config cannot
        # inherit a leveraged default.
        return 1.0

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()

        informative_pairs = [
            (pair, self.informative_timeframe)
            for pair in pairs
        ]

        # BTC/USDT is used everywhere as a market-regime filter, even for
        # pairs that aren't in the whitelist alongside it.
        if ("BTC/USDT", self.informative_timeframe) not in informative_pairs:
            informative_pairs.append(("BTC/USDT", self.informative_timeframe))

        return informative_pairs

    def populate_indicators(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:

        # Main timeframe indicators.
        dataframe["rsi"] = ta.RSI(
            dataframe,
            timeperiod=14,
        )

        dataframe["adx"] = ta.ADX(
            dataframe,
            timeperiod=14,
        )

        dataframe["sma_fast"] = ta.SMA(
            dataframe,
            timeperiod=20,
        )

        dataframe["sma_slow"] = ta.SMA(
            dataframe,
            timeperiod=50,
        )

        dataframe["ema_trend"] = ta.EMA(
            dataframe,
            timeperiod=200,
        )

        dataframe["atr"] = ta.ATR(
            dataframe,
            timeperiod=14,
        )

        dataframe["atr_ratio"] = (
            dataframe["atr"] / dataframe["close"]
        )

        # How far price has already run from the fast MA, in ATRs, so the
        # "don't chase" filter scales with each pair's own volatility.
        dataframe["extension_atr"] = (
            (dataframe["close"] - dataframe["sma_fast"]) /
            dataframe["atr"].where(dataframe["atr"] > 0)
        )

        dataframe["ema_trend_slope"] = (
            dataframe["ema_trend"] -
            dataframe["ema_trend"].shift(5)
        )

        dataframe["volume_mean"] = (
            dataframe["volume"]
            .rolling(20)
            .mean()
        )

        dataframe["volume_ratio"] = (
            dataframe["volume"] /
            dataframe["volume_mean"]
        )

        dataframe["rsi_min_5"] = (
            dataframe["rsi"]
            .rolling(5)
            .min()
        )

        # Four-hour trend indicators for this pair.
        informative = self.dp.get_pair_dataframe(
            pair=metadata["pair"],
            timeframe=self.informative_timeframe,
        )

        informative["sma_trend"] = ta.SMA(
            informative,
            timeperiod=50,
        )

        # Slope of the 4h trend, computed before the merge. Differencing the
        # merged (forward-filled) column would compare a value against itself
        # on 15 out of every 16 rows.
        #
        # Expressed as a fraction of the average itself so one threshold means
        # the same thing for BTC at 60k and for a sub-dollar altcoin.
        informative["sma_trend_slope"] = (
            informative["sma_trend"] -
            informative["sma_trend"].shift(3)
        ) / informative["sma_trend"]

        dataframe = merge_informative_pair(
            dataframe,
            informative,
            self.timeframe,
            self.informative_timeframe,
            ffill=True,
        )

        # Market-regime filter: only take longs (in any pair, including
        # BTC/USDT itself) while BTC's own 4h trend is constructive. Crypto
        # alts are highly correlated to BTC in drawdowns, so this cuts
        # exposure to "everything sells off together" scenarios that a
        # single-pair trend filter can't see.
        dataframe = self._merge_btc_regime(dataframe)

        return dataframe

    def _merge_btc_regime(self, dataframe: DataFrame) -> DataFrame:
        btc_pair = "BTC/USDT"
        btc_informative = self.dp.get_pair_dataframe(
            pair=btc_pair,
            timeframe=self.informative_timeframe,
        )

        if btc_informative is None or btc_informative.empty:
            # Fail closed: every entry condition below then evaluates to False
            # instead of the bot silently trading without a regime filter.
            logger.warning(
                "No %s %s data available; BTC regime filter will block all entries.",
                btc_pair,
                self.informative_timeframe,
            )
            dataframe["btc_close"] = float("nan")
            dataframe["btc_trend_sma"] = float("nan")
            return dataframe

        btc_slice = DataFrame(
            {
                "btc_close": btc_informative["close"],
                "btc_trend_sma": ta.SMA(btc_informative, timeperiod=50),
            }
        )

        # merge_informative_pair() can't be reused here: with
        # append_timeframe=False it leaves the informative "date" column in
        # place, and the inner pd.merge would then rename this pair's own
        # "date" to "date_x" and break every date lookup downstream.
        #
        # So replicate its merge key by hand, using the *same* offset it
        # applies internally: informative open time + one informative candle -
        # one base candle. A 4h candle opening at 00:00 is complete at 04:00,
        # which is also when the 15m candle dated 03:45 closes, so 03:45 is the
        # first row allowed to see it. Offsetting by a full 4h instead (as an
        # earlier revision did) is not lookahead, but it does make the BTC
        # filter one candle staler than this pair's own 4h columns.
        btc_slice["date_merge"] = btc_informative["date"] + pd.to_timedelta(
            timeframe_to_minutes(self.informative_timeframe)
            - timeframe_to_minutes(self.timeframe),
            unit="m",
        )

        merged = pd.merge_asof(
            dataframe.sort_values("date"),
            btc_slice.sort_values("date_merge"),
            left_on="date",
            right_on="date_merge",
            direction="backward",
        )

        return merged.drop(columns=["date_merge"])

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def populate_entry_trend(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:

        htf_close = f"close_{self.informative_timeframe}"
        htf_trend = f"sma_trend_{self.informative_timeframe}"
        htf_slope = f"sma_trend_slope_{self.informative_timeframe}"

        market_is_tradable = (
            # Four-hour trend confirmation, and that trend is not rolling over.
            (dataframe[htf_close] > dataframe[htf_trend])
            & (dataframe[htf_slope] >= self.htf_slope_min.value)

            # Short-term trend is not bearish.
            & (dataframe["sma_fast"] > dataframe["sma_slow"])

            # Long-term trend is flat or rising.
            & (dataframe["ema_trend_slope"] >= 0)

            # Broad market (BTC) is not in a 4h downtrend.
            & (dataframe["btc_close"] > dataframe["btc_trend_sma"])

            # There is a real trend to trade, not directionless chop.
            & (dataframe["adx"] >= self.adx_min.value)

            # Avoid markets where fees/noise dominate or volatility is extreme.
            & (dataframe["atr_ratio"] > self.atr_ratio_min.value)
            & (dataframe["atr_ratio"] < self.atr_ratio_max.value)

            # Don't buy an already-extended move, where the ATR stop sits far
            # below and mean reversion is the likeliest next candle.
            & (dataframe["extension_atr"] < self.max_entry_extension_atr.value)

            & (dataframe["volume"] > 0)
        )

        pullback_recovery = (
            market_is_tradable

            # RSI genuinely dipped below the trigger within the last five
            # candles and now produces a one-candle recovery cross.
            # crossed_above() alone only proves the previous candle touched the
            # trigger, so the depth term is what makes this an oversold
            # pullback rather than a restatement of the cross.
            & (
                dataframe["rsi_min_5"]
                <= (self.rsi_pullback_trigger.value - self.rsi_pullback_depth.value)
            )
            & qtpylib.crossed_above(dataframe["rsi"], self.rsi_pullback_trigger.value)
            & (dataframe["rsi"] < self.rsi_pullback_max.value)

            # Price is recovering rather than continuing to fall.
            & (dataframe["close"] > dataframe["close"].shift(1))
            & (dataframe["volume_ratio"] > 0.8)
        )

        trend_continuation = (
            market_is_tradable

            # Join an existing uptrend on a one-candle momentum trigger.
            & (dataframe["close"] > dataframe["ema_trend"])
            & (dataframe["close"] > dataframe["sma_fast"])
            & qtpylib.crossed_above(dataframe["rsi"], self.rsi_continuation_trigger.value)
            & (dataframe["rsi"] <= self.rsi_continuation_max.value)
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["volume_ratio"] > 1.05)
        )

        dataframe.loc[
            pullback_recovery,
            "enter_long",
        ] = 1
        dataframe.loc[pullback_recovery, "enter_tag"] = "pullback_recovery"

        continuation_only = trend_continuation & ~pullback_recovery
        dataframe.loc[
            continuation_only,
            "enter_long",
        ] = 1
        dataframe.loc[continuation_only, "enter_tag"] = "trend_continuation"

        return dataframe

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        if side != "long":
            logger.warning("Blocked non-long entry on %s (side=%s).", pair, side)
            return False

        if not self._is_compliant_pair(pair):
            logger.warning("Blocked entry on %s: pair is on the non-compliant list.", pair)
            return False

        dataframe, _ = self.dp.get_analyzed_dataframe(
            pair=pair,
            timeframe=self.timeframe,
        )

        if dataframe.empty:
            return False

        last_candle = dataframe.iloc[-1].squeeze()
        signal_close = last_candle.get("close")
        atr = last_candle.get("atr")

        # The signal fires on a closed candle but the order fills later. If
        # price has already jumped, the entry no longer offers the reward the
        # ATR stop distance was sized against.
        if atr and atr > 0 and signal_close:
            max_rate = signal_close + (self.max_entry_slippage_atr * atr)
            if rate > max_rate:
                logger.info(
                    "Skipping %s entry: rate %.8f is above signal close + %.2f ATR (%.8f).",
                    pair,
                    rate,
                    self.max_entry_slippage_atr,
                    max_rate,
                )
                return False

        return True

    # ------------------------------------------------------------------
    # Risk model
    # ------------------------------------------------------------------

    def _initial_stop_price(self, trade_open_rate: float, entry_atr: float) -> float:
        initial_stop_price = (
            trade_open_rate - (self.initial_atr_multiplier.value * entry_atr)
        )
        max_loss_price = trade_open_rate * (1 - self.max_initial_loss.value)
        return max(initial_stop_price, max_loss_price)

    def _entry_atr(self, dataframe: DataFrame, trade: Trade) -> float | None:
        # Anchor risk to volatility at entry instead of letting the current ATR
        # silently redefine the original trade risk.
        trade_candle_date = timeframe_to_prev_date(
            self.timeframe,
            trade.open_date_utc,
        )
        entry_candle = dataframe.loc[dataframe["date"] == trade_candle_date]

        if entry_candle.empty:
            return None

        entry_atr = entry_candle["atr"].iloc[-1]

        if entry_atr is None or entry_atr <= 0:
            return None

        return float(entry_atr)

    def _initial_risk(
        self,
        dataframe: DataFrame,
        trade: Trade,
        fallback_atr: float | None,
    ):
        """Return (stop_price, initial_risk) in quote units, or (None, None)."""
        entry_atr = self._entry_atr(dataframe, trade) or fallback_atr

        if not entry_atr or entry_atr <= 0:
            return None, None

        stop_price = self._initial_stop_price(trade.open_rate, entry_atr)
        initial_risk = trade.open_rate - stop_price

        if initial_risk <= 0:
            return None, None

        return stop_price, initial_risk

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        # Size every entry so a full stop-out loses roughly the same fraction
        # of the wallet, instead of a flat stake regardless of how volatile
        # (and therefore how distant the stop) each pair currently is.
        dataframe, _ = self.dp.get_analyzed_dataframe(
            pair=pair,
            timeframe=self.timeframe,
        )

        if dataframe.empty or current_rate <= 0:
            return proposed_stake

        last_candle = dataframe.iloc[-1].squeeze()
        atr = last_candle.get("atr")

        if atr is None or atr <= 0:
            return proposed_stake

        stop_price = self._initial_stop_price(current_rate, atr)
        stop_distance_pct = (current_rate - stop_price) / current_rate

        if stop_distance_pct <= 0:
            return proposed_stake

        wallet_value = self.wallets.get_total_stake_amount()
        risk_amount = wallet_value * self.risk_per_trade
        risk_based_stake = risk_amount / stop_distance_pct

        stake = min(
            risk_based_stake,
            proposed_stake * self.max_stake_multiplier,
            max_stake,
        )

        if min_stake is not None and stake < min_stake:
            # Round up to the exchange minimum only if that minimum still fits
            # under max_stake; otherwise skip the trade rather than silently
            # taking on more risk than the model allows.
            if min_stake > max_stake:
                return 0.0
            stake = min_stake

        return stake

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(
            pair=pair,
            timeframe=self.timeframe,
        )

        if dataframe.empty:
            return None

        last_candle = dataframe.iloc[-1].squeeze()
        current_atr = last_candle.get("atr")

        if current_atr is None or current_atr <= 0:
            return None

        stop_price, initial_risk = self._initial_risk(dataframe, trade, current_atr)

        if stop_price is None:
            return None

        # Progress measured in R against price, not against fee-inclusive
        # profit, so each rung of the stop ladder means the same thing
        # regardless of fee schedule or partial exits already taken.
        r_multiple = (current_rate - trade.open_rate) / initial_risk

        # At 1R, protect the entry price plus a small fee/slippage buffer.
        if r_multiple >= 1:
            breakeven_price = (
                trade.open_rate * (1 + self.breakeven_profit_buffer)
            )
            stop_price = max(stop_price, breakeven_price)

        # At 2R, trail using current volatility. Freqtrade will only ratchet the
        # stop upward outside an after-fill call.
        if r_multiple >= 2:
            trailing_stop_price = (
                current_rate - (self.trailing_atr_multiplier.value * current_atr)
            )
            stop_price = max(stop_price, trailing_stop_price)

        # If price already traded through the intended stop (a gap, or a move
        # between two calls), clamp just under the current rate so the position
        # exits now. Bailing out with None here would leave the trade riding the
        # -6% hard fallback instead of the ATR stop it was sized for.
        stop_price = min(stop_price, current_rate * (1 - 1e-6))

        return stoploss_from_absolute(
            stop_price,
            current_rate=current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ):
        # Scale out once at partial_exit_r. Together with the breakeven stop at
        # 1R this turns most trades that reach target into locked-in winners,
        # while the remainder still trails a genuine trend.
        if trade.nr_of_successful_exits > 0:
            return None

        dataframe, _ = self.dp.get_analyzed_dataframe(
            pair=trade.pair,
            timeframe=self.timeframe,
        )

        if dataframe.empty:
            return None

        _, initial_risk = self._initial_risk(dataframe, trade, None)

        if initial_risk is None:
            return None

        r_multiple = (current_rate - trade.open_rate) / initial_risk

        if r_multiple < self.partial_exit_r.value:
            return None

        exit_stake = trade.stake_amount * self.partial_exit_fraction.value
        remaining_stake = trade.stake_amount - exit_stake

        if min_stake is not None and (
            exit_stake < min_stake or remaining_stake < min_stake
        ):
            # The position is too small to split without leaving an
            # untradeable remainder; let the trailing stop handle all of it.
            return None

        return -exit_stake, "partial_take_profit"

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | None:
        # Close trades that are neither stopped out nor working after a
        # fixed number of candles. Capital stuck sideways in one trade is
        # capital that can't be risked on the next real signal, and
        # max_open_trades is small enough that this matters.
        timeframe_minutes = timeframe_to_minutes(self.timeframe)
        candles_open = (
            (current_time - trade.open_date_utc).total_seconds()
            / 60
            / timeframe_minutes
        )

        if (
            candles_open >= self.stale_trade_candles
            and current_profit < self.stale_trade_profit_ceiling
        ):
            return "stale_trade_timeout"

        return None

    # ------------------------------------------------------------------
    # Exits
    # ------------------------------------------------------------------

    def populate_exit_trend(
        self,
        dataframe: DataFrame,
        metadata: dict,
    ) -> DataFrame:

        htf_close = f"close_{self.informative_timeframe}"
        htf_trend = f"sma_trend_{self.informative_timeframe}"

        local_trend_breakdown = (
            # Two closes below EMA200 confirm that this is not a single wick.
            (dataframe["close"] < dataframe["ema_trend"])
            & (
                dataframe["close"].shift(1)
                < dataframe["ema_trend"].shift(1)
            )
            # The moving-average structure has also weakened.
            & (dataframe["sma_fast"] < dataframe["sma_slow"])
            # Do not require exceptional volume during a slow deterioration.
            & (dataframe["volume_ratio"] > 0.8)
            & (dataframe["volume"] > 0)
        )

        higher_timeframe_breakdown = (
            (dataframe[htf_close] < dataframe[htf_trend])
            & (dataframe["close"] < dataframe["sma_slow"])
            & (dataframe["rsi"] < 45)
            & (dataframe["volume"] > 0)
        )

        # The same broad-market filter that gates entries also closes trades:
        # once BTC loses its 4h trend, correlated alt longs are the first thing
        # to hand back their gains.
        btc_regime_breakdown = (
            (dataframe["btc_close"] < dataframe["btc_trend_sma"])
            & (dataframe["close"] < dataframe["sma_fast"])
            & (dataframe["volume"] > 0)
        )

        # Overbought exhaustion: take profit into strength rather than
        # waiting for the trend-breakdown exits, which only fire after price
        # has already given back a chunk of the move.
        rsi_exhaustion_reversal = (
            (dataframe["rsi"].shift(1) > 72)
            & (dataframe["rsi"] <= 70)
            & (dataframe["close"] < dataframe["close"].shift(1))
            & (dataframe["volume"] > 0)
        )

        dataframe.loc[
            local_trend_breakdown,
            ["exit_long", "exit_tag"],
        ] = (1, "local_trend_breakdown")

        dataframe.loc[
            higher_timeframe_breakdown & ~local_trend_breakdown,
            ["exit_long", "exit_tag"],
        ] = (1, "higher_timeframe_breakdown")

        already_flagged = local_trend_breakdown | higher_timeframe_breakdown

        dataframe.loc[
            btc_regime_breakdown & ~already_flagged,
            ["exit_long", "exit_tag"],
        ] = (1, "btc_regime_breakdown")

        already_flagged = already_flagged | btc_regime_breakdown

        dataframe.loc[
            rsi_exhaustion_reversal & ~already_flagged,
            ["exit_long", "exit_tag"],
        ] = (1, "rsi_exhaustion")

        return dataframe
