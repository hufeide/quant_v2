"""组合管理层（公共模块，所有策略共用）：
- PortfolioManager：持仓数量与信号排序
- PositionSizer：ATR 风险预算 -> 目标股数
- RiskManager：单票/总仓位检查 + 回撤熔断
"""
from __future__ import annotations


class PortfolioManager:
    def __init__(self, risk_cfg: dict):
        self.max_positions = int(risk_cfg.get("max_positions", 8))

    def available_slots(self, current_positions: int) -> int:
        return max(0, self.max_positions - current_positions)

    def rank(self, candidates: list[dict]) -> list[dict]:
        """按信号评分从高到低排序，评分同分时保留原始顺序。"""
        return sorted(candidates, key=lambda item: item.get("score", 0), reverse=True)


class PositionSizer:
    """按单笔风险预算计算股数，并受单票最大市值约束。"""

    def __init__(self, sizer_cfg: dict):
        self.cfg = sizer_cfg

    def calculate(self, equity: float, price: float, atr: float, lot_size: int = 100) -> int:
        if price <= 0 or equity <= 0:
            return 0
        risk_pct = float(self.cfg.get("risk_per_trade_pct", 0.01))
        atr_mult = float(self.cfg.get("atr_multiple", 2.5))
        max_pos_pct = float(self.cfg.get("max_position_pct", 0.25))
        min_stop_pct = float(self.cfg.get("min_stop_pct", 0.015))

        stop_distance = max(atr * atr_mult, price * min_stop_pct)
        size_by_risk = equity * risk_pct / stop_distance
        size_by_cap = equity * max_pos_pct / price
        size = min(size_by_risk, size_by_cap)
        return int(size // lot_size) * lot_size


class RiskManager:
    def __init__(self, risk_cfg: dict):
        self.max_total_exposure_pct = float(risk_cfg.get("max_total_exposure_pct", 0.95))
        self.max_position_pct = float(risk_cfg.get("max_position_pct", 0.25))
        self.max_drawdown_stop = float(risk_cfg.get("max_drawdown_stop", 0.35))
        self.peak = 0.0
        self.drawdown = 0.0

    def update(self, equity: float) -> None:
        self.peak = max(self.peak, equity)
        self.drawdown = 0.0 if self.peak <= 0 else 1.0 - equity / self.peak

    @property
    def halted(self) -> bool:
        return self.drawdown >= self.max_drawdown_stop

    def approve(self, size: int, price: float, equity: float, current_exposure: float) -> bool:
        if size <= 0 or self.halted:
            return False
        new_value = size * price
        if new_value > equity * self.max_position_pct:
            return False
        if current_exposure + new_value > equity * self.max_total_exposure_pct:
            return False
        return True
