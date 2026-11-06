"""绩效指标与月度收益表。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _safe(value, digits=4):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame, cfg: dict) -> dict:
    """根据净值曲线与成交明细计算核心绩效指标。"""
    if equity.empty:
        return {}
    values = equity["value"].astype(float)
    start_value = float(values.iloc[0])
    end_value = float(values.iloc[-1])
    total_return = end_value / start_value - 1.0

    dates = pd.to_datetime(equity["date"])
    years = max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 1 / 365.25)
    cagr = (end_value / start_value) ** (1.0 / years) - 1.0 if end_value > 0 else -1.0

    daily = values.pct_change().dropna()
    periods = int(cfg["engine"].get("trading_days_per_year", 252))
    ann_vol = float(daily.std(ddof=0) * math.sqrt(periods)) if len(daily) > 1 else 0.0
    rf = float(cfg["engine"].get("risk_free_rate", 0.0))
    excess = daily.mean() * periods - rf
    sharpe = excess / ann_vol if ann_vol > 0 else 0.0

    running_max = values.cummax()
    drawdown = values / running_max - 1.0
    max_drawdown = float(drawdown.min())
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else 0.0

    wins = losses = 0
    avg_win = avg_loss = profit_factor = 0.0
    n_trades = 0
    if not trades.empty:
        pnl = trades["pnl_net"].astype(float)
        n_trades = int(len(pnl))
        wins = int((pnl > 0).sum())
        losses = int((pnl < 0).sum())
        gross_win = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl < 0].sum())
        avg_win = float(pnl[pnl > 0].mean()) if wins else 0.0
        avg_loss = float(pnl[pnl < 0].mean()) if losses else 0.0
        profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    return {
        "start_value": _safe(start_value, 2),
        "end_value": _safe(end_value, 2),
        "total_return": _safe(total_return),
        "annual_return": _safe(cagr),
        "annual_volatility": _safe(ann_vol),
        "sharpe": _safe(sharpe),
        "max_drawdown": _safe(max_drawdown),
        "calmar": _safe(calmar),
        "trades": n_trades,
        "win_rate": _safe(wins / n_trades if n_trades else 0.0),
        "avg_win": _safe(avg_win, 2),
        "avg_loss": _safe(avg_loss, 2),
        "profit_factor": _safe(profit_factor),
        "total_commission": _safe(float(trades["commission"].sum()) if n_trades else 0.0, 2),
    }


def monthly_returns(equity: pd.DataFrame) -> pd.DataFrame:
    """按月收益表：每个自然月的收益率 + 当年累计。"""
    if equity.empty:
        return pd.DataFrame()
    series = equity.set_index(pd.to_datetime(equity["date"]))["value"].astype(float)
    month_end = series.resample("ME").last()
    monthly = month_end.pct_change()
    first_month = month_end.iloc[0] / series.iloc[0] - 1.0
    if not math.isnan(first_month):
        monthly.iloc[0] = first_month
    table = pd.DataFrame(
        {
            "year": monthly.index.year,
            "month": monthly.index.month,
            "return": monthly.values,
        }
    )
    pivot = table.pivot(index="year", columns="month", values="return")
    pivot = pivot.reindex(columns=range(1, 13))
    pivot["YTD"] = (1 + pivot.fillna(0)).prod(axis=1) - 1
    return pivot


def format_metrics_table(metrics_by_account: dict[str, dict], master: dict) -> pd.DataFrame:
    keys = [
        ("total_return", "总收益"),
        ("annual_return", "年化收益"),
        ("annual_volatility", "年化波动"),
        ("sharpe", "夏普"),
        ("max_drawdown", "最大回撤"),
        ("calmar", "Calmar"),
        ("trades", "交易次数"),
        ("win_rate", "胜率"),
        ("profit_factor", "盈亏比"),
        ("total_commission", "总佣金"),
    ]
    rows = []
    for name, metrics in list(metrics_by_account.items()) + [("总账户", master)]:
        row = {"账户": name}
        for key, label in keys:
            value = metrics.get(key)
            if value is None:
                row[label] = "-"
            elif key in ("total_return", "annual_return", "annual_volatility", "max_drawdown", "win_rate"):
                row[label] = f"{value:.2%}"
            elif key in ("sharpe", "calmar", "profit_factor"):
                row[label] = f"{value:.2f}"
            elif key in ("trades",):
                row[label] = f"{int(value)}"
            else:
                row[label] = f"{value:,.2f}"
        rows.append(row)
    return pd.DataFrame(rows)


def drawdown_series(equity: pd.DataFrame) -> pd.Series:
    values = equity.set_index(pd.to_datetime(equity["date"]))["value"].astype(float)
    return values / values.cummax() - 1.0
