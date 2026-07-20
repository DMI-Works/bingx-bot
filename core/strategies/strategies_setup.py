from .registry import STRATEGY_REGISTRY

def setup_strategies(event_bus, config, logger):
    enabled_strategies = config.get('strategies.enabled', [])
    strategies = []
    
    logger.info(f"[ Enabled STRATEGIES ]: {len(enabled_strategies)}")

    for name in enabled_strategies:
        strategy_cls = STRATEGY_REGISTRY.get(name)
        if strategy_cls is None:
            logger.warning(f"[SKIP] Unknown strategy in config: {name}")
            continue

        strategy_config = strategy_cls.build_config(config)
        strategy = strategy_cls(event_bus, strategy_config)
        strategy.enable()
        strategies.append(strategy)
        logger.info(f"[OK] {name} enabled")

    return strategies