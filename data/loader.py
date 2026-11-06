"""数据层：上证 50 成分股 + 前复权日线，带 parquet 缓存、增量更新和离线兜底。"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from data.source import DataSourceError, SourceStats, available_sources, fetch_with_fallback
from data.timeline import MemberTimeline, build_timeline

UNIVERSE_COLUMNS = ["fetched_at", "code", "name"]


@dataclass
class DataReport:
    universe_source: str = ""
    universe_count: int = 0
    universe_from_cache: bool = False
    universe_mode: str = "current"
    timeline_source: str = ""
    timeline_span: str = ""
    timeline_changes: list[str] = field(default_factory=list)
    symbols_requested: int = 0
    symbols_loaded: int = 0
    symbols_fetched: int = 0
    symbols_from_cache: int = 0
    sources: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coverage: str = ""

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)


# ----------------------------------------------------------------------
# 成分股
# ----------------------------------------------------------------------
def _fetch_universe_csindex(index_code: str) -> pd.DataFrame:
    import akshare as ak

    raw = ak.index_stock_cons_csindex(symbol=index_code)
    if raw is None or raw.empty:
        raise DataSourceError("中证指数成分股接口返回空数据")
    code_col = "成分券代码" if "成分券代码" in raw.columns else raw.columns[3]
    name_col = "成分券名称" if "成分券名称" in raw.columns else raw.columns[4]
    df = raw[[code_col, name_col]].copy()
    df.columns = ["code", "name"]
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df.drop_duplicates(subset=["code"]).sort_values("code").reset_index(drop=True)


def _fetch_universe_sina(index_code: str) -> pd.DataFrame:
    import akshare as ak

    raw = ak.index_stock_cons(symbol=f"sh{index_code}")
    if raw is None or raw.empty:
        raise DataSourceError("新浪成分股接口返回空数据")
    code_col = "品种代码" if "品种代码" in raw.columns else raw.columns[0]
    name_col = "品种名称" if "品种名称" in raw.columns else raw.columns[1]
    df = raw[[code_col, name_col]].copy()
    df.columns = ["code", "name"]
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df.drop_duplicates(subset=["code"]).sort_values("code").reset_index(drop=True)


def _read_universe_file(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    return df


def load_universe(cfg: dict, report: DataReport) -> pd.DataFrame:
    data_cfg = cfg["data"]
    universe_dir = Path(data_cfg["universe_dir"])
    universe_dir.mkdir(parents=True, exist_ok=True)
    cache_file = universe_dir / f"{data_cfg.get('universe_name', 'index')}_{data_cfg['index_code']}.csv"
    ttl = int(data_cfg.get("universe_ttl_days", 30))
    refresh = data_cfg.get("refresh", "incremental")

    df = None
    if cache_file.exists() and refresh != "full":
        cached = _read_universe_file(cache_file)
        fetched_at = pd.to_datetime(cached["fetched_at"].iloc[0]).date()
        age = (dt.date.today() - fetched_at).days
        if age <= ttl:
            df = cached
            report.universe_from_cache = True
            report.universe_source = "cache"
        elif refresh == "cache_only":
            df = cached
            report.universe_from_cache = True
            report.universe_source = "cache(stale)"
            report.warn(f"成分股缓存已过期 {age} 天，cache_only 模式下继续使用")

    if df is None and refresh != "cache_only":
        fetchers = []
        if data_cfg.get("universe_source", "csindex") == "csindex":
            fetchers = [("csindex", _fetch_universe_csindex), ("sina", _fetch_universe_sina)]
        else:
            fetchers = [("sina", _fetch_universe_sina), ("csindex", _fetch_universe_csindex)]
        for name, fn in fetchers:
            try:
                fetched = fn(data_cfg["index_code"])
                fetched.insert(0, "fetched_at", dt.date.today().isoformat())
                fetched.to_csv(cache_file, index=False, encoding="utf-8-sig")
                df = fetched
                report.universe_source = name
                break
            except Exception as exc:
                report.warn(f"成分股获取失败（{name}）：{type(exc).__name__}: {exc}")
                time.sleep(0.5)

    if df is None:
        fallback = data_cfg.get("fallback_universe")
        if fallback and Path(fallback).exists():
            df = _read_universe_file(Path(fallback))
            report.universe_source = "snapshot"
            report.warn(f"网络不可用，使用内置成分股快照：{fallback}")
        elif cache_file.exists():
            df = _read_universe_file(cache_file)
            report.universe_source = "cache(stale)"
            report.warn("网络不可用，使用过期成分股缓存")
        else:
            raise DataSourceError("无法获取上证50成分股，且本地无缓存/快照")

    df["code"] = df["code"].str.zfill(6)
    df = df.drop_duplicates(subset=["code"])
    excluded = {str(x).zfill(6) for x in data_cfg.get("exclude_symbols") or []}
    if excluded:
        df = df[~df["code"].isin(excluded)]
    max_symbols = int(data_cfg.get("max_symbols") or 0)
    if max_symbols > 0:
        df = df.head(max_symbols)
    report.universe_count = len(df)
    return df.reset_index(drop=True)


# ----------------------------------------------------------------------
# 日线
# ----------------------------------------------------------------------
def _cache_path(cfg: dict, symbol: str) -> Path:
    cache_dir = Path(cfg["data"]["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{symbol}.parquet"


def _read_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        df.index = pd.to_datetime(df.index)
        return df.sort_index()
    except Exception:
        return None


def load_daily(
    cfg: dict,
    symbol: str,
    fetch_start: str,
    fetch_end: str,
    sources: list,
    stats: SourceStats,
    report: DataReport,
) -> pd.DataFrame | None:
    data_cfg = cfg["data"]
    refresh = data_cfg.get("refresh", "incremental")
    path = _cache_path(cfg, symbol)
    cached = _read_cache(path)

    if refresh == "cache_only":
        if cached is None:
            report.failures.append(f"{symbol}: cache_only 模式且无缓存")
            return None
        report.symbols_from_cache += 1
        return cached

    if refresh == "full":
        start, end = fetch_start, fetch_end
    elif cached is not None and not cached.empty:
        last = cached.index.max().normalize()
        earliest = cached.index.min().normalize()
        if earliest > pd.Timestamp(fetch_start).normalize() + pd.Timedelta(days=10):
            # 缓存起点明显晚于本次预热起点（超过 10 天），整体回补更早历史
            start, end = fetch_start, fetch_end
        elif last >= pd.Timestamp(fetch_end).normalize():
            report.symbols_from_cache += 1
            return cached
        else:
            # 重叠 20 个自然日重取，减少前复权因子变动造成的接缝
            start = (last - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
            end = fetch_end
    else:
        start, end = fetch_start, fetch_end
        cached = None

    fetched, source = fetch_with_fallback(sources, symbol, start, end, stats)
    if fetched is None or fetched.empty:
        if cached is not None and not cached.empty:
            report.symbols_from_cache += 1
            report.warn(f"{symbol}: 网络获取失败，使用本地缓存（最后日期 {cached.index.max().date()}）")
            return cached
        report.failures.append(f"{symbol}: 无网络数据且无缓存")
        return None

    if cached is not None and not cached.empty:
        # 重叠区间以新数据为准
        merged = pd.concat([cached[~cached.index.isin(fetched.index)], fetched]).sort_index()
    else:
        merged = fetched
    merged = merged[~merged.index.duplicated(keep="last")]
    merged = merged.loc[: pd.Timestamp(fetch_end)]
    merged.to_parquet(path)
    report.symbols_fetched += 1
    return merged


def _load_universe_for_mode(
    cfg: dict, report: DataReport, start: dt.date, end: dt.date
) -> tuple[pd.DataFrame, MemberTimeline | None]:
    """按 universe.mode 返回标的表与（historical 模式下的）成员时间线。"""
    mode = str((cfg.get("universe") or {}).get("mode", "current")).lower()
    report.universe_mode = mode
    data_cfg = cfg["data"]

    if mode == "current":
        return load_universe(cfg, report), None
    if mode != "historical":
        raise DataSourceError(f"未知 universe.mode: {mode}（应为 current 或 historical）")

    hcfg = (cfg.get("universe") or {}).get("historical") or {}
    timeline_file = Path(hcfg.get("timeline_file") or "data/cache/universe/sse50_timeline.csv")
    if not timeline_file.is_absolute():
        timeline_file = Path(cfg["_project_root"]) / timeline_file
    timeline = build_timeline(
        timeline_file,
        start,
        end,
        sample_step_days=int(hcfg.get("sample_step_days", 5)),
        refresh=str(hcfg.get("refresh", "auto")),
        max_age_days=int(hcfg.get("max_age_days", 7)),
        log=lambda msg: print(f"[成分] {msg}"),
    )
    report.universe_source = f"baostock_timeline({timeline.source})"
    report.timeline_source = timeline.source
    report.timeline_span = timeline.span()
    report.timeline_changes = [
        f"{item['date']} 调入{len(item['in'])}/调出{len(item['out'])}" for item in timeline.changes
    ]
    if len(timeline.segments) < 2:
        report.warn("历史成分时间线只有一个区间，请检查区间是否覆盖了调样日")
    universe = timeline.universe_frame()
    if data_cfg.get("max_symbols"):
        report.warn(
            f"historical 模式载入历史并集 {len(universe)} 只，忽略 data.max_symbols="
            f"{data_cfg.get('max_symbols')}（否则会截断历史成分）"
        )
    excluded = {str(x).zfill(6) for x in data_cfg.get("exclude_symbols") or []}
    if excluded:
        universe = universe[~universe["code"].isin(excluded)]
        report.warn(f"按 exclude_symbols 剔除 {len(excluded)} 只：{sorted(excluded)}")
    report.universe_count = len(universe)
    return universe.reset_index(drop=True), timeline


def build_panel(cfg: dict) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, DataReport, MemberTimeline | None]:
    """返回 (symbol -> OHLCV DataFrame, 标的表, 数据报告, 成员时间线)。

    DataFrame 含指标预热区间（start_date - warmup_calendar_days），
    回测/统计仍以 config 的 start_date 为起点。
    historical 模式下标的表为历史并集，时间线用于按日过滤可交易标的。
    """
    report = DataReport()
    data_cfg = cfg["data"]
    start = data_cfg["_start_date"]
    end = data_cfg["_end_date"]
    warmup = int(data_cfg.get("warmup_calendar_days", 260))
    fetch_start = (start - dt.timedelta(days=warmup)).isoformat()
    fetch_end = end.isoformat()

    universe, timeline = _load_universe_for_mode(cfg, report, start, end)
    historical = timeline is not None

    sources = available_sources(data_cfg.get("provider", "auto"), data_cfg.get("adjust", "qfq"))
    stats = SourceStats()
    interim = float(data_cfg.get("request_interval", 0.3))
    panel: dict[str, pd.DataFrame] = {}
    min_bars = int(data_cfg.get("min_bars", 200))

    report.symbols_requested = len(universe)
    for i, row in enumerate(universe.itertuples(index=False), start=1):
        symbol = row.code
        try:
            df = load_daily(cfg, symbol, fetch_start, fetch_end, sources, stats, report)
        except Exception as exc:
            report.failures.append(f"{symbol}: {type(exc).__name__}: {exc}")
            df = None
        if df is not None and not df.empty:
            first_date = df.index.min()
            if first_date > pd.Timestamp(start):
                if historical:
                    # 历史模式下保留：该标的只在上市之后可能成为成分股
                    report.warn(
                        f"{symbol}: 首个交易日 {first_date.date()} 晚于回测起点 {start}，"
                        "historical 模式保留（上市前不参与）"
                    )
                else:
                    report.warn(f"{symbol}: 首个交易日 {first_date.date()} 晚于回测起点 {start}，已剔除")
                    first_date = None
            if first_date is not None:
                bars_in_window = df.loc[pd.Timestamp(start) : pd.Timestamp(end)]
                if len(bars_in_window) >= min_bars:
                    panel[symbol] = df
                else:
                    report.warn(f"{symbol}: 区间内仅 {len(bars_in_window)} 根K线（阈值 {min_bars}），已剔除")
        if i < len(universe):
            time.sleep(interim)

    # historical 模式下按首个交易日排序：Backtrader 以 datas[0] 为时钟基准，
    # 晚上市标的不能排在首位。current 模式保持原标的顺序，便于与旧结果对照。
    if historical:
        panel = dict(sorted(panel.items(), key=lambda kv: kv[1].index.min()))

    report.sources = dict(stats.used)
    report.symbols_loaded = len(panel)
    if panel:
        first = min(df.index.min() for df in panel.values())
        last = max(df.index.max() for df in panel.values())
        report.coverage = f"{first.date()} ~ {last.date()}"
    if not panel:
        raise DataSourceError("没有可用行情数据，请检查网络或 data/cache 缓存")
    if len(panel) < report.symbols_requested:
        report.warn(f"可用标的 {len(panel)}/{report.symbols_requested}，部分标的因数据缺失被剔除")
    if stats.errors:
        report.warn(f"数据源报错 {len(stats.errors)} 次，已自动降级/使用缓存")
        report.warnings.extend(stats.errors[:5])

    universe["has_data"] = universe["code"].isin(panel.keys())
    if historical and timeline is not None:
        missing = [c for c in timeline.union() if c not in panel]
        if missing:
            report.warn(f"历史成分中 {len(missing)} 只无可用行情，仅在其缺失期间无法交易：{missing[:12]}")
    return panel, universe, report, timeline
