"""行情数据源适配层。

优先级（auto 模式）：
  1. akshare 东方财富日线接口（ak.stock_zh_a_hist）
  2. akshare 新浪日线接口（ak.stock_zh_a_daily）
  3. 腾讯行情 HTTP 接口（无需额外依赖）
  4. 本地缓存（网络失败兜底）

所有源统一输出列：date/index, open, high, low, close, volume, amount。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pandas as pd

STANDARD_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


class DataSourceError(RuntimeError):
    pass


@dataclass
class SourceStats:
    used: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    disabled: set[str] = field(default_factory=set)
    consecutive: dict[str, int] = field(default_factory=dict)

    def hit(self, name: str) -> None:
        self.used[name] = self.used.get(name, 0) + 1
        self.consecutive[name] = 0

    def fail(self, name: str, exc: Exception) -> None:
        self.errors.append(f"{name}: {type(exc).__name__}: {exc}")
        self.consecutive[name] = self.consecutive.get(name, 0) + 1
        # 连续失败两次就暂时停用该源，避免被持续限流（例如东方财富 IP 限流）
        if self.consecutive[name] >= 2:
            self.disabled.add(name)


def exchange_prefix(symbol: str) -> str:
    """6 位代码 -> 交易所前缀。"""
    code = symbol.zfill(6)
    if code.startswith(("6", "5", "9")):
        return "sh"
    if code.startswith(("0", "3", "2", "1")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    return "sh"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" not in df.columns:
        df["amount"] = 0.0
    df = df[STANDARD_COLUMNS]
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[df["close"] > 0]
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return df.set_index("date", drop=True)


class AkshareEastmoneySource:
    name = "akshare_em"

    def __init__(self, adjust: str = "qfq"):
        self.adjust = adjust

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        raw = ak.stock_zh_a_hist(
            symbol=symbol.zfill(6),
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust=self.adjust,
        )
        if raw is None or raw.empty:
            raise DataSourceError("东方财富接口返回空数据")
        raw = raw.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce") * 100.0  # 手 -> 股
        return _normalize(raw)


class AkshareSinaSource:
    name = "akshare_sina"

    def __init__(self, adjust: str = "qfq"):
        self.adjust = adjust

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        raw = ak.stock_zh_a_daily(
            symbol=f"{exchange_prefix(symbol)}{symbol.zfill(6)}",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust=self.adjust,
        )
        if raw is None or raw.empty:
            raise DataSourceError("新浪接口返回空数据")
        return _normalize(raw.reset_index())


class TencentSource:
    name = "tencent"

    def __init__(self, adjust: str = "qfq"):
        self.adjust = adjust

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import requests

        code = f"{exchange_prefix(symbol)}{symbol.zfill(6)}"
        mode = "qfq" if self.adjust == "qfq" else ""
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        frames = []
        cursor = s
        while cursor <= e:
            chunk_end = min(cursor + pd.DateOffset(years=2), e)
            url = (
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                f"?param={code},day,{cursor.date()},{chunk_end.date()},640,{mode}"
            )
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            block = payload.get("data", {}).get(code, {})
            rows = block.get("qfqday") or block.get("day") or []
            for row in rows:
                frames.append(
                    {
                        "date": row[0],
                        "open": row[1],
                        "close": row[2],
                        "high": row[3],
                        "low": row[4],
                        "volume": float(row[5]) * 100.0,  # 手 -> 股
                        "amount": 0.0,
                    }
                )
            cursor = chunk_end + pd.Timedelta(days=1)
        if not frames:
            raise DataSourceError("腾讯接口返回空数据")
        return _normalize(pd.DataFrame(frames))


class BaoStockSource:
    """BaoStock 日线（前复权），作为 akshare/腾讯全部失败后的兜底。

    对已退市/长期停牌标的的覆盖优于免费 HTTP 接口，且与历史成分时间线同源。
    """

    name = "baostock"
    _logged_in = False
    _bs = None

    def __init__(self, adjust: str = "qfq"):
        self.adjust = adjust

    def _session(self):
        if not BaoStockSource._logged_in:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != "0":
                raise DataSourceError(f"BaoStock 登录失败：{lg.error_msg}")
            BaoStockSource._bs = bs
            BaoStockSource._logged_in = True
        return BaoStockSource._bs

    def fetch_daily(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        bs = self._session()
        adjustflag = "2" if self.adjust == "qfq" else "3"
        rs = bs.query_history_k_data_plus(
            f"{exchange_prefix(symbol)}{symbol.zfill(6)}",
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            raise DataSourceError(f"BaoStock 日线失败：{rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            raise DataSourceError("BaoStock 日线返回空数据")
        raw = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        for col in ("open", "high", "low", "close", "volume", "amount"):
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
        return _normalize(raw)


def available_sources(preferred: str = "auto", adjust: str = "qfq") -> list:
    mapping = {
        "akshare_em": [AkshareEastmoneySource],
        "akshare_sina": [AkshareSinaSource],
        "tencent": [TencentSource],
        "baostock": [BaoStockSource],
        "auto": [AkshareEastmoneySource, AkshareSinaSource, TencentSource, BaoStockSource],
        "cache": [],
    }
    if preferred not in mapping:
        raise ValueError(f"未知 data.provider: {preferred}")
    return [cls(adjust=adjust) for cls in mapping[preferred]]


def fetch_with_fallback(
    sources: list, symbol: str, start: str, end: str, stats: SourceStats
) -> tuple[pd.DataFrame | None, str]:
    for src in sources:
        if src.name in stats.disabled:
            continue
        try:
            df = src.fetch_daily(symbol, start, end)
            if df is None or df.empty:
                raise DataSourceError("空数据")
            stats.hit(src.name)
            return df, src.name
        except Exception as exc:  # 网络/接口异常都降级到下一个源
            stats.fail(src.name, exc)
            time.sleep(0.2)
    return None, "cache"
