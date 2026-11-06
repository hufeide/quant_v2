"""A2 平台突破：放量突破近 N 日高点，且处于长期均线之上。"""
from __future__ import annotations

import backtrader as bt

from strategy.base import BaseStrategy
from strategy.registry import register_strategy


@register_strategy("breakout", "平台突破")
class BreakoutStrategy(BaseStrategy):
    def build_indicators(self, data):
        cfg = self.cfg
        lookback = int(cfg.get("lookback", 20))
        return {
            "highest": bt.indicators.Highest(data.high(-1), period=lookback),
            "vol_ma": bt.indicators.SMA(data.volume, period=int(cfg.get("volume_ma", 20))),
            "ma_trend": bt.indicators.SMA(data.close, period=int(cfg.get("ma_trend", 60))),
            "atr": bt.indicators.ATR(data, period=int(cfg.get("atr_period", 14))),
        }

    def signal(self, data, ind):
        cfg = self.cfg
        close = float(data.close[0])
        high_n = float(ind["highest"][0])
        vol_ma = float(ind["vol_ma"][0])
        if high_n <= 0 or vol_ma <= 0:
            return None

        buffer = float(cfg.get("breakout_buffer", 0.005))
        vol_ratio = float(cfg.get("volume_ratio", 1.5))
        if close <= high_n * (1 + buffer):
            return None
        if float(data.volume[0]) < vol_ma * vol_ratio:
            return None
        if close < float(ind["ma_trend"][0]):
            return None

        breakout_pct = close / high_n - 1.0
        volume_strength = float(data.volume[0]) / vol_ma
        score = min(1.0, breakout_pct / (buffer * 3)) * 55.0 + min(1.0, (volume_strength - vol_ratio) / 1.5) * 45.0
        return {
            "score": score,
            "reason": (
                f"突破{cfg.get('lookback', 20)}日高点{high_n:.2f} {breakout_pct:+.2%}, "
                f"量比{volume_strength:.2f}"
            ),
        }

    def custom_exit(self, data, ind, pos):
        if float(data.close[0]) < float(ind["ma_trend"][0]):
            return "跌破趋势均线"
        return None
