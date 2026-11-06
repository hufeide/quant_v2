"""策略基类：把 Backtrader Strategy 作为交易流程的协调者。

职责划分（与参考文档一致）：
- 子类只负责指标构建与信号/离场判断
- 仓位大小由 PositionSizer 统一计算
- 持仓数量/总仓位/回撤熔断由 RiskManager 统一检查
- 订单执行交给 Backtrader broker，基类只做去重与状态记录
"""
from __future__ import annotations

import backtrader as bt

from engine.portfolio import PortfolioManager, PositionSizer, RiskManager


class BaseStrategy(bt.Strategy):
    params = (
        ("account_name", "account"),
        ("strategy_params", None),
        ("risk_params", None),
        ("sizer_params", None),
        ("trade_start", None),
        ("lot_size", 100),
        ("verbose", False),
        ("member_timeline", None),
        ("skip_stale_bars", False),
    )

    strategy_name = "base"
    display_name = "基类"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def __init__(self):
        self.cfg = dict(self.p.strategy_params or {})
        self.risk_cfg = dict(self.p.risk_params or {})
        self.sizer_cfg = dict(self.p.sizer_params or {})

        self.pm = PortfolioManager(self.risk_cfg)
        self.position_sizer = PositionSizer(self.sizer_cfg)
        self.risk_mgr = RiskManager(self.risk_cfg)

        self.inds = {}
        for data in self.datas:
            self.inds[data._name] = self.build_indicators(data)

        self.pending: dict[str, bt.Order] = {}
        self.entry_meta: dict[str, dict] = {}
        self.trade_start = self.p.trade_start
        self.member_timeline = self.p.member_timeline
        self.skip_stale_bars = bool(self.p.skip_stale_bars)
        self._logged_halt = False

    def build_indicators(self, data) -> dict:
        raise NotImplementedError

    def signal(self, data, ind) -> dict | None:
        """返回 {"score": float, ...} 表示买入信号；None 表示无信号。"""
        return None

    def custom_exit(self, data, ind, pos) -> str | None:
        """策略自定义离场条件，返回原因字符串。"""
        return None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def next(self):
        today = self.datas[0].datetime.date(0)
        if self.trade_start and today < self.trade_start:
            return

        equity = self.broker.getvalue()
        self.risk_mgr.update(equity)

        self._process_exits(today)
        self._process_entries(today, equity)

    def prenext(self):
        # 历史口径会把晚上市标的（如 2025 年上市）纳入 parallel feed，
        # Backtrader 默认要等所有数据（含其指标）预热完成才进入 next()，
        # 会让整段回测被迫空仓到晚上市标的热身结束。此处提前驱动主循环，
        # 未上市/数据不足的标的由空数据与 NaN 信号自然跳过。
        self.next()

    def _process_exits(self, today):
        for data in self.datas:
            name = data._name
            if len(data) == 0:
                continue
            if self.skip_stale_bars and data.datetime.date(0) != today:
                # 当日无 K 线（停牌/数据缺口）：不更新持仓峰值与持有天数，也不产生新离场单
                continue
            pos = self.getposition(data)
            if pos.size <= 0:
                self.entry_meta.pop(name, None)
                continue
            if name in self.pending:
                continue

            meta = self.entry_meta.get(name)
            if not meta:
                continue
            ind = self.inds[name]
            close = data.close[0]
            atr = float(ind["atr"][0])
            meta["peak"] = max(meta["peak"], float(data.high[0]))
            meta["bars"] += 1

            stop_atr = float(self.risk_cfg.get("atr_multiple", 2.0))
            stop_price = meta["entry_price"] - stop_atr * meta["atr"]
            trailing = float(self.risk_cfg.get("trailing_stop_atr", 0) or 0)
            if trailing > 0:
                stop_price = max(stop_price, meta["peak"] - trailing * atr)
            take_atr = float(self.risk_cfg.get("take_profit_atr", 0) or 0)
            take_price = meta["entry_price"] + take_atr * meta["atr"] if take_atr > 0 else None
            max_hold = int(self.risk_cfg.get("max_hold_days", 9999))

            reason = None
            if close <= stop_price:
                reason = "stop_loss"
            elif take_price is not None and close >= take_price:
                reason = "take_profit"
            elif meta["bars"] >= max_hold:
                reason = "max_hold"
            else:
                reason = self.custom_exit(data, ind, pos)

            if reason:
                self.pending[name] = self.close(data=data)
                self.log(f"{today} {name} 离场触发：{reason}，收盘 {close:.2f}")

    def _process_entries(self, today, equity):
        if self.risk_mgr.halted:
            if not self._logged_halt:
                self.log(
                    f"{today} 子账户回撤 {self.risk_mgr.drawdown:.1%} 超过阈值 "
                    f"{self.risk_mgr.max_drawdown_stop:.0%}，停止开新仓"
                )
                self._logged_halt = True
            return

        current_positions = sum(1 for d in self.datas if self.getposition(d).size > 0)
        slots = self.pm.available_slots(current_positions)
        if slots <= 0:
            return

        candidates = []
        for data in self.datas:
            name = data._name
            if len(data) == 0:
                # 尚未上市／尚无任何 K 线：指标不可用，跳过
                continue
            if self.skip_stale_bars and data.datetime.date(0) != today:
                continue
            if self.member_timeline is not None and not self.member_timeline.contains(name, today):
                continue
            if self.getposition(data).size > 0 or name in self.pending:
                continue
            sig = self.signal(data, self.inds[name])
            if sig and sig.get("score", 0) > 0:
                sig["symbol"] = name
                candidates.append(sig)

        if not candidates:
            return
        ranked = self.pm.rank(candidates)
        exposure = self._current_exposure()
        daily_cap = int(self.risk_cfg.get("max_daily_entries", 3))

        for idx, sig in enumerate(ranked[:slots]):
            if idx >= daily_cap:
                break
            data = self.getdatabyname(sig["symbol"])
            price = float(data.close[0])
            atr = float(self.inds[data._name]["atr"][0])
            equity = self.broker.getvalue()
            size = self.position_sizer.calculate(equity=equity, price=price, atr=atr, lot_size=self.p.lot_size)
            if size <= 0:
                continue
            if not self.risk_mgr.approve(
                size=size,
                price=price,
                equity=equity,
                current_exposure=exposure,
            ):
                continue
            self.pending[sig["symbol"]] = self.buy(data=data, size=size)
            exposure += size * price
            self.log(
                f"{today} {sig['symbol']} 买入信号 score={sig['score']:.1f} "
                f"reason={sig.get('reason', '')} size={size} close={price:.2f}"
            )

    def _current_exposure(self) -> float:
        total = 0.0
        for data in self.datas:
            pos = self.getposition(data)
            if pos.size > 0:
                total += pos.size * float(data.close[0])
        return total

    # ------------------------------------------------------------------
    # 订单回报
    # ------------------------------------------------------------------
    def notify_order(self, order):
        name = order.data._name
        status = order.status
        if status in (order.Submitted, order.Accepted):
            return
        if status == order.Completed:
            if order.isbuy():
                ind = self.inds[name]
                self.entry_meta[name] = {
                    "entry_price": float(order.executed.price),
                    "entry_bar": len(self),
                    "atr": float(ind["atr"][0]),
                    "bars": 0,
                    "peak": float(order.executed.price),
                }
            else:
                self.entry_meta.pop(name, None)
        self.pending.pop(name, None)

    def log(self, message: str):
        if self.p.verbose:
            print(f"[{self.p.account_name}] {message}")
