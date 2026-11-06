"""命令行入口。

用法：
  python main.py backtest --config config/backtest.yaml
  python main.py data     --config config/backtest.yaml
  python main.py compare  --config config/backtest.yaml   # 改造前/后 2×2 对照
  python main.py list-strategies
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from analysis.report import generate_outputs
from core.config import ConfigError, describe_config, load_config
from data.loader import build_panel
from engine.runner import run_portfolio
from strategy.registry import get_strategy, list_strategies


def _check_strategies(cfg: dict) -> None:
    for sub in cfg["accounts"]["subaccounts"]:
        if sub.get("enabled", True):
            get_strategy(sub["strategy"])  # 未注册会抛 KeyError


def cmd_data(args) -> int:
    cfg = load_config(args.config)
    _check_strategies(cfg)
    panel, universe, report, timeline = build_panel(cfg)
    print(f"成分股：{report.universe_count} 只（{report.universe_mode} 口径），来源 {report.universe_source}")
    if timeline is not None:
        print(f"时间线：{report.timeline_span}，{len(timeline.changes)} 次调样，并集 {len(timeline.union())} 只")
        for item in timeline.changes:
            print(f"  {item['date']} 调入 {item['in']} 调出 {item['out']}")
    print(
        f"行情：{report.symbols_loaded}/{report.symbols_requested} 只可用，"
        f"覆盖 {report.coverage}，来源 {report.sources}"
    )
    print(f"抓取 {report.symbols_fetched} 只，缓存命中 {report.symbols_from_cache} 只")
    for warning in report.warnings:
        print(f"[提示] {warning}")
    for failure in report.failures:
        print(f"[失败] {failure}")
    cache_dir = Path(cfg["data"]["cache_dir"])
    files = list(cache_dir.glob("*.parquet"))
    print(f"缓存目录：{cache_dir}（{len(files)} 个 parquet）")
    return 0


def _data_names(universe: pd.DataFrame) -> dict[str, str]:
    if "name" not in universe.columns:
        return {}
    return dict(zip(universe["code"].astype(str).str.zfill(6), universe["name"].fillna("")))


def cmd_backtest(args) -> int:
    started = dt.datetime.now()
    cfg = load_config(args.config)
    _check_strategies(cfg)
    print("=" * 78)
    print(describe_config(cfg))
    print("=" * 78)

    panel, universe, data_report, timeline = build_panel(cfg)
    print(
        f"[数据] {data_report.symbols_loaded}/{data_report.symbols_requested} 只可用，"
        f"覆盖 {data_report.coverage}，来源 {data_report.sources or 'cache'}"
    )
    for warning in data_report.warnings:
        print(f"[数据提示] {warning}")
    for failure in data_report.failures:
        print(f"[数据失败] {failure}")

    result = run_portfolio(cfg, panel, timeline, names=_data_names(universe))
    run_name = (
        f"{cfg['project'].get('name', 'backtest')}_"
        f"{cfg['engine'].get('run_mode', 'backtest')}_"
        f"{started.strftime('%Y%m%d_%H%M%S')}"
    )
    run_dir = Path(cfg["paths"]["output_dir"]) / run_name
    outputs = generate_outputs(cfg, result, universe, data_report, run_dir)

    pd.set_option("display.width", 220)
    print("\n[汇总指标]")
    print(outputs["summary"].to_string(index=False))
    print("\n[月度收益]")
    print(outputs["monthly"].round(4).to_string())
    print("\n[产物]")
    for key in ("run_dir", "report", "plot", "summary_csv", "monthly_csv"):
        print(f"  {key:12s}: {outputs[key]}")
    elapsed = (dt.datetime.now() - started).total_seconds()
    print(f"\n[完成] 用时 {elapsed:.1f}s，子账户 {len(result.accounts)} 个")
    return 0


def cmd_compare(args) -> int:
    """2×2 因子对照：成分口径（current/historical）× 成交约束（关/开）。"""
    started = dt.datetime.now()
    base = load_config(args.config)
    _check_strategies(base)
    out_root = Path(base["paths"]["output_dir"]) / f"compare_{started.strftime('%Y%m%d_%H%M%S')}"
    out_root.mkdir(parents=True, exist_ok=True)

    panels = {}
    for mode in ("current", "historical"):
        cfg = copy.deepcopy(base)
        cfg["universe"]["mode"] = mode
        print(f"\n[数据] 构建 {mode} 口径标的池 ...")
        panel, universe, report, timeline = build_panel(cfg)
        panels[mode] = (panel, universe, report, timeline)
        print(
            f"[数据] {mode}: {report.symbols_loaded}/{report.symbols_requested} 只可用，"
            f"覆盖 {report.coverage}，时间线 {report.timeline_span or '-'}"
        )

    combos = [
        ("A_current_unconstrained", "current", False, "改造前：当前成分 + 无成交约束"),
        ("B_historical_unconstrained", "historical", False, "仅换历史成分口径"),
        ("C_current_constrained", "current", True, "仅加成交约束"),
        ("D_historical_constrained", "historical", True, "改造后：历史成分 + 成交约束"),
    ]
    rows = []
    summary_tables = {}
    for tag, mode, constraints_on, label in combos:
        cfg = copy.deepcopy(base)
        cfg["universe"]["mode"] = mode
        cfg["constraints"]["enabled"] = constraints_on
        cfg["project"]["name"] = f"cmp_{tag}"
        panel, universe, report, timeline = panels[mode]
        print(f"\n[回测] {tag} — {label}")
        result = run_portfolio(cfg, panel, timeline, names=_data_names(universe))
        outputs = generate_outputs(cfg, result, universe, report, out_root / tag)
        summary_tables[tag] = outputs["summary"]
        m = outputs["master_metrics"]
        stats = outputs["order_stats"].iloc[-1] if not outputs["order_stats"].empty else {}
        rows.append(
            {
                "组合": tag,
                "说明": label,
                "成分口径": mode,
                "成交约束": "开" if constraints_on else "关",
                "总收益": m.get("total_return"),
                "年化": m.get("annual_return"),
                "最大回撤": m.get("max_drawdown"),
                "夏普": m.get("sharpe"),
                "交易次数": m.get("trades"),
                "提交订单": int(stats.get("orders_submitted", 0)),
                "被拒订单": int(stats.get("orders_rejected", 0)),
                "拒单率": float(stats.get("reject_rate", 0.0)),
            }
        )
        for account in result.accounts:
            am = outputs["account_metrics"][account.name]
            ast = outputs["account_order_stats"][account.name]
            rows.append(
                {
                    "组合": tag,
                    "说明": label,
                    "成分口径": mode,
                    "成交约束": "开" if constraints_on else "关",
                    "总收益": am.get("total_return"),
                    "年化": am.get("annual_return"),
                    "最大回撤": am.get("max_drawdown"),
                    "夏普": am.get("sharpe"),
                    "交易次数": am.get("trades"),
                    "提交订单": int(ast.get("orders_submitted", 0)),
                    "被拒订单": int(ast.get("orders_rejected", 0)),
                    "拒单率": float(ast.get("reject_rate", 0.0)),
                    "账户": account.name,
                }
            )
        print(
            f"  总收益 {m.get('total_return', 0):.2%} / 年化 {m.get('annual_return', 0):.2%} / "
            f"MDD {m.get('max_drawdown', 0):.2%} / Sharpe {m.get('sharpe', 0):.3f} / "
            f"交易 {m.get('trades', 0)} 次 / 拒单 {int(stats.get('orders_rejected', 0))} "
            f"({float(stats.get('reject_rate', 0)):.2%})"
        )

    table = pd.DataFrame(rows)
    table.to_csv(out_root / "comparison.csv", index=False, encoding="utf-8-sig")
    _write_comparison_report(out_root, table, combos, base)
    print(f"\n[完成] 对照产物：{out_root}")
    print(f"  对照表：{out_root / 'comparison.csv'}")
    print(f"  对照报告：{out_root / 'comparison.md'}")
    return 0


def _metric_of(table: pd.DataFrame, tag: str, account: str | None, column: str):
    mask = (table["组合"] == tag) & (table["账户"] == account if account else table["账户"].isna())
    if not mask.any():
        return None
    return table.loc[mask, column].iloc[0]


def _delta(table: pd.DataFrame, lhs: str, rhs: str, column: str = "总收益"):
    a = _metric_of(table, lhs, None, column)
    b = _metric_of(table, rhs, None, column)
    if a is None or b is None:
        return None
    return float(a) - float(b)


def _pp(value) -> str:
    return "-" if value is None else f"{value * 100:+.2f}pp"


def _write_comparison_report(out_root: Path, table: pd.DataFrame, combos, cfg: dict) -> None:
    master = table[table["账户"].isna()] if "账户" in table else table
    lines = [
        "# 改造前 vs 改造后 — 2×2 因子对照",
        "",
        f"生成时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}  ",
        f"区间：{cfg['data']['start_date']} ~ {cfg['data']['end_date']}，"
        f"初始资金 {cfg['broker']['cash']:,.0f} 元，配置除下表两项外完全一致。",
        "",
        "| 组合 | 成分口径 | 成交约束 | 说明 |",
        "|---|---|---|---|",
        *[f"| {t} | {m} | {'开' if c else '关'} | {label} |" for t, m, c, label in combos],
        "",
        "## 一、总账户对照",
        "",
        "| 组合 | 总收益 | 年化 | 最大回撤 | 夏普 | 交易次数 | 提交订单 | 被拒订单 | 拒单率 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in master.iterrows():
        lines.append(
            f"| {r['组合']} | {r['总收益']:.2%} | {r['年化']:.2%} | {r['最大回撤']:.2%} | {r['夏普']:.4f} | "
            f"{r['交易次数']} | {r['提交订单']} | {r['被拒订单']} | {r['拒单率']:.2%} |"
        )

    lines += [
        "",
        "## 二、变化归因（百分点）",
        "",
        f"- 成分股口径贡献（无成交约束，B−A）：{_pp(_delta(table, 'B_historical_unconstrained', 'A_current_unconstrained'))}",
        f"- 成分股口径贡献（有成交约束，D−C）：{_pp(_delta(table, 'D_historical_constrained', 'C_current_constrained'))}",
        f"- 成交约束贡献（当前成分，C−A）：{_pp(_delta(table, 'C_current_constrained', 'A_current_unconstrained'))}",
        f"- 成交约束贡献（历史成分，D−B）：{_pp(_delta(table, 'D_historical_constrained', 'B_historical_unconstrained'))}",
        f"- 交互项（(D−B)−(C−A)）：{_pp((_delta(table, 'D_historical_constrained', 'B_historical_unconstrained') or 0) - (_delta(table, 'C_current_constrained', 'A_current_unconstrained') or 0))}",
        "",
        "读法：若两种口径下的成交约束贡献方向相同，说明约束影响主要来自执行层面；"
        "若历史口径下的成分股贡献显著为正/为负，说明原回测的生存者偏差被高估/低估。",
        "",
        "## 三、子账户对照（A → D）",
        "",
        "| 子账户 | A 总收益 | D 总收益 | 变化 | A 交易 | D 交易 | A 拒单率 | D 拒单率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    accounts = sorted(table["账户"].dropna().unique()) if "账户" in table else []
    for name in accounts:
        a_ret = _metric_of(table, "A_current_unconstrained", name, "总收益")
        d_ret = _metric_of(table, "D_historical_constrained", name, "总收益")
        a_tr = _metric_of(table, "A_current_unconstrained", name, "交易次数")
        d_tr = _metric_of(table, "D_historical_constrained", name, "交易次数")
        a_rr = _metric_of(table, "A_current_unconstrained", name, "拒单率")
        d_rr = _metric_of(table, "D_historical_constrained", name, "拒单率")
        delta = (float(d_ret) - float(a_ret)) if a_ret is not None and d_ret is not None else None
        lines.append(
            f"| {name} | {a_ret:.2%} | {d_ret:.2%} | {_pp(delta)} | {int(a_tr)} | {int(d_tr)} | "
            f"{float(a_rr):.2%} | {float(d_rr):.2%} |"
        )
    lines += [
        "",
        "## 四、口径与验证范围",
        "",
        "- 历史成分：BaoStock `query_sz50_stocks(date)` 逐日快照，按检测到的调样生效交易日切换；"
        "采样步长 5 个自然日，检测到变更后逐日回查定位首个变化交易日。",
        "- 涨跌停口径：主板 10%、科创板 20%、创业板 20%、北交所 30%、主板 ST 5%；"
        "涨跌停价 = 前收盘 × (1±比例)，按 0.01 元四舍五入；开盘价触及涨停不可买、跌停不可卖。",
        "- 停牌：执行日无 K 线或成交量为 0 时不得成交，订单保持挂单等待下一有行情交易日。",
        "- 成交量参与率：单笔不超过当日成交量的配置比例，超出部分次日续挂。",
        "- 未模拟：集合竞价排队、盘中开板后的部分成交、ST 状态历史区间、分红现金流。",
        "",
        "子组合完整产物见各子目录 `A_*/`、`B_*/`、`C_*/`、`D_*/`。",
        "",
    ]
    (out_root / "comparison.md").write_text("\n".join(lines), encoding="utf-8")


def cmd_list(_args) -> int:
    rows = list_strategies()
    for row in rows:
        print(f"{row['name']:12s} {row['display_name']:8s} {row['class']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="上证50 多策略多子账户量化回测系统")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bt = sub.add_parser("backtest", help="运行回测")
    p_bt.add_argument("--config", default="config/backtest.yaml", help="配置文件路径")
    p_bt.set_defaults(func=cmd_backtest)

    p_data = sub.add_parser("data", help="仅拉取/更新数据并检查缓存")
    p_data.add_argument("--config", default="config/backtest.yaml", help="配置文件路径")
    p_data.set_defaults(func=cmd_data)

    p_cmp = sub.add_parser("compare", help="改造前/后 2×2 因子对照（成分口径 × 成交约束）")
    p_cmp.add_argument("--config", default="config/backtest.yaml", help="配置文件路径")
    p_cmp.set_defaults(func=cmd_compare)

    p_list = sub.add_parser("list-strategies", help="列出已注册策略")
    p_list.set_defaults(func=cmd_list)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"[策略错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 数据源等运行时错误，给出明确提示
        print(f"[运行失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
