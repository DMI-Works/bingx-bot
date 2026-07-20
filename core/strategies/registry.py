from typing import Dict, Type
from .base_strategy import BaseStrategy

STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {}

def register_strategy(name: str):
    def decorator(cls: Type[BaseStrategy]):
        STRATEGY_REGISTRY[name] = cls
        return cls
    return decorator