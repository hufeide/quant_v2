"""A4 强者恒强：短均线高于长均线，中期涨幅与量能同步走强。"""
from __future__ import annotations

import backtrader as bt

from strategy.base import BaseStrategy
from strategy.registry import register_strategy


@register_strategy("momentum", "强者恒强")
class MomentumStrategy(BaseStrategy):
    def build_indicators(self, data):
        cfg = self.cfg
        period = int(cfg.get("roc_period", 20))
        return {
            "ma_fast": bt.indicators.SMA(data.close, period=int(cfg.get("ma_fast", 20))),
            "ma_slow": bt.indicators.SMA(data.close, period=int(cfg.get("ma_slow", 60))),
            "vol_ma": bt.indicators.SMA(data.volume, period=int(cfg.get("volume_ma", 20))),
            "roc": (data.close - data.close(-period)) / data.close(-period),
            "atr": bt.indicators.ATR(data, period=int(cfg.get("atr_period", 14))),
        }

    def signal(self, data, ind):
        cfg = self.cfg
        close = float(data.close[0])
        ma_fast = float(ind["ma_fast"][0])
        ma_slow = float(ind["ma_slow"][0])
        roc = float(ind["roc"][0])
        vol_ma = float(ind["vol_ma"][0])
        if ma_slow <= 0 or vol_ma <= 0:
            return None

        roc_min = float(cfg.get("roc_min", 0.05))
        if not (close > ma_fast > ma_slow):
            return None
        if roc < roc_min:
            return None
        if float(data.volume[0]) < vol_ma:
            return None

        roc_score = min(1.0, roc / 0.20) * 60.0
        spread_score = min(1.0, (ma_fast / ma_slow - 1.0) / 0.06) * 40.0
        return {
            "score": roc_score + spread_score,
            "reason": f"{cfg.get('roc_period', 20)}日涨幅{roc:.1%}, 均线多头排列",
        }

    def custom_exit(self, data, ind, pos):
        if float(data.close[0]) < float(ind["ma_fast"][0]):
            return "跌破短均线"
        return None
