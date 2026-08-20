import inspect
import logging
from typing import Dict, List, Optional

from .registry import STRATEGY_REGISTRY
from .base_strategy import BaseStrategy


class StrategyManager:
    """
    Держит живі інстанси всіх зареєстрованих стратегій та є єдиною точкою,
    через яку Telegram-меню (SettingsMenu) змінює їх стан у рантаймі —
    на відміну від StrategySettingsStore, який лише зберігає стан у БД.

    Інстанс створюється для КОЖНОЇ стратегії з реєстру одразу при setup(),
    незалежно від того, enabled вона чи ні — вимкнена стратегія просто
    підписана на шину подій, але self.enabled=False і вона нічого не робить
    (див. BaseStrategy._on_price_update). Це дозволяє вмикати її пізніше
    без рестарту бота.
    """

    def __init__(self, event_bus, config, logger: logging.Logger, strategy_settings, bingx_client=None):
        self.event_bus = event_bus
        self.config = config
        self.logger = logger
        self.store = strategy_settings
        self.bingx_client = bingx_client
        self.instances: Dict[str, BaseStrategy] = {}

    def setup(self) -> List[BaseStrategy]:
        initially_enabled = set(self.config.get('strategies.enabled', []))
        self.logger.info(f"[ Registered STRATEGIES ]: {len(STRATEGY_REGISTRY)}")

        for name, strategy_cls in STRATEGY_REGISTRY.items():
            default_config = strategy_cls.build_config(self.config)
            self.store.seed_defaults(name, default_config, enabled=(name in initially_enabled))

            strategy_config = self.store.get_params(name)
            if strategy_config is None:
                self.logger.error(f"[SKIP] No params in DB for {name} even after seeding — unexpected")
                continue

            extra_kwargs = self._build_extra_kwargs(name, strategy_cls)
            strategy = strategy_cls(self.event_bus, strategy_config, **extra_kwargs)
            self.instances[name] = strategy

            if self.store.is_enabled(name):
                strategy.enable()
                self.logger.info(f"[OK] {name} enabled (params from DB)")
            else:
                strategy.disable()
                self.logger.info(f"[SKIP] {name} disabled (toggle in Telegram to enable)")

        return list(self.instances.values())

    def _build_extra_kwargs(self, name: str, strategy_cls) -> dict:
        """
        Не всі стратегії приймають bingx_client (наприклад, ті, що не
        успадковують CandleWarmupMixin) — передаємо його лише класам, чий
        __init__ явно очікує цей параметр. Це той самий принцип DI, що й
        для exchange у SimpleTrader/SymbolSelector, просто автоматизований
        для довільної кількості стратегій.
        """
        kwargs = {}
        params = inspect.signature(strategy_cls.__init__).parameters

        if 'bingx_client' in params:
            if self.bingx_client is None:
                self.logger.warning(
                    f"[{name}] очікує bingx_client, але StrategyManager його не отримав "
                    f"(bingx_client=None) — прогрів історії буде недоступний"
                )
            else:
                kwargs['bingx_client'] = self.bingx_client
                self.logger.info(f"[{name}] bingx_client передано в конструктор")

        return kwargs

    # ---------- виклики з SettingsMenu (миттєве застосування) ----------

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Оновлює enabled і в БД, і одразу на живому інстансі стратегії."""
        self.store.set_enabled(name, enabled)
        strategy = self.instances.get(name)
        if strategy is None:
            self.logger.warning(f"StrategyManager.set_enabled: немає live-інстансу для '{name}'")
            return
        strategy.enable() if enabled else strategy.disable()

    def apply_params(self, name: str, params: dict) -> None:
        """Підміняє config на живому інстансі стратегії новими значеннями."""
        strategy = self.instances.get(name)
        if strategy is None:
            self.logger.warning(f"StrategyManager.apply_params: немає live-інстансу для '{name}'")
            return
        strategy.config = params

    def get(self, name: str) -> Optional[BaseStrategy]:
        return self.instances.get(name)