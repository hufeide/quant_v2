"""策略注册表：装饰器 + 目录扫描。

新增策略只需：
1. 在 strategy/ 下新建一个 .py 文件
2. 用 @register_strategy("名字") 装饰 BaseStrategy 子类
3. 在 YAML 的 accounts.subaccounts 里填 strategy: 名字
无需修改引擎代码。
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

_REGISTRY: dict[str, type] = {}
_DISCOVERED = False

_SKIP_MODULES = {"base", "registry"}


def register_strategy(name: str, display_name: str | None = None):
    def decorator(cls):
        if not name:
            raise ValueError("策略名称不能为空")
        _REGISTRY[name] = cls
        cls.strategy_name = name
        if display_name:
            cls.display_name = display_name
        return cls

    return decorator


def discover_strategies() -> dict[str, type]:
    """扫描 strategy 包下所有模块并导入，触发装饰器注册。"""
    global _DISCOVERED
    if _DISCOVERED:
        return _REGISTRY
    package_dir = Path(__file__).resolve().parent
    for module in pkgutil.iter_modules([str(package_dir)]):
        if module.name.startswith("_") or module.name in _SKIP_MODULES:
            continue
        importlib.import_module(f"strategy.{module.name}")
    _DISCOVERED = True
    return _REGISTRY


def get_strategy(name: str) -> type:
    registry = discover_strategies()
    if name not in registry:
        available = ", ".join(sorted(registry.keys()))
        raise KeyError(f"未找到策略 {name!r}，已注册策略：{available}")
    return registry[name]


def list_strategies() -> list[dict]:
    return [
        {
            "name": name,
            "class": cls.__name__,
            "display_name": getattr(cls, "display_name", ""),
        }
        for name, cls in sorted(discover_strategies().items(), key=lambda item: item[0])
    ]
