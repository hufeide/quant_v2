"""A 股成交约束：涨跌停、停牌、成交量参与率，以及被拒订单记录。

规则口径
--------
- 涨跌停：主板 ±10%、科创板 ±20%、创业板 ±20%、北交所 ±30%；
  主板 ST 股 ±5%（科创板/创业板 ST 仍为 ±20%）。价格按 0.01 元四舍五入。
  ST 判定：``constraints.limit.st_symbols`` 显式配置，或标的名称含 "ST"。
  注意：ST 状态的历史区间无法从免费数据源完整还原，名称口径属于近似。
- 停牌：执行日该标的没有 K 线（数据缺口）或成交量为 0/缺失 → 当日不得成交。
  订单保持 pending，在下一个有行情的交易日继续尝试。
- 成交量参与率：单笔成交不超过当日成交量的 ``max_rate``，超出部分留在
  pending 队列次日继续，避免大单穿透。
- 连续被拒超过 ``max_pending_bars`` 次的订单会被撤销并记录（避免永久挂单）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

import backtrader as bt
import pandas as pd

SIDE_LABELS = {"buy": "买入", "sell": "卖出"}


def board_of(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("688", "689")):
        return "star"
    if c.startswith(("300", "301")):
        return "chinext"
    if c.startswith(("4", "8", "92")):
        return "bse"
    return "main"


def round_to_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    exp = Decimal(str(tick))
    return float(Decimal(str(value)).quantize(exp, rounding=ROUND_HALF_UP))


@dataclass
class ConstraintEngine:
    enabled: bool = False
    limit_enabled: bool = True
    suspension_enabled: bool = True
    require_volume: bool = True
    participation_enabled: bool = True
    max_rate: float = 0.10
    tick: float = 0.01
    pct_main: float = 0.10
    pct_star: float = 0.20
    pct_chinext: float = 0.20
    pct_bse: float = 0.30
    pct_st: float = 0.05
    st_symbols: frozenset[str] = frozenset()
    names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: dict, names: dict[str, str] | None = None) -> "ConstraintEngine":
        cfg = cfg or {}
        limit_cfg = cfg.get("limit") or {}
        susp = cfg.get("suspension") or {}
        part = cfg.get("participation") or {}
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            limit_enabled=bool(limit_cfg.get("enabled", True)),
            suspension_enabled=bool(susp.get("enabled", True)),
            require_volume=bool(susp.get("require_volume", True)),
            participation_enabled=bool(part.get("enabled", True)),
            max_rate=float(part.get("max_rate", 0.10)),
            tick=float(limit_cfg.get("tick", 0.01)),
            pct_main=float(limit_cfg.get("main", 0.10)),
            pct_star=float(limit_cfg.get("star", 0.20)),
            pct_chinext=float(limit_cfg.get("chinext", 0.20)),
            pct_bse=float(limit_cfg.get("bse", 0.30)),
            pct_st=float(limit_cfg.get("st", 0.05)),
            st_symbols=frozenset(str(x).zfill(6) for x in limit_cfg.get("st_symbols") or []),
            names=dict(names or {}),
        )

    # ------------------------------------------------------------------
    def is_st(self, code: str) -> bool:
        code = str(code).zfill(6)
        if code in self.st_symbols:
            return True
        return "ST" in (self.names.get(code) or "").upper()

    def limit_pct(self, code: str) -> float:
        board = board_of(code)
        if self.is_st(code):
            # 科创板/创业板 ST 股不适用主板 5% 口径
            return self.pct_star if board == "star" else self.pct_chinext if board == "chinext" else self.pct_st
        return {
            "star": self.pct_star,
            "chinext": self.pct_chinext,
            "bse": self.pct_bse,
            "main": self.pct_main,
        }[board]

    def limit_prices(self, code: str, prev_close: float) -> tuple[float, float]:
        pct = self.limit_pct(code)
        up = round_to_tick(prev_close * (1 + pct), self.tick)
        down = round_to_tick(prev_close * (1 - pct), self.tick)
        return up, down

    def check_execution(self, order, data) -> tuple[bool, str]:
        """在执行 bar 上检查涨跌停/停牌/零成交，返回 (是否可成交, 拒绝原因)。"""
        code = order.data._name
        try:
            open_price = float(data.open[0])
            volume = float(data.volume[0])
        except Exception:
            return False, "missing_bar"
        if not math.isfinite(open_price) or open_price <= 0:
            return False, "suspended"
        if self.require_volume and (not math.isfinite(volume) or volume <= 0):
            return False, "no_volume"

        if self.limit_enabled and len(data) >= 2:
            prev_close = float(data.close[-1])
            if math.isfinite(prev_close) and prev_close > 0:
                up, down = self.limit_prices(code, prev_close)
                if order.isbuy() and open_price >= up - 1e-9:
                    return False, "limit_up"
                if not order.isbuy() and open_price <= down + 1e-9:
                    return False, "limit_down"
        return True, ""

    def limits_for_record(self, code: str, data) -> tuple[float | None, float | None]:
        if len(data) < 2:
            return None, None
        prev_close = float(data.close[-1])
        if not math.isfinite(prev_close) or prev_close <= 0:
            return None, None
        return self.limit_prices(code, prev_close)


@dataclass
class RejectRecorder:
    """订单拒绝/部分成交记录。每发生一次即一行，便于审计。"""

    account: str = ""
    enabled: bool = True
    events: list[dict] = field(default_factory=list)
    submitted: set[int] = field(default_factory=set)
    attempts: dict[int, int] = field(default_factory=dict)

    def mark_submitted(self, ref: int) -> None:
        self.submitted.add(ref)

    def record(self, order, data, reason: str, kind: str, engine: ConstraintEngine | None, bar_date) -> None:
        if not self.enabled:
            return
        code = order.data._name
        up, down = engine.limits_for_record(code, data) if engine else (None, None)
        prev_close = float(data.close[-1]) if len(data) >= 2 else None
        volume = float(data.volume[0]) if len(data) else 0.0
        self.events.append(
            {
                "date": bar_date,
                "account": self.account,
                "symbol": code,
                "side": "buy" if order.isbuy() else "sell",
                "size": int(abs(order.created.size)),
                "exec_type": "market" if order.exectype == bt.Order.Market else str(order.exectype),
                "kind": kind,
                "reason": reason,
                "order_ref": int(order.ref),
                "attempt": self.attempts.get(order.ref, 0) + (1 if kind == "reject" else 0),
                "open": float(data.open[0]) if len(data) else None,
                "prev_close": prev_close,
                "limit_up": up,
                "limit_down": down,
                "volume": volume,
            }
        )

    def bump(self, ref: int) -> int:
        self.attempts[ref] = self.attempts.get(ref, 0) + 1
        return self.attempts[ref]

    def to_frame(self) -> pd.DataFrame:
        cols = [
            "date",
            "account",
            "symbol",
            "side",
            "size",
            "exec_type",
            "kind",
            "reason",
            "order_ref",
            "attempt",
            "open",
            "prev_close",
            "limit_up",
            "limit_down",
            "volume",
        ]
        if not self.events:
            return pd.DataFrame(columns=cols)
        return pd.DataFrame(self.events, columns=cols)

    def stats(self) -> dict:
        rejects = [e for e in self.events if e["kind"] == "reject"]
        partials = [e for e in self.events if e["kind"] == "partial"]
        rejected_refs = {e["order_ref"] for e in rejects}
        by_reason: dict[str, int] = {}
        for e in rejects:
            by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
        submitted = len(self.submitted)
        return {
            "account": self.account,
            "orders_submitted": submitted,
            "orders_rejected": len(rejected_refs),
            "reject_rate": (len(rejected_refs) / submitted) if submitted else 0.0,
            "reject_events": len(rejects),
            "limit_up": by_reason.get("limit_up", 0),
            "limit_down": by_reason.get("limit_down", 0),
            "suspended": by_reason.get("suspended", 0),
            "no_volume": by_reason.get("no_volume", 0),
            "max_wait_cancel": by_reason.get("max_wait_cancel", 0),
            "partial_events": len(partials),
            "partial_bars": len({(e["order_ref"], e["date"]) for e in partials}),
        }
