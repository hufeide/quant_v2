# 上证 50 多策略多子账户量化回测系统（Backtrader）

一套配置外置、策略可插拔、按比例切分子账户的 A 股日线回测工程。
策略只负责决策，仓位/风控是公共模块，订单执行与账户记账交给 Backtrader。

## 一、目录结构

```
temp_20261106_01/
├── config/
│   ├── config.yaml          # 基础配置（所有默认参数）
│   ├── backtest.yaml        # 回测配置，extends config.yaml
│   ├── live.yaml            # 纸面跟踪配置，extends config.yaml
│   └── quick.yaml           # 冒烟测试（单策略、少量标的、短区间）
├── core/
│   └── config.py            # YAML 加载、extends 继承、参数校验
├── data/
│   ├── source.py            # 数据源：akshare 东财 / akshare 新浪 / 腾讯 / BaoStock
│   ├── loader.py            # 成分股、日线缓存、增量更新、离线兜底、按日期切片
│   ├── timeline.py          # 上证50 历史成分股时间线（BaoStock 快照）
│   └── cache/               # parquet 日线 + 成分股 CSV（运行后生成）
├── strategy/
│   ├── base.py              # BaseStrategy（交易流程协调者）
│   ├── registry.py          # 装饰器 + 目录扫描注册表
│   ├── pullback.py          # A1 缩量回踩
│   ├── breakout.py          # A2 平台突破
│   ├── oversold.py          # A3 超跌反弹
│   └── momentum.py          # A4 强者恒强
├── engine/
│   ├── runner.py            # 多子账户 Cerebro 运行与总账户汇总
│   ├── broker.py            # ChinaABroker：涨跌停/停牌/参与率成交约束
│   ├── constraints.py       # 涨跌停规则、拒单记录与统计
│   ├── portfolio.py         # PortfolioManager / PositionSizer / RiskManager
│   ├── commission.py        # A 股佣金 + 卖出印花税
│   └── analyzers.py         # 净值/持仓/成交/订单分析器
├── analysis/
│   ├── metrics.py           # 收益、年化、回撤、夏普、胜率、月度收益
│   ├── plots.py             # 净值曲线 + 回撤图
│   └── report.py            # CSV / PNG / Markdown 报告
├── output/                  # 每次回测一个时间戳目录
├── main.py                  # CLI 入口
├── requirements.txt
└── README.md
```

## 二、安装与运行

```powershell
pip install -r requirements.txt

# 拉取/更新数据（可离线复用缓存）
python main.py data --config config/backtest.yaml

# 冒烟测试：单策略、8 只标的、2025 年起
python main.py backtest --config config/quick.yaml

# 正式回测：4 个子账户、历史成分口径、成交约束开启
python main.py backtest --config config/backtest.yaml

# 改造前/后 2×2 对照（成分口径 × 成交约束，自动重跑 4 组）
python main.py compare --config config/backtest.yaml

# 查看已注册策略
python main.py list-strategies
```

`--config` 可指向任意 YAML；配置支持 `extends`，子配置只需写与父配置不同的部分。

## 三、配置说明（`config/config.yaml`）

| 区块 | 关键参数 | 说明 |
|---|---|---|
| `data` | `provider`、`index_code`、`start_date`、`end_date`、`refresh` | 数据源、标的池、区间、缓存策略 |
| `universe` | `mode: current / historical`、`historical.timeline_file`、`sample_step_days` | 当前成分 or 历史调样时间线 |
| `constraints` | `enabled`、`max_pending_bars`、`limit.*`、`suspension.*`、`participation.max_rate` | 涨跌停/停牌/成交量参与率 |
| `broker` | `cash`、`commission`、`min_commission`、`stamp_duty`、`slippage_perc` | 初始资金与交易成本 |
| `engine` | `run_mode`、`lot_size`、`trade_start`、`risk_free_rate` | 运行模式、整手交易、信号起始日 |
| `risk_defaults` | `max_positions`、`max_position_pct`、`risk_per_trade_pct`、`atr_multiple`、`max_hold_days`、`max_drawdown_stop` | 子账户公共风控 |
| `sizer_defaults` | `risk_per_trade_pct`、`atr_multiple`、`max_position_pct`、`min_stop_pct` | ATR 风险预算仓位 |
| `accounts` | `cash_reserve`、`subaccounts[].weight / strategy / params / risk / sizer` | 总账户按比例切分 |

校验规则：`cash_reserve + Σ(weight) == 1.0`，否则抛配置错误并给出实际合计值；
子账户名称唯一、策略必须已在注册表中存在、日期区间合法。

### 子账户资金分配示例

```yaml
accounts:
  cash_reserve: 0.20
  subaccounts:
    - {name: a_pullback,  strategy: pullback, weight: 0.25, ...}
    - {name: b_breakout,  strategy: breakout, weight: 0.25, ...}
    - {name: c_oversold,  strategy: oversold, weight: 0.15, ...}
    - {name: d_momentum,  strategy: momentum, weight: 0.15, ...}
```

总账户 100 万时：四个子账户初始资金分别为 25 万 / 25 万 / 15 万 / 15 万，
现金池 20 万不参与交易，但计入总净值。

**实现方式**：每个子账户拥有独立 `Cerebro + Broker + 持仓`，按自己的初始资金独立记账，
天然避免“不同策略对同一标的多空持仓被 netting 合并”的失真；回测结束后逐日对齐、
把子账户净值与现金池相加得到总账户净值。这是真正的资金切分，而不是共享账户的伪分仓。

## 四、新增策略（不改引擎代码）

1. 在 `strategy/` 下新建文件，例如 `mystrategy.py`：

```python
import backtrader as bt
from strategy.base import BaseStrategy
from strategy.registry import register_strategy

@register_strategy("mysignal", "我的信号")
class MyStrategy(BaseStrategy):
    def build_indicators(self, data):
        return {
            "ma": bt.indicators.SMA(data.close, period=int(self.cfg.get("ma", 20))),
            "atr": bt.indicators.ATR(data, period=14),
        }

    def signal(self, data, ind):
        if data.close[0] > ind["ma"][0]:
            return {"score": 80, "reason": "收盘价上穿均线"}
        return None
```

2. 在 YAML 里挂上即可生效：

```yaml
subaccounts:
  - name: e_mysignal
    strategy: mysignal
    weight: 0.10
    params: {ma: 20}
```

`registry.py` 会扫描 `strategy/` 目录并导入所有模块，装饰器负责登记。
`BaseStrategy` 已统一处理：ATR 止损/止盈、移动止损、最大持有天数、
最大持仓数、总仓位上限、单票上限、回撤熔断、每日开仓上限、订单去重。

## 五、换标的池

- 换指数：修改 `data.index_code`（如沪深 300 为 `000300`），同步改 `universe_name`；
- 只取部分标的：`data.max_symbols` 或 `data.exclude_symbols: ["600028", ...]`；
- 自定义列表：把 `data.fallback_universe` 指向自己的 CSV（列：`code,name`），
  并设 `data.refresh: cache_only` 可完全离线运行；
- 成分口径（`universe.mode`）：
  - `current`：当前成分股回填历史，存在生存者偏差，仅适用于快速验证；
  - `historical`：按历史调样时间线逐日切片，回测每一天只使用当日实际成分股。

```yaml
universe:
  mode: historical
  historical:
    timeline_file: data/cache/universe/sse50_timeline.csv
    sample_step_days: 5       # 采样粒度（自然日）
    refresh: auto             # auto | force | cache_only
```

`historical` 模式首次运行会调用 BaoStock 构建时间线（约 5~6 分钟，结果落盘缓存），
之后按 `max_age_days` 复用。成分变动会写入控制台与报告；若 BaoStock 不可用且无缓存，
会直接报错而不是用当前成分静默回填。历史并集会扩充到约 79 只（当前 50 只之外
的旧成分股），因此首次行情下载也会更久。
晚上市标的（如 2025-07 上市的 600930）会保留在并集中，但上市前不参与交易；
引擎已避免 Backtrader 多数据（指标）预热把整段回测推迟到最晚上市标的预热完成。
注意：`historical` 模式会忽略 `data.max_symbols`，否则会截断历史并集。

## 六、数据层行为

- 优先 akshare 东方财富日线（`stock_zh_a_hist`），连续失败两次后本次运行自动停用该源，
  降级到 akshare 新浪日线（`stock_zh_a_daily`），再降级到腾讯行情 HTTP 接口，
  最后可用 BaoStock 日线兜底；
- 所有行情前复权，统一为 `date/open/high/low/close/volume(股)/amount`；
- 每只标的一个 `data/cache/daily/<code>.parquet`，增量更新（重叠 20 天重取以修正前复权因子），
  无网络时用缓存并在日志/报告中明确提示；
- 成分股列表缓存 30 天，另有 `data/universe/sse50_snapshot.csv` 离线快照；
- 历史成分时间线缓存在 `data/cache/universe/sse50_timeline.csv`，
  列为 `start_date, end_date, code, name, built_at`，可直接审计每一段成分。

## 七、产出

每次运行生成 `output/<名称>_<时间戳>/`：

- `report.md`：配置、数据状态、总账户与子账户指标、成交约束/拒单统计、月度收益、期末持仓、最近交易、局限说明
- `equity_curve.png`：子账户与总账户累计收益曲线 + 总账户回撤
- `summary.csv`、`monthly_returns.csv`、`master_equity.csv`、`master_trades.csv`
- `rejected_orders.csv`：被拒/部分成交订单明细（日期、方向、数量、原因、开盘价、前收盘、涨跌停价、成交量）
- `accounts/<子账户>/`：`equity.csv`、`positions.csv`、`trades.csv`、`orders.csv`、
  `rejected_orders.csv`、`final_positions.csv`、`metrics.json`、`order_stats.json`
- `config_used.yaml`：本次运行配置快照，便于复现

`python main.py compare` 额外生成 `output/compare_<时间戳>/comparison.md` 与
`comparison.csv`，以及 A/B/C/D 四组完整产物目录。

### 成交约束实现要点

- 涨跌停：执行日开盘价等于涨停价时买单不成交、等于跌停价时卖单不成交；
  涨跌停价按前收盘 × (1±比例) 并以 0.01 元四舍五入；
- 一字板/连续跌停：订单**不撤销**，保持 pending，每个交易日自动重试，
  连续 `constraints.max_pending_bars` 个交易日仍无法成交才撤单并记录；
- 停牌：执行日该标的无 K 线或成交量为 0 时不成交；持仓停牌期间不会产生成交，
  复牌后按当日开盘价执行；
- 成交量参与率：单笔成交量超过当日成交量 `max_rate` 时只部分成交，剩余部分次日续挂；
- 所有拒绝/部分成交事件写入 `rejected_orders.csv`，报告"成交约束与拒单统计"
  分涨停买入、跌停卖出、停牌、零成交、超时撤单五类计数并给出拒单率。

## 八、本次验证结果（2026-11-06 实际运行）

### 8.1 历史成分数据可行性

- 数据源：BaoStock `query_sz50_stocks(date)`，可按任意历史日期返回当日 50 只上证 50 成分股
  （实测 2015 / 2020 / 2022 / 2023 等日期均返回 50 只），行格式 `[更新日期, sh.600028, 中国石化]`；
  “更新日期”是厂商每周刷新戳，不是调样生效日。
- 落地方式：以 5 个自然日为采样步长扫描全区间，相邻采样快照不一致时逐日回查，
  以“首个成分发生变化的交易日”作为该次调样的生效日；产物
  `data/cache/universe/sse50_timeline.csv`（列 `start_date,end_date,code,name,built_at`）。
- 实测覆盖：2023-01-01 ~ 2026-11-05 共 9 段、8 次变动、并集 79 只
  （当前 50 只之外含 29 只历史成分）。首次构建约 5~6 分钟（201 个采样点，341.9 秒），
  之后按 `max_age_days` 复用缓存。
- 可信度说明：生效日以快照第一次变化为准，可能与官方公告相差 1 个交易日；
  2023-03-20 的变动由快照差异检测得到，不排除是非定期调整或厂商数据修正。
  当前时间线只覆盖 2023-01-01 起，更早区间需 `universe.historical.refresh: force` 重建。
- 降级方案：BaoStock 不可用且无本地时间线缓存时直接报错，不会回退到当前成分静默回填；
  若只需近似，可增大 `sample_step_days` 缩短构建时间（口径等价于按采样点近似）。

### 8.2 改造前 vs 改造后（2×2 因子对照）

命令：`python main.py compare --config config/backtest.yaml`。同一区间 2023-01-01 ~ 2026-11-05、
同一初始资金 100 万、同一组策略参数，仅 `universe.mode` 与 `constraints.enabled` 两项不同：

| 组合 | 成分口径 | 成交约束 | 总收益 | 年化 | 最大回撤 | 夏普 | 交易次数 | 提交订单 | 被拒订单 | 拒单率 |
|---|---|---|---|---|---|---|---|---|---|---|
| A | current | 关（改造前基线） | 43.66% | 9.90% | -9.96% | 0.9067 | 985 | 1985 | 0 | 0.00% |
| B | historical | 关 | 18.20% | 4.45% | -12.87% | 0.3465 | 940 | 1895 | 0 | 0.00% |
| C | current | 开 | 33.99% | 7.92% | -9.97% | 0.7345 | 984 | 1991 | 12 | 0.60% |
| D | historical | 开（改造后正式口径） | 11.96% | 2.99% | -12.61% | 0.1718 | 933 | 1890 | 13 | 0.69% |

变化归因（百分点，总收益口径）：

| 项 | 计算 | 数值 |
|---|---|---|
| 成分口径贡献（无约束） | B − A | **-25.46pp** |
| 成分口径贡献（有约束） | D − C | -22.03pp |
| 成交约束贡献（当前成分） | C − A | **-9.67pp** |
| 成交约束贡献（历史成分） | D − B | -6.24pp |
| 交互项 | (D−B) − (C−A) | +3.43pp |
| 合计 | D − A | -31.70pp |

结论：收益下滑的**主因是成分口径**（消除生存者偏差），无约束口径下单独贡献 -25.46pp，
占合计降幅的约 80%；成交约束单独贡献 -9.67pp（当前成分）/ -6.24pp（历史成分），
两项不是简单相加，二因子分解的交互项为 +3.43pp。A 组复现了改造前基线
（43.66% / 985 笔，与原 43.66% / 985 笔一致，差异来自四舍五入）。

成交约束的影响高度集中在 `b_breakout`：
当前口径 90.91% → 57.40%（-33.51pp），历史口径 43.43% → 16.89%（-26.54pp）。
直接触发点是 2024-09-30 / 2024-10-08 的涨停板排队（601888、601688、600030、600036 等）：
订单在涨停开盘日不能成交，顺延到节后更高的开盘价成交；同时挂单期间该标的不能重复入场，
后续的入场名额与离场日期整体位移，带来路径依赖式的成交序列变化。
其余子账户约束影响在 -10.01pp ~ +1.33pp 之间。

D 组拒单统计：提交 1890 笔、被拒 13 笔（0.69%）；按事件口径涨停买 12 次、跌停卖 2 次、
停牌/零成交 16 次、超时撤单 1 次。按提交订单口径，涨跌停被拒占比 0.74%、停牌/无成交被拒占比 0.85%。
停牌拒单全部来自数据缺口型停牌：600150 连续 10 个交易日无 K 线（挂满 10 日后撤单）、
688981 连续 6 个交易日数据缺口（复牌后成交）。可逐笔审计 `rejected_orders.csv`。

### 8.3 改造后正式回测（D 组）

100 万总账户按 25/25/15/15 切分 + 20% 现金池：

| 账户 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 交易次数 |
|---|---|---|---|---|---|---|
| a_pullback | 7.68% | 1.95% | -17.64% | 0.06 | 42.74% | 248 |
| b_breakout | 16.89% | 4.15% | -21.38% | 0.22 | 37.72% | 228 |
| c_oversold | 7.90% | 2.00% | -6.62% | 0.04 | 55.21% | 96 |
| d_momentum | 30.86% | 7.26% | -17.11% | 0.43 | 33.24% | 361 |
| **总账户** | **11.96%** | **2.99%** | **-12.61%** | **0.17** | **39.12%** | **933** |

期末总净值 1,119,562.07 元，总佣金 29,753.73 元，盈亏比 1.15。
完整产物见 `output/compare_20261106_181939/D_historical_constrained/`，
对照表见同级的 `comparison.md` / `comparison.csv`。

### 8.4 已验证范围

- 单元测试：涨停开盘买不进 → 记录 `limit_up` → 次日正常成交；跌停卖不出；停牌 → 记录
  `suspended` → 复牌按规则成交；成交量参与率部分成交；挂单超时撤单。
- 全量回测：4 个子账户、79 只历史并集、覆盖 8 次成分调整，每个子账户输出
  `rejected_orders.csv` 与 `order_stats.json`。
- 数据层：79/79 只历史并集标的行情可用（其中 3 只增量抓取、76 只命中缓存）。


## 九、已知局限

- 历史成分只覆盖 2023-01-01 起；成分切换以 BaoStock 快照第一次变化的交易日为准，
  可能与官方公告生效日相差 1 个交易日，2023-03-20 的变动来源需进一步核对；
- 停牌以“当日无 K 线或成交量为 0”近似，无法区分真实停牌与数据源缺口
  （本次 600150、688981 的连续拒单即属于数据缺口型“停牌”）；
- 涨跌停价按前复权价 ×(1±比例) 并以 0.01 元取整，与交易所未复权价存在细微差异；
  ST 判定使用当前代码/名称近似，未还原历史 ST 状态；
- 未模拟集合竞价、盘中开板后的排队与部分成交、分红现金流、行业中性/集中度约束；
- 成交量参与率上限已实现（部分成交 + 次日续挂），但本次样本未触发部分成交（0 事件），
  该逻辑目前仅由单元测试覆盖；
- `current` 模式仍保留生存者偏差，只用于与旧结果对照，不应作为策略评估依据；
- 晚上市标的（如 600930，2025-07 上市）在 `historical` 模式中保留但不参与上市前交易；
  策略已绕过 Backtrader 多数据预热延迟，不会因单一晚上市标的把整段回测推迟到其指标预热完成；
- 数据源限流时会自动降级到备用源或使用缓存，报告中会给出提示；
- `live.yaml` 只是纸面跟踪配置（复用回测引擎跑最新区间），未接券商实盘接口。
