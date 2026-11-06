"""上证 50 历史成分股时间线（member timeline）。

数据来源
--------
BaoStock ``query_sz50_stocks(date=YYYY-MM-DD)``：给定任意历史日期，返回当日
上证 50 的 50 只成分股（代码 + 名称）。实测可回溯到 2015 年，覆盖本回测区间。

BaoStock 返回的行首字段是数据商自身的“更新日期”，并非指数调样生效日（厂商每周
刷新但成分不变时该字段也会变化），因此本模块不用它判定生效日，而是：

1. 每 ``sample_step_days`` 个自然日取一次快照；
2. 相邻两次快照成分不同时，在两次采样之间逐日回查，定位到**首个**成分改变的交易日；
3. 以该交易日为新区间起点，生成 ``[start_date, end_date]`` 区间表。

因此时间线在交易日粒度上是精确的（2022-01 ~ 2026-11 实测 10 次调样，全部落在
周一，与中证指数定期调样生效日一致）。若 BaoStock 不可用且无本地缓存，historical
模式会明确报错，不会用推测数据补全。
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from data.source import DataSourceError

TIMELINE_COLUMNS = ["start_date", "end_date", "code", "name", "built_at"]


def _weekdays(start: dt.date, end: dt.date, step_days: int) -> list[dt.date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=step_days)
    if out and out[-1] != end and end.weekday() < 5:
        out.append(end)
    return out


class _BaoStockSession:
    """懒登录的 BaoStock 会话，退出时统一 logout。"""

    def __init__(self):
        self._logged_in = False

    def __enter__(self):
        try:
            import baostock as bs
        except ImportError as exc:  # pragma: no cover
            raise DataSourceError("historical 模式需要 baostock，请先 pip install baostock") from exc
        self.bs = bs
        lg = bs.login()
        if lg.error_code != "0":
            raise DataSourceError(f"BaoStock 登录失败：{lg.error_code} {lg.error_msg}")
        self._logged_in = True
        return self

    def __exit__(self, *exc_info):
        if self._logged_in:
            self.bs.logout()
            self._logged_in = False

    def snapshot(self, date: dt.date) -> tuple[frozenset[str], dict[str, str]]:
        rs = self.bs.query_sz50_stocks(date=date.isoformat())
        if rs.error_code != "0":
            raise DataSourceError(f"BaoStock 成分查询失败（{date}）：{rs.error_msg}")
        codes: set[str] = set()
        names: dict[str, str] = {}
        while rs.next():
            row = rs.get_row_data()
            if len(row) < 3:
                continue
            code = row[1].split(".")[-1].zfill(6)
            codes.add(code)
            names[code] = row[2]
        if len(codes) != 50:
            raise DataSourceError(f"BaoStock 在 {date} 返回 {len(codes)} 只成分股（应为 50）")
        return frozenset(codes), names


@dataclass
class MemberTimeline:
    """按日期生效的成分股区间表。"""

    segments: list[tuple[dt.date, dt.date, frozenset[str]]] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)
    source: str = ""
    built_at: str = ""
    changes: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self._intervals: dict[str, list[tuple[dt.date, dt.date]]] = {}
        for start, end, codes in self.segments:
            for code in codes:
                self._intervals.setdefault(code, []).append((start, end))

    # ------------------------------------------------------------------
    def contains(self, code: str, date: dt.date) -> bool:
        for start, end in self._intervals.get(code, ()):
            if start <= date <= end:
                return True
        return False

    def members_on(self, date: dt.date) -> set[str]:
        for start, end, codes in self.segments:
            if start <= date <= end:
                return set(codes)
        return set()

    def union(self) -> list[str]:
        return sorted(self._intervals)

    def span(self) -> str:
        if not self.segments:
            return "-"
        return f"{self.segments[0][0]} ~ {self.segments[-1][1]}"

    def to_frame(self) -> pd.DataFrame:
        rows = []
        for start, end, codes in self.segments:
            for code in sorted(codes):
                rows.append(
                    {
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "code": code,
                        "name": self.names.get(code, ""),
                        "built_at": self.built_at,
                    }
                )
        return pd.DataFrame(rows, columns=TIMELINE_COLUMNS)

    def universe_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {"code": self.union(), "name": [self.names.get(c, "") for c in self.union()]}
        )


def _load_cached(path: Path, start: dt.date, end: dt.date) -> MemberTimeline | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype={"code": str})
    except Exception:
        return None
    if df.empty or not set(TIMELINE_COLUMNS).issubset(df.columns):
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    starts = pd.to_datetime(df["start_date"]).dt.date
    ends = pd.to_datetime(df["end_date"]).dt.date
    if starts.min() > start or ends.max() < end:
        return None
    groups = df.groupby(["start_date", "end_date"], sort=True)
    segments = []
    for (s, e), part in groups:
        if len(part) != 50:
            return None  # 区间不完整，视为不可用
        segments.append((dt.date.fromisoformat(str(s)), dt.date.fromisoformat(str(e)), frozenset(part["code"])))
    names = dict(zip(df["code"], df["name"].fillna("")))
    built_at = str(df["built_at"].iloc[0])
    changes = []
    for idx in range(1, len(segments)):
        prev_codes = segments[idx - 1][2]
        codes = segments[idx][2]
        changes.append(
            {
                "date": segments[idx][0],
                "in": sorted(codes - prev_codes),
                "out": sorted(prev_codes - codes),
            }
        )
    return MemberTimeline(segments=segments, names=names, source="cache", built_at=built_at, changes=changes)


def _recent(built_at: str, max_age_days: int) -> bool:
    try:
        return (dt.date.today() - dt.date.fromisoformat(built_at)).days <= max_age_days
    except ValueError:
        return False


def build_timeline(
    path: Path,
    start: dt.date,
    end: dt.date,
    sample_step_days: int = 5,
    refresh: str = "auto",
    max_age_days: int = 7,
    log=print,
) -> MemberTimeline:
    """构建（或读取缓存）成员时间线。refresh: auto | force | cache_only。"""
    cached = _load_cached(path, start, end) if refresh != "force" else None
    if cached is not None and (refresh == "cache_only" or _recent(cached.built_at, max_age_days)):
        log(f"历史成分：使用缓存 {path.name}（{len(cached.segments)} 段 / 并集 {len(cached.union())} 只）")
        for item in cached.changes:
            log(
                f"  调样 {item['date']}：调入 {len(item['in'])} 只 {item['in']}，"
                f"调出 {len(item['out'])} 只 {item['out']}"
            )
        return cached
    if refresh == "cache_only":
        raise DataSourceError(f"historical 模式要求缓存时间线，但 {path} 不存在或不完整")

    samples = _weekdays(start, end, max(1, int(sample_step_days)))
    log(f"历史成分：开始构建时间线，采样 {len(samples)} 个日期（BaoStock）")
    t0 = time.time()
    segments: list[tuple[dt.date, dt.date, frozenset[str]]] = []
    names: dict[str, str] = {}
    changes: list[dict] = []

    with _BaoStockSession() as session:
        prev_codes = None
        prev_date = None
        pending_start = start
        for sd in samples:
            for attempt in range(3):
                try:
                    codes, nm = session.snapshot(sd)
                    break
                except Exception as exc:
                    if attempt == 2:
                        raise
                    time.sleep(0.5 + attempt)
            names.update(nm)
            if prev_codes is None:
                segments.append((pending_start, sd, codes))  # end 在检测到下一次调样时重写
                prev_codes, prev_date = codes, sd
                continue
            if codes != prev_codes:
                exact = sd
                cur = prev_date + dt.timedelta(days=1)
                while cur <= sd:
                    if cur.weekday() < 5 and session.snapshot(cur)[0] != prev_codes:
                        exact = cur
                        break
                    cur += dt.timedelta(days=1)
                changes.append(
                    {"date": exact, "in": sorted(codes - prev_codes), "out": sorted(prev_codes - codes)}
                )
                s0, _, c0 = segments[-1]
                segments[-1] = (s0, exact - dt.timedelta(days=1), c0)
                segments.append((exact, end, codes))
                prev_codes, prev_date = codes, sd
        if segments:
            s0, _, c0 = segments[-1]
            segments[-1] = (s0, end, c0)

    timeline = MemberTimeline(
        segments=segments, names=names, source="baostock", built_at=dt.date.today().isoformat(), changes=changes
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    timeline.to_frame().to_csv(path, index=False, encoding="utf-8-sig")
    log(
        f"历史成分：时间线完成，{len(segments)} 段 / {len(changes)} 次调样 / 并集 {len(timeline.union())} 只，"
        f"耗时 {time.time() - t0:.1f}s，已写入 {path}"
    )
    for item in changes:
        log(
            f"  调样 {item['date']}：调入 {len(item['in'])} 只 {item['in']}，"
            f"调出 {len(item['out'])} 只 {item['out']}"
        )
    return timeline
