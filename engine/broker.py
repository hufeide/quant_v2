"""带 A 股成交约束的 Broker。

在 Backtrader 的 ``BackBroker`` 上做两处扩展：

1. ``_try_exec`` 前置校验：执行日没有 K 线（停牌/数据缺口）、成交量为 0、
   开盘价已在涨停（买）或跌停（卖）时，不调用父类成交逻辑。订单保持
   pending，backtrader 会在下一个交易日自动重试 —— 即“排队等待”，
   一字板连续多日也能逐日重试；超过 ``max_pending_bars`` 次后撤销。
2. ``filler`` 限制单笔成交量占当日成交量的比例，超出部分留在 pending。

每个子账户一个 broker，因此拒绝记录也按子账户分开。
"""
from __future__ import annotations

import math

import backtrader as bt

from engine.constraints import ConstraintEngine, RejectRecorder


class ChinaABroker(bt.brokers.BackBroker):
    params = (
        ("constraint_engine", None),
        ("reject_recorder", None),
        ("max_pending_bars", 10),
        ("lot_size", 100),
    )

    def init(self):
        super().init()
        engine = self.p.constraint_engine
        if engine is not None and engine.enabled and engine.participation_enabled:
            self.p.filler = self._cap_filler

    # ------------------------------------------------------------------
    def submit(self, order, check=True):
        recorder = self.p.reject_recorder
        if recorder is not None and order.transmit:
            recorder.mark_submitted(order.ref)
        return super().submit(order, check=check)

    # ------------------------------------------------------------------
    def _try_exec(self, order):
        engine = self.p.constraint_engine
        recorder = self.p.reject_recorder
        if engine is None or not engine.enabled or recorder is None:
            return super()._try_exec(order)

        data = order.data
        # datas[0] 为最早上市标的，其 K 线日期即当前回测时钟
        master_date = self.cerebro.datas[0].datetime.datetime(0).date()
        bar_date = data.datetime.datetime(0).date()

        if bar_date < master_date:
            # 当日该标的没有新 K 线：停牌或数据缺口，订单排队等待
            self._constraint_reject(order, "suspended", master_date, engine, recorder)
            return

        ok, reason = engine.check_execution(order, data)
        if not ok:
            self._constraint_reject(order, reason, master_date, engine, recorder)
            return

        super()._try_exec(order)

    # ------------------------------------------------------------------
    def _constraint_reject(self, order, reason: str, bar_date, engine: ConstraintEngine, recorder: RejectRecorder) -> None:
        recorder.record(order, order.data, reason, "reject", engine, bar_date)
        attempts = recorder.bump(order.ref)
        if attempts >= int(self.p.max_pending_bars):
            # 连续被拒达到上限：撤销并记录，避免永久挂单
            recorder.record(order, order.data, "max_wait_cancel", "cancel", engine, bar_date)
            order.cancel()
            self.notify(order)
            self._ococheck(order)
            self._bracketize(order, cancel=True)

    def _cap_filler(self, order, price, ago):
        remaining = int(abs(order.executed.remsize))
        engine = self.p.constraint_engine
        recorder = self.p.reject_recorder
        if ago is None or engine is None or recorder is None or not engine.participation_enabled:
            return remaining
        data = order.data
        volume = float(data.volume[0]) if len(data) else 0.0
        if not math.isfinite(volume) or volume <= 0:
            return remaining  # 停牌已由 _try_exec 拦截
        allowed = int(volume * engine.max_rate)
        if allowed >= remaining:
            return remaining
        lot = max(1, int(self.p.lot_size or 1))
        if order.isbuy():
            allowed = (allowed // lot) * lot
        if allowed <= 0:
            recorder.record(order, data, "volume_cap", "partial", engine, data.datetime.datetime(0).date())
            return 0
        recorder.record(order, data, "volume_cap", "partial", engine, data.datetime.datetime(0).date())
        return allowed
