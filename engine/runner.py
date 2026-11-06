"""回测引擎：总账户按比例拆分为多个独立子账户，分别用 Cerebro 运行后汇总。

设计取舍：
- 每个子账户拥有独立的 broker / 现金 / 持仓，避免不同策略对同一标的的
  多空持仓被 netting 合并，从而保证“各子账户独立记账”是真实的。
- 总账户净值 = Σ 子账户净值 + 现金池（cash_reserve），逐日对齐求和。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import backtrader as bt
import pandas as pd

from core.config import _deep_merge
from engine.analyzers import (
    EquityCurveAnalyzer,
    OrderRecorder,
    PositionSnapshotAnalyzer,
    TradeRecorder,
)
from engine.broker import ChinaABroker
from engine.commission import ChinaACommission
from engine.constraints import ConstraintEngine, RejectRecorder
from data.timeline import MemberTimeline
from strategy.registry import get_strategy


@dataclass
class AccountResult:
    name: str
    strategy: str
    weight: float
    initial_cash: float
    equity: pd.DataFrame = field(default_factory=pd.DataFrame)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    orders: pd.DataFrame = field(default_factory=pd.DataFrame)
    rejected: pd.DataFrame = field(default_factory=pd.DataFrame)
    order_stats: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def final_positions(self) -> pd.DataFrame:
        if self.positions.empty or self.equity.empty:
            return pd.DataFrame(columns=["symbol", "size", "price", "value", "weight"])
        last = pd.to_datetime(self.equity["date"].max()).date()
        dates = pd.to_datetime(self.positions["date"]).dt.date
        df = self.positions[dates == last].copy()
        total = self.equity["value"].iloc[-1]
        df["weight"] = df["value"] / total if total else 0.0
        return df[["symbol", "size", "price", "value", "weight"]].sort_values("value", ascending=False)


def _add_feeds(cerebro: bt.Cerebro, panel: dict[str, pd.DataFrame]):
    for symbol, df in panel.items():
        feed_df = df.copy()
        feed_df["openinterest"] = 0.0
        cerebro.adddata(
            bt.feeds.PandasData(dataname=feed_df, name=symbol, plot=False, openinterest="openinterest")
        )


def _slice_by_date(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df
    dates = pd.to_datetime(df["date"]).dt.date
    return df[(dates >= start) & (dates <= end)].reset_index(drop=True)


def run_account(
    cfg: dict,
    sub: dict,
    panel: dict[str, pd.DataFrame],
    master_cash: float,
    timeline: MemberTimeline | None = None,
) -> AccountResult:
    strategy_cls = get_strategy(sub["strategy"])
    risk_cfg = _deep_merge(cfg.get("risk_defaults") or {}, sub.get("risk") or {})
    sizer_cfg = _deep_merge(cfg.get("sizer_defaults") or {}, sub.get("sizer") or {})
    weight = float(sub["weight"])
    initial_cash = master_cash * weight
    broker_cfg = cfg["broker"]
    engine_cfg = cfg["engine"]
    data_cfg = cfg["data"]

    constraint_cfg = cfg.get("constraints") or {}
    engine = ConstraintEngine.from_config(constraint_cfg, names=cfg.get("_symbol_names") or {})
    recorder = RejectRecorder(account=sub["name"], enabled=engine.enabled)
    broker = ChinaABroker(
        constraint_engine=engine,
        reject_recorder=recorder,
        max_pending_bars=int(constraint_cfg.get("max_pending_bars", 10)),
        lot_size=int(engine_cfg.get("lot_size", 100)),
    )
    broker.setcash(initial_cash)
    broker.addcommissioninfo(
        ChinaACommission(
            commission=float(broker_cfg.get("commission", 0.0003)),
            stamp_duty=float(broker_cfg.get("stamp_duty", 0.0005)),
            min_commission=float(broker_cfg.get("min_commission", 5.0)),
        )
    )
    broker.set_slippage_perc(
        perc=float(broker_cfg.get("slippage_perc", 0)),
        slip_open=bool(broker_cfg.get("slippage_open", True)),
        slip_match=True,
        slip_out=False,
    )

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.setbroker(broker)
    _add_feeds(cerebro, panel)
    cerebro.addstrategy(
        strategy_cls,
        account_name=sub["name"],
        strategy_params=sub.get("params") or {},
        risk_params=risk_cfg,
        sizer_params=sizer_cfg,
        trade_start=engine_cfg["_trade_start"],
        lot_size=int(engine_cfg.get("lot_size", 100)),
        verbose=bool(engine_cfg.get("verbose", False)),
        member_timeline=timeline,
        skip_stale_bars=engine.enabled,
    )
    cerebro.addanalyzer(EquityCurveAnalyzer, _name="equity")
    cerebro.addanalyzer(PositionSnapshotAnalyzer, _name="positions")
    cerebro.addanalyzer(TradeRecorder, _name="trades")
    cerebro.addanalyzer(OrderRecorder, _name="orders")

    strat = cerebro.run()[0]

    equity = strat.analyzers.equity.get_analysis()
    if not equity.empty:
        equity["date"] = pd.to_datetime(equity["date"])
    positions = strat.analyzers.positions.get_analysis()
    trades = strat.analyzers.trades.get_analysis()
    orders = strat.analyzers.orders.get_analysis()
    rejected = recorder.to_frame()

    start, end = data_cfg["_start_date"], data_cfg["_end_date"]
    equity = _slice_by_date(equity, start, end)
    positions = _slice_by_date(positions, start, end)
    orders = _slice_by_date(orders, start, end)
    rejected = _slice_by_date(rejected, start, end)
    if not trades.empty:
        entry_dates = pd.to_datetime(trades["entry_date"]).dt.date
        trades = trades[(entry_dates >= start) & (entry_dates <= end)].reset_index(drop=True)

    stats = recorder.stats()
    stats["orders_submitted"] = len(recorder.submitted)
    if not rejected.empty:
        rej = rejected[rejected["kind"] == "reject"]
        cancels = rejected[rejected["kind"] == "cancel"]
        stats["orders_rejected"] = int(rej["order_ref"].nunique())
        stats["reject_events"] = int(len(rej))
        stats["reject_rate"] = (
            stats["orders_rejected"] / stats["orders_submitted"] if stats["orders_submitted"] else 0.0
        )
        for reason in ("limit_up", "limit_down", "suspended", "no_volume"):
            stats[reason] = int((rej["reason"] == reason).sum())
        stats["max_wait_cancel"] = int((cancels["reason"] == "max_wait_cancel").sum())
        stats["partial_events"] = int((rejected["kind"] == "partial").sum())

    return AccountResult(
        name=sub["name"],
        strategy=sub["strategy"],
        weight=weight,
        initial_cash=initial_cash,
        equity=equity,
        positions=positions,
        trades=trades,
        orders=orders,
        rejected=rejected,
        order_stats=stats,
    )


@dataclass
class MasterResult:
    accounts: list[AccountResult]
    cash_reserve: float
    equity: pd.DataFrame
    metrics: dict[str, Any] = field(default_factory=dict)


def run_portfolio(
    cfg: dict,
    panel: dict[str, pd.DataFrame],
    timeline: MemberTimeline | None = None,
    names: dict[str, str] | None = None,
) -> MasterResult:
    master_cash = float(cfg["broker"]["cash"])
    cash_reserve_ratio = float(cfg["accounts"]["cash_reserve"])
    reserve_value = master_cash * cash_reserve_ratio
    cfg["_symbol_names"] = dict(names or {})

    accounts = []
    for sub in cfg["accounts"]["subaccounts"]:
        if not sub.get("enabled", True):
            continue
        accounts.append(run_account(cfg, sub, panel, master_cash, timeline))
    if not accounts:
        raise ValueError("没有任何启用的子账户")

    # 逐日对齐汇总总净值
    frames = []
    for account in accounts:
        if account.equity.empty:
            continue
        frames.append(account.equity.set_index("date")[["value", "cash", "exposure", "positions"]])
    if not frames:
        raise RuntimeError("所有子账户均无净值记录")

    master = pd.concat(frames, axis=1, keys=[a.name for a in accounts]).sort_index().ffill()
    equity = pd.DataFrame(index=master.index)
    equity["value"] = master.xs("value", axis=1, level=1).sum(axis=1) + reserve_value
    equity["cash"] = master.xs("cash", axis=1, level=1).sum(axis=1) + reserve_value
    equity["exposure"] = master.xs("exposure", axis=1, level=1).sum(axis=1)
    equity["positions"] = master.xs("positions", axis=1, level=1).sum(axis=1)
    equity = equity.reset_index().rename(columns={"index": "date"})
    equity.attrs["cash_reserve"] = reserve_value

    return MasterResult(
        accounts=accounts,
        cash_reserve=reserve_value,
        equity=equity,
    )
