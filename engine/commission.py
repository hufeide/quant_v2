"""A 股交易成本模型：佣金（含最低佣金）+ 卖出印花税。"""
from __future__ import annotations

import backtrader as bt


class ChinaACommission(bt.CommInfoBase):
    params = (
        ("commission", 0.0003),
        ("stamp_duty", 0.0005),
        ("min_commission", 5.0),
        ("stocklike", True),
        ("commtype", bt.CommInfoBase.COMM_PERC),
        ("percabs", True),
    )

    def _getcommission(self, size, price, pseudoexec):
        if not size or not price:
            return 0.0
        value = abs(size) * price
        commission = max(value * self.p.commission, self.p.min_commission)
        if size < 0:  # 卖出收印花税
            commission += value * self.p.stamp_duty
        return commission
