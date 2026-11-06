"""结果输出：CSV / PNG / Markdown 报告。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import yaml

from analysis.metrics import compute_metrics, format_metrics_table, monthly_returns
from analysis.plots import plot_equity
from core.config import describe_config


def _as_serializable(cfg: dict) -> dict:
    result = {}
    for key, value in cfg.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict):
            result[key] = _as_serializable(value)
        elif isinstance(value, dt.date):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def _fmt_pct(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.2%}"


def _format_sources(sources: dict) -> str:
    if not sources:
        return "全部来自本地缓存"
    return "、".join(f"{name} ×{count}" for name, count in sources.items())


def _monthly_markdown(table: pd.DataFrame) -> str:
    if table.empty:
        return "（无数据）"
    header = "| 年份 | " + " | ".join(f"{m}月" for m in range(1, 13)) + " | 全年 |"
    sep = "|" + "---|" * 14
    lines = [header, sep]
    for year, row in table.iterrows():
        cells = [_fmt_pct(row.get(m)) if not pd.isna(row.get(m)) else "-" for m in range(1, 13)]
        lines.append(f"| {year} | " + " | ".join(cells) + f" | {_fmt_pct(row.get('YTD'))} |")
    return "\n".join(lines)


def generate_outputs(
    cfg: dict,
    result,
    universe: pd.DataFrame,
    data_report,
    run_dir: Path,
) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    accounts_dir = run_dir / "accounts"
    accounts_dir.mkdir(exist_ok=True)

    # ---- 指标 ----
    for account in result.accounts:
        account.metrics = compute_metrics(account.equity, account.trades, cfg)

    master_trades = (
        pd.concat(
            [a.trades.assign(account=a.name) for a in result.accounts if not a.trades.empty],
            ignore_index=True,
        )
        if any(not a.trades.empty for a in result.accounts)
        else pd.DataFrame()
    )
    master_metrics = compute_metrics(result.equity, master_trades, cfg)
    result.metrics = master_metrics

    summary = format_metrics_table({a.name: a.metrics for a in result.accounts}, master_metrics)

    rejected_frames = [a.rejected.assign(account=a.name) for a in result.accounts if not a.rejected.empty]
    all_rejected = pd.concat(rejected_frames, ignore_index=True) if rejected_frames else pd.DataFrame()
    order_stats = _order_stats_table(result.accounts)

    # ---- CSV ----
    result.equity.to_csv(run_dir / "master_equity.csv", index=False, encoding="utf-8-sig")
    if not master_trades.empty:
        master_trades.to_csv(run_dir / "master_trades.csv", index=False, encoding="utf-8-sig")
    if not all_rejected.empty:
        all_rejected.to_csv(run_dir / "rejected_orders.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(run_dir / "summary.csv", index=False, encoding="utf-8-sig")
    monthly = monthly_returns(result.equity)
    monthly.to_csv(run_dir / "monthly_returns.csv", encoding="utf-8-sig")

    for account in result.accounts:
        base = accounts_dir / account.name
        base.mkdir(exist_ok=True)
        account.equity.to_csv(base / "equity.csv", index=False, encoding="utf-8-sig")
        account.positions.to_csv(base / "positions.csv", index=False, encoding="utf-8-sig")
        account.trades.to_csv(base / "trades.csv", index=False, encoding="utf-8-sig")
        account.orders.to_csv(base / "orders.csv", index=False, encoding="utf-8-sig")
        account.rejected.to_csv(base / "rejected_orders.csv", index=False, encoding="utf-8-sig")
        (base / "metrics.json").write_text(
            json.dumps(account.metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (base / "order_stats.json").write_text(
            json.dumps(account.order_stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        final = account.final_positions()
        final.to_csv(base / "final_positions.csv", index=False, encoding="utf-8-sig")

    with (run_dir / "config_used.yaml").open("w", encoding="utf-8") as fh:
        yaml.safe_dump(_as_serializable(cfg), fh, allow_unicode=True, sort_keys=False)

    # ---- 图 ----
    png_path = run_dir / "equity_curve.png"
    plot_equity(result.equity, result.accounts, png_path)

    # ---- 报告 ----
    report_path = run_dir / "report.md"
    report_path.write_text(
        _render_report(
            cfg, result, universe, data_report, summary, monthly, master_trades, order_stats
        ),
        encoding="utf-8",
    )

    return {
        "run_dir": str(run_dir),
        "report": str(report_path),
        "plot": str(png_path),
        "summary_csv": str(run_dir / "summary.csv"),
        "monthly_csv": str(run_dir / "monthly_returns.csv"),
        "rejected_csv": str(run_dir / "rejected_orders.csv"),
        "master_metrics": master_metrics,
        "account_metrics": {a.name: a.metrics for a in result.accounts},
        "account_order_stats": {a.name: a.order_stats for a in result.accounts},
        "order_stats": order_stats,
        "monthly": monthly,
        "summary": summary,
    }


def _order_stats_table(accounts) -> pd.DataFrame:
    rows = []
    for account in accounts:
        s = account.order_stats or {}
        rows.append(
            {
                "account": account.name,
                "orders_submitted": int(s.get("orders_submitted", 0)),
                "orders_rejected": int(s.get("orders_rejected", 0)),
                "reject_rate": float(s.get("reject_rate", 0.0)),
                "reject_events": int(s.get("reject_events", 0)),
                "limit_up": int(s.get("limit_up", 0)),
                "limit_down": int(s.get("limit_down", 0)),
                "suspended": int(s.get("suspended", 0)),
                "no_volume": int(s.get("no_volume", 0)),
                "max_wait_cancel": int(s.get("max_wait_cancel", 0)),
                "partial_events": int(s.get("partial_events", 0)),
            }
        )
    if not rows:
        return pd.DataFrame()
    total = {"account": "总账户"}
    for key in rows[0]:
        if key == "account":
            continue
        total[key] = sum(r[key] for r in rows)
    submitted = total["orders_submitted"]
    total["reject_rate"] = total["orders_rejected"] / submitted if submitted else 0.0
    rows.append(total)
    return pd.DataFrame(rows)


def _render_report(
    cfg, result, universe, data_report, summary, monthly, master_trades, order_stats
) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    project = cfg.get("project", {}).get("name", "quant")
    metrics = result.metrics
    cons_cfg = cfg.get("constraints") or {}
    cons_on = bool(cons_cfg.get("enabled"))

    data_lines = [
        f"- 标的池口径：**{data_report.universe_mode}**"
        + (
            "（按历史调样时间线逐日切片，已消除生存者偏差）"
            if data_report.universe_mode == "historical"
            else "（当前成分股回填历史，存在生存者偏差）"
        ),
        f"- 成分股来源：{data_report.universe_source}（共 {data_report.universe_count} 只；"
        f"{'缓存' if data_report.universe_from_cache else '当日拉取'}）",
        f"- 实际载入：{data_report.symbols_loaded}/{data_report.symbols_requested} 只",
        f"- 行情来源使用次数：{_format_sources(data_report.sources)}",
        f"- 数据覆盖：{data_report.coverage}",
        f"- 网络新抓取标的数：{data_report.symbols_fetched}；直接使用缓存：{data_report.symbols_from_cache}",
    ]
    if data_report.timeline_span:
        data_lines.append(
            f"- 历史成分时间线：{data_report.timeline_span}，"
            f"{len(data_report.timeline_changes)} 次调样"
        )
        if data_report.timeline_changes:
            data_lines.append("  - 调样记录：" + "；".join(data_report.timeline_changes))
    if data_report.warnings:
        data_lines.append("- 数据提示：")
        data_lines.extend(f"  - {item}" for item in data_report.warnings[:8])
    if data_report.failures:
        data_lines.append(f"- 数据失败：{len(data_report.failures)} 只（" + "；".join(data_report.failures[:5]) + "）")

    warnings = []
    if data_report.universe_mode == "historical":
        warnings.append(
            "成分股按 BaoStock 历史快照逐日切片，区间内每次调样均按生效交易日切换；"
            "交易所公告/指数公司官网的历史调样文件未接入，时间线以第三方数据为准。"
        )
    else:
        warnings.append("上证 50 成分为当前快照回填历史，存在生存者偏差；成分股调整未做历史时点还原。")
    if cons_on:
        warnings.append(
            "已模拟涨跌停（主板 10%/科创创业板 20%/主板 ST 5%）、停牌与零成交禁成交、"
            "成交量参与率上限；涨跌停价按 0.01 元四舍五入，未模拟集合竞价排队与盘中开板。"
        )
    else:
        warnings.append("未启用成交约束：涨跌停/停牌/成交量上限未模拟，部分成交在真实市场中不可能发生。")
    warnings.extend(
        [
            "行情为前复权价格，未单独建模分红现金流；长期分红收益会低估。",
            "信号在收盘后产生，订单于下一交易日开盘以市价成交，并计入配置的滑点与佣金/印花税。",
            "每个子账户使用独立 broker 独立记账，总净值为子账户净值之和加现金池，符合“比例切分”定义。",
            "A 股 T+1 未单独模拟（同一标的当日买入后不会在当日再卖出，实际行为与 T+1 基本一致）。",
        ]
    )
    if data_report.failures:
        warnings.append("部分标的数据缺失被剔除，收益结果的可比性会受影响，详见数据段落。")
    warnings.append("ST 状态历史区间无法完整还原，本次按“显式配置 + 名称含 ST”近似判定，实际影响需单独核对。")

    position_rows = []
    for account in result.accounts:
        final = account.final_positions()
        if final.empty:
            position_rows.append(f"| {account.name} | - | 0 | 0.00 | - |")
        else:
            for row in final.itertuples(index=False):
                position_rows.append(
                    f"| {account.name} | {row.symbol} | {row.size} | {row.value:,.2f} | {row.weight:.2%} |"
                )

    trade_rows = []
    if not master_trades.empty:
        recent = master_trades.sort_values("exit_date").tail(15)
        for row in recent.itertuples(index=False):
            trade_rows.append(
                f"| {row.account} | {row.symbol} | {row.entry_date} | {row.exit_date} | "
                f"{row.size} | {row.entry_price:.2f} | {row.pnl_net:,.2f} | {row.bars} |"
            )
    else:
        trade_rows.append("| - | - | - | - | - | - | - | - |")

    if order_stats is not None and not order_stats.empty:
        reject_rows = [
            f"| {r.account} | {r.orders_submitted} | {r.orders_rejected} | {r.reject_rate:.2%} | "
            f"{r.reject_events} | {r.limit_up} | {r.limit_down} | {r.suspended} | {r.no_volume} | "
            f"{r.max_wait_cancel} | {r.partial_events} |"
            for r in order_stats.itertuples(index=False)
        ]
        total = order_stats.iloc[-1]
        submitted = max(1, int(total["orders_submitted"]))
        limit_events = int(total["limit_up"]) + int(total["limit_down"])
        halt_events = int(total["suspended"]) + int(total["no_volume"])
        mixed = (
            f"总账户被拒订单 {int(total['orders_rejected'])}/{int(total['orders_submitted'])}"
            f"（{total['reject_rate']:.2%}），其中涨停买 {int(total['limit_up'])} 次、"
            f"跌停卖 {int(total['limit_down'])} 次、停牌/零成交 {halt_events} 次、"
            f"超时撤单 {int(total['max_wait_cancel'])} 次。"
            f"按提交订单口径：涨跌停被拒占比 {limit_events / submitted:.2%}，"
            f"停牌/无成交被拒占比 {halt_events / submitted:.2%}。"
        )
    else:
        reject_rows = ["| - | 0 | 0 | 0.00% | 0 | 0 | 0 | 0 | 0 | 0 | 0 |"]
        mixed = "无订单记录。"

    lines = [
        f"# 量化回测报告 — {project}",
        "",
        f"生成时间：{now}  ",
        f"运行模式：{cfg['engine'].get('run_mode', 'backtest')}",
        "",
        "## 一、配置与数据",
        "",
        describe_config(cfg),
        "",
        "### 数据状态",
        "",
        "\n".join(data_lines),
        "",
        "## 二、总账户绩效",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 初始净值 | {metrics.get('start_value', 0):,.2f} |",
        f"| 期末净值 | {metrics.get('end_value', 0):,.2f} |",
        f"| 总收益 | {_fmt_pct(metrics.get('total_return'))} |",
        f"| 年化收益 | {_fmt_pct(metrics.get('annual_return'))} |",
        f"| 年化波动 | {_fmt_pct(metrics.get('annual_volatility'))} |",
        f"| 夏普比率 | {metrics.get('sharpe')} |",
        f"| 最大回撤 | {_fmt_pct(metrics.get('max_drawdown'))} |",
        f"| Calmar | {metrics.get('calmar')} |",
        f"| 交易次数 | {metrics.get('trades')} |",
        f"| 胜率 | {_fmt_pct(metrics.get('win_rate'))} |",
        f"| 盈亏比 | {metrics.get('profit_factor')} |",
        f"| 总佣金 | {metrics.get('total_commission', 0):,.2f} |",
        "",
        "## 三、子账户绩效对比",
        "",
        summary.to_markdown(index=False),
        "",
        "## 四、成交约束与拒单统计",
        "",
        mixed,
        "",
        "| 子账户 | 提交订单 | 被拒订单 | 拒单率 | 拒单事件 | 涨停买入 | 跌停卖出 | 停牌 | 零成交 | 超时撤单 | 部分成交事件 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *reject_rows,
        "",
        "拒单明细见 `rejected_orders.csv`（每行一次拒绝事件，含开盘价、前收盘、涨跌停价与当日成交量）。"
        "被拒订单不会消失：涨停买不到/跌停卖不掉/停牌无法成交时订单保持挂单，"
        f"在后续交易日继续尝试，连续 {int(cons_cfg.get('max_pending_bars', 10))} 个交易日后撤销。",
        "",
        "## 五、总账户月度收益",
        "",
        _monthly_markdown(monthly),
        "",
        "## 六、期末持仓（按子账户）",
        "",
        "| 子账户 | 代码 | 股数 | 市值 | 占子账户净值 |",
        "|---|---|---|---|---|",
        *(position_rows if position_rows else ["| - | - | - | - | - |"]),
        "",
        "## 七、最近平仓交易（总账户，最多15笔）",
        "",
        "| 子账户 | 代码 | 开仓日 | 平仓日 | 股数 | 开仓价 | 净盈亏 | 持有K线 |",
        "|---|---|---|---|---|---|---|---|",
        *trade_rows,
        "",
        "## 八、产物清单",
        "",
        "- `equity_curve.png`：总账户与子账户净值曲线、总账户回撤",
        "- `master_equity.csv`：总账户逐日净值/现金/仓位市值",
        "- `master_trades.csv`：总账户已平仓交易明细",
        "- `rejected_orders.csv`：全部被拒/部分成交订单明细",
        "- `summary.csv`：总账户与子账户指标汇总",
        "- `monthly_returns.csv`：总账户月度收益表",
        "- `accounts/<子账户>/`：equity.csv、positions.csv、trades.csv、orders.csv、"
        "rejected_orders.csv、final_positions.csv、metrics.json、order_stats.json",
        "- `config_used.yaml`：本次运行完整配置快照",
        "",
        "## 九、说明与局限",
        "",
        *[f"- {item}" for item in warnings],
        "",
    ]
    return "\n".join(lines)
