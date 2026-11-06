"""配置加载与校验。

- 支持 YAML 的 extends 继承（单层/多层均可），子配置覆盖父配置
- 所有相对路径均以项目根目录（配置文件所在目录的上一级）为基准
- 校验：日期区间、权重之和、子账户必填字段、资金比例
"""
from __future__ import annotations

import copy
import datetime as dt
import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """配置非法时抛出，携带可读的中文原因。"""


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 dict；list 直接覆盖（子账户列表整体替换）。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_raw(path: Path, seen: set[Path] | None = None) -> dict:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        raise ConfigError(f"配置 extends 出现循环引用: {path}")
    seen.add(path)
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射结构: {path}")

    parent = data.pop("extends", None)
    if parent:
        parent_path = (path.parent / parent).resolve()
        base = _load_raw(parent_path, seen)
        data = _deep_merge(base, data)
    return data


def _parse_date(value: Any, field: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(f"{field} 日期格式应为 YYYY-MM-DD，当前为 {value!r}") from exc
    raise ConfigError(f"{field} 必须是 YYYY-MM-DD 字符串")


def load_config(path: str | os.PathLike) -> dict:
    """加载并校验配置，返回带路径解析结果的 dict。"""
    config_path = Path(path)
    cfg = _load_raw(config_path)
    root = config_path.resolve().parent.parent
    cfg["_config_path"] = str(config_path.resolve())
    cfg["_project_root"] = str(root)

    paths = cfg.setdefault("paths", {})
    for key in ("data_dir", "output_dir"):
        if key not in paths:
            raise ConfigError(f"缺少 paths.{key}")
        p = Path(paths[key])
        paths[key] = str(p if p.is_absolute() else root / p)

    data_cfg = cfg.setdefault("data", {})
    for key in ("cache_dir", "universe_dir", "fallback_universe"):
        value = data_cfg.get(key)
        if value:
            p = Path(value)
            data_cfg[key] = str(p if p.is_absolute() else root / p)

    # 日期解析与区间校验
    data_cfg["_start_date"] = _parse_date(data_cfg.get("start_date"), "data.start_date")
    data_cfg["_end_date"] = _parse_date(data_cfg.get("end_date"), "data.end_date")
    if data_cfg["_start_date"] > data_cfg["_end_date"]:
        raise ConfigError("data.start_date 必须早于 data.end_date")

    engine = cfg.setdefault("engine", {})
    trade_start = engine.get("trade_start")
    if trade_start:
        engine["_trade_start"] = _parse_date(trade_start, "engine.trade_start")
    else:
        engine["_trade_start"] = data_cfg["_start_date"]

    universe = cfg.setdefault("universe", {})
    mode = str(universe.get("mode", "current")).lower()
    if mode not in ("current", "historical"):
        raise ConfigError(f"universe.mode 只能是 current 或 historical，当前 {mode!r}")
    universe["mode"] = mode

    constraints = cfg.setdefault("constraints", {})
    if "enabled" not in constraints:
        constraints["enabled"] = False
    if float(constraints.get("max_pending_bars", 10)) < 1:
        raise ConfigError("constraints.max_pending_bars 必须 >= 1")
    part = constraints.get("participation") or {}
    rate = float(part.get("max_rate", 0.1))
    if not 0 < rate <= 1:
        raise ConfigError(f"constraints.participation.max_rate 必须在 (0,1] 内，当前 {rate}")

    validate_accounts(cfg)
    return cfg


def validate_accounts(cfg: dict) -> None:
    accounts = cfg.get("accounts") or {}
    subs = accounts.get("subaccounts") or []
    if not subs:
        raise ConfigError("accounts.subaccounts 不能为空")

    cash_reserve = accounts.get("cash_reserve")
    if cash_reserve is None:
        raise ConfigError("缺少 accounts.cash_reserve（现金池比例）")
    if not 0 <= float(cash_reserve) < 1:
        raise ConfigError(f"accounts.cash_reserve 必须在 [0,1) 内，当前 {cash_reserve}")

    total = float(cash_reserve)
    names: set[str] = set()
    for idx, sub in enumerate(subs):
        for field in ("name", "strategy", "weight"):
            if field not in sub:
                raise ConfigError(f"accounts.subaccounts[{idx}] 缺少字段 {field}")
        if sub["name"] in names:
            raise ConfigError(f"子账户名称重复: {sub['name']}")
        names.add(sub["name"])
        weight = sub["weight"]
        if not 0 < float(weight) <= 1:
            raise ConfigError(f"子账户 {sub['name']} 的 weight 必须在 (0,1] 内，当前 {weight}")
        if "params" in sub and not isinstance(sub["params"], dict):
            raise ConfigError(f"子账户 {sub['name']} 的 params 必须是映射")
        total += float(weight)

    if abs(total - 1.0) > 1e-6:
        raise ConfigError(
            "资金比例不合法：cash_reserve + Σ(子账户 weight) 必须等于 1.0，"
            f"当前合计 {total:.6f}（cash_reserve={cash_reserve}）"
        )

    broker = cfg.setdefault("broker", {})
    if float(broker.get("cash", 0)) <= 0:
        raise ConfigError("broker.cash 必须为正数")


def describe_config(cfg: dict) -> str:
    """生成配置摘要，写入报告。"""
    data = cfg["data"]
    lines = [
        f"- 配置文件：`{cfg['_config_path']}`",
        f"- 回测区间：{data['start_date']} → {data['end_date']}（预热 {data.get('warmup_calendar_days')} 自然日）",
        f"- 总账户资金：{cfg['broker']['cash']:,.2f} 元",
        f"- 佣金 {cfg['broker']['commission']:.5%}（最低 {cfg['broker'].get('min_commission')} 元）"
        f"，印花税 {cfg['broker'].get('stamp_duty', 0):.4%}（卖出）"
        f"，滑点 {cfg['broker'].get('slippage_perc', 0):.4%}",
        f"- 现金池：{cfg['accounts']['cash_reserve']:.0%}",
        "- 子账户：",
    ]
    for sub in cfg["accounts"]["subaccounts"]:
        state = "启用" if sub.get("enabled", True) else "停用"
        lines.append(
            f"  - `{sub['name']}` → 策略 `{sub['strategy']}`，权重 {sub['weight']:.0%}（{state}）"
        )
    universe = cfg.get("universe") or {}
    mode = universe.get("mode", "current")
    lines.append(
        f"- 标的池口径：`{mode}`"
        + ("（按历史调样时间线逐日过滤，无生存者偏差）" if mode == "historical" else "（当前成分股回填历史）")
    )
    cons = cfg.get("constraints") or {}
    if cons.get("enabled"):
        limit_cfg = cons.get("limit") or {}
        lines.append(
            f"- 成交约束：涨跌停 主板 {limit_cfg.get('main', 0.10):.0%} / 科创 {limit_cfg.get('star', 0.20):.0%}"
            f" / 创业板 {limit_cfg.get('chinext', 0.20):.0%} / ST {limit_cfg.get('st', 0.05):.0%}，"
            f"停牌与零成交禁成交，成交量参与率上限 {float((cons.get('participation') or {}).get('max_rate', 0.1)):.0%}，"
            f"挂单最长 {int(cons.get('max_pending_bars', 10))} 个交易日"
        )
    else:
        lines.append("- 成交约束：未启用（不模拟涨跌停/停牌/成交量上限）")
    return "\n".join(lines)
