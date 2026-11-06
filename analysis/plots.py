"""净值曲线与回撤图（PNG 产物）。"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager

_CJK_FONTS = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS"]


def _setup_font() -> bool:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in _CJK_FONTS:
        if name in installed:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return True
    return False


def plot_equity(master_equity: pd.DataFrame, accounts: list, path: Path) -> None:
    has_cjk = _setup_font()
    labels = (
        {
            "title": "总账户与子账户净值曲线",
            "curve": "累计收益率",
            "dd": "总账户回撤",
            "master": "总账户",
            "x": "日期",
            "y": "累计收益率",
        }
        if has_cjk
        else {
            "title": "Equity Curves (Master & Sub-accounts)",
            "curve": "Cumulative Return",
            "dd": "Master Drawdown",
            "master": "Master",
            "x": "Date",
            "y": "Cumulative Return",
        }
    )

    fig, axes = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1.2]}
    )
    ax = axes[0]

    for account in accounts:
        if account.equity.empty:
            continue
        series = account.equity.set_index(pd.to_datetime(account.equity["date"]))["value"]
        ax.plot(series.index, series / series.iloc[0] - 1, linewidth=1.0, label=f"{account.name}")

    master = master_equity.set_index(pd.to_datetime(master_equity["date"]))["value"]
    ax.plot(master.index, master / master.iloc[0] - 1, color="black", linewidth=2.0, label=labels["master"])
    ax.set_title(labels["title"])
    ax.set_ylabel(labels["y"])
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=8, ncol=3)
    ax.axhline(0, color="gray", linewidth=0.6)

    dd = master / master.cummax() - 1.0
    axes[1].fill_between(dd.index, dd.values, 0, color="#c0392b", alpha=0.35)
    axes[1].plot(dd.index, dd.values, color="#c0392b", linewidth=1.0)
    axes[1].set_title(labels["dd"])
    axes[1].set_xlabel(labels["x"])
    axes[1].grid(alpha=0.3)

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
