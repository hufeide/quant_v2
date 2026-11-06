"""回测分析器：净值、持仓、成交、订单明细（每个子账户各一份）。"""
from __future__ import annotations

import math

import backtrader as bt
import pandas as pd


class EquityCurveAnalyzer(bt.Analyzer):
    def __init__(self):
        self.records: list[dict] = []

    def next(self):
        strategy = self.strategy
        self.records.append(
            {
                "date": strategy.datas[0].datetime.date(0),
                "value": strategy.broker.getvalue(),
                "cash": strategy.broker.getcash(),
                "exposure": strategy._current_exposure(),
                "positions": sum(1 for d in strategy.datas if strategy.getposition(d).size > 0),
            }
        )

    def get_analysis(self):
        return pd.DataFrame(self.records)


class PositionSnapshotAnalyzer(bt.Analyzer):
    """逐日持仓快照，用于输出子账户持仓明细。"""

    def __init__(self):
        self.records: list[dict] = []

    def next(self):
        strategy = self.strategy
        date = strategy.datas[0].datetime.date(0)
        for data in strategy.datas:
            pos = strategy.getposition(data)
            if pos.size <= 0:
                continue
            price = float(data.close[0])
            self.records.append(
                {
                    "date": date,
                    "symbol": data._name,
                    "size": int(pos.size),
                    "price": price,
                    "value": pos.size * price,
                }
            )

    def get_analysis(self):
        return pd.DataFrame(self.records)


class TradeRecorder(bt.Analyzer):
    """已平仓交易明细（一笔完整的开平仓记为一条）。"""

    def __init__(self):
        self.records: list[dict] = []
        self._open_size: dict[str, int] = {}

    def notify_trade(self, trade):
        symbol = trade.data._name
        if trade.isopen and trade.justopened:
            self._open_size[symbol] = int(trade.size)
        if not trade.isclosed:
            return
        size = self._open_size.pop(symbol, 0)
        self.records.append(
            {
                "symbol": symbol,
                "entry_date": trade.open_datetime().date(),
                "exit_date": trade.close_datetime().date(),
                "size": size,
                "entry_price": float(trade.price),
                "pnl": float(trade.pnl),
                "pnl_net": float(trade.pnlcomm),
                "commission": float(trade.commission),
                "bars": int(trade.barlen),
            }
        )

    def get_analysis(self):
        return pd.DataFrame(self.records)


class OrderRecorder(bt.Analyzer):
    """成交订单明细（每次买卖一条）。"""

    def __init__(self):
        self.records: list[dict] = []

    def notify_order(self, order):
        if order.status != order.Completed:
            return
        self.records.append(
            {
                "date": order.data.datetime.date(0),
                "symbol": order.data._name,
                "side": "buy" if order.isbuy() else "sell",
                "size": int(abs(order.executed.size)),
                "price": float(order.executed.price),
                "value": float(abs(order.executed.size) * order.executed.price),
                "commission": float(order.executed.comm),
            }
        )

    def get_analysis(self):
        return pd.DataFrame(self.records)


def clean_value(value):
    """把 backtrader / numpy 数值转成可 JSON 化的原生类型。"""
    if isinstance(value, dict):
        return {key: clean_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_value(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    return value
