from .registry import STRATEGY_REGISTRY


def setup_strategies(event_bus, config, logger, strategy_settings):
    enabled_strategies = config.get('strategies.enabled', [])
    strategies = []

    logger.info(f"[ Enabled STRATEGIES ]: {len(enabled_strategies)}")

    for name in enabled_strategies:
        strategy_cls = STRATEGY_REGISTRY.get(name)
        if strategy_cls is None:
            logger.warning(f"[SKIP] Unknown strategy in config: {name}")
            continue

        # build_config(config) як і раніше формує "заводський" набір
        # параметрів (з fallback-дефолтами прямо в самій стратегії, оскільки
        # Він використовується лише як джерело для seed_defaults — якщо
        # default-рядок в БД вже є, seed_defaults його не перезапише.
        default_config = strategy_cls.build_config(config)
        strategy_settings.seed_defaults(name, default_config)

        strategy_config = strategy_settings.get_params(name)
        if strategy_config is None:
            logger.error(f"[SKIP] No params in DB for {name} even after seeding — unexpected")
            continue

        strategy = strategy_cls(event_bus, strategy_config)
        strategy.enable()
        strategies.append(strategy)
        logger.info(f"[OK] {name} enabled (params from DB)")

    return strategies