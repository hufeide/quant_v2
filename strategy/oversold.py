"""A3 超跌反弹：RSI 超卖且价格显著低于短均线，RSI 修复后离场。"""
from __future__ import annotations

import backtrader as bt

from strategy.base import BaseStrategy
from strategy.registry import register_strategy


@register_strategy("oversold", "超跌反弹")
class OversoldStrategy(BaseStrategy):
    def build_indicators(self, data):
        cfg = self.cfg
        return {
            "rsi": bt.indicators.RSI(data.close, period=int(cfg.get("rsi_period", 14))),
            "ma_filter": bt.indicators.SMA(data.close, period=int(cfg.get("ma_filter", 20))),
            "atr": bt.indicators.ATR(data, period=int(cfg.get("atr_period", 14))),
        }

    def signal(self, data, ind):
        cfg = self.cfg
        close = float(data.close[0])
        ma_filter = float(ind["ma_filter"][0])
        rsi = float(ind["rsi"][0])
        if ma_filter <= 0:
            return None

        rsi_oversold = float(cfg.get("rsi_oversold", 30))
        drop_pct = float(cfg.get("drop_pct", 0.10))
        if rsi >= rsi_oversold:
            return None
        if close >= ma_filter * (1 - drop_pct):
            return None

        drop_actual = 1 - close / ma_filter
        score = min(1.0, (rsi_oversold - rsi) / 15.0) * 55.0 + min(1.0, drop_actual / (drop_pct * 2)) * 45.0
        return {
            "score": score,
            "reason": f"RSI={rsi:.1f} 超卖, 偏离{cfg.get('ma_filter', 20)}日均线{drop_actual:.1%}",
        }

    def custom_exit(self, data, ind, pos):
        recover = float(self.cfg.get("rsi_recover", 55))
        if float(ind["rsi"][0]) >= recover:
            return "RSI修复"
        return None
