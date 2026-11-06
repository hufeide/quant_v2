"""A1 缩量回踩：长期均线之上，价格回踩短均线且成交量萎缩。"""
from __future__ import annotations

import backtrader as bt

from strategy.base import BaseStrategy
from strategy.registry import register_strategy


@register_strategy("pullback", "缩量回踩")
class PullbackStrategy(BaseStrategy):
    def build_indicators(self, data):
        cfg = self.cfg
        return {
            "ma_trend": bt.indicators.SMA(data.close, period=int(cfg.get("ma_trend", 60))),
            "ma_pullback": bt.indicators.SMA(data.close, period=int(cfg.get("ma_pullback", 20))),
            "vol_ma": bt.indicators.SMA(data.volume, period=int(cfg.get("volume_ma", 20))),
            "rsi": bt.indicators.RSI(data.close, period=int(cfg.get("rsi_period", 14))),
            "atr": bt.indicators.ATR(data, period=int(cfg.get("atr_period", 14))),
        }

    def signal(self, data, ind):
        cfg = self.cfg
        close = float(data.close[0])
        ma_trend = float(ind["ma_trend"][0])
        ma_pull = float(ind["ma_pullback"][0])
        volume = float(data.volume[0])
        vol_ma = float(ind["vol_ma"][0])
        rsi = float(ind["rsi"][0])
        if vol_ma <= 0 or ma_pull <= 0:
            return None

        tol = float(cfg.get("pullback_tolerance", 0.02))
        distance = abs(close - ma_pull) / ma_pull
        shrink_ratio = float(cfg.get("volume_shrink_ratio", 0.7))
        rsi_max = float(cfg.get("rsi_max", 65))

        if close <= ma_trend:
            return None
        if distance > tol:
            return None
        if volume >= vol_ma * shrink_ratio:
            return None
        if rsi >= rsi_max:
            return None
        if float(data.close[-1]) <= float(ind["ma_pullback"][-1]):
            return None

        shrink_score = max(0.0, 1.0 - volume / vol_ma) * 45.0
        touch_score = max(0.0, 1.0 - distance / tol) * 30.0
        trend_score = min(1.0, (close / ma_trend - 1.0) / 0.10) * 25.0
        return {
            "score": shrink_score + touch_score + trend_score,
            "reason": (
                f"trend>{cfg.get('ma_trend', 60)}日均线, 回踩{cfg.get('ma_pullback', 20)}日均线, "
                f"量比{volume / vol_ma:.2f}, RSI{rsi:.1f}"
            ),
        }
