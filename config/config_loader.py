import os
import yaml
from pathlib import Path
from typing import Any, Dict


class ConfigLoader:
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(self.config_path, 'r', encoding='utf-8') as f:
            content = f.read()
            content = self._replace_env_vars(content)
            self.config = yaml.safe_load(content)

    def _replace_env_vars(self, content: str) -> str:
        import re
        pattern = r'\$\{([^}]+)\}'

        def replacer(match):
            env_var = match.group(1)
            return os.getenv(env_var, match.group(0))

        return re.sub(pattern, replacer, content)

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split('.')
        value = self.config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default

        return value

    def set(self, key: str, value: Any) -> None:
        keys = key.split('.')
        config = self.config

        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        config[keys[-1]] = value

    def save(self) -> None:
        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)

    def reload(self) -> None:
        self.load()
