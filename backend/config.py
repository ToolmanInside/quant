from __future__ import annotations

import os
from dataclasses import dataclass
import json
from pathlib import Path
import re

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
DEFAULT_UNIFIED_CONFIG = PROJECT_ROOT / "config" / "quant-config.json"

_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def resolve_environment_placeholder(
    value: object,
    field_name: str,
    *,
    required: bool = True,
) -> str:
    """解析 ``${ENV_VAR}`` 占位符；配置加载的唯一实现（后端与 job 共用）。"""
    text = str(value or "").strip()
    match = _PLACEHOLDER_PATTERN.fullmatch(text)
    if match:
        text = os.getenv(match.group(1), "").strip()
    if not text and required:
        raise ValueError(f"配置项 {field_name} 为空")
    return text


def _configured_tushare_token() -> str | None:
    config_path = Path(
        os.getenv("QUANT_CONFIG_FILE") or DEFAULT_UNIFIED_CONFIG
    )
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            raw = (payload.get("market_data") or {}).get("tushare_token")
            value = resolve_environment_placeholder(
                raw,
                "market_data.tushare_token",
                required=False,
            )
            if value:
                return value
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return os.getenv("TUSHARE_TOKEN") or None


@dataclass(frozen=True)
class Settings:
    tushare_token: str | None = _configured_tushare_token()
    app_name: str = "Quant Lab"
    app_version: str = "0.1.0"


settings = Settings()
