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


def _configured_tushare_token() -> str | None:
    config_path = Path(
        os.getenv("QUANT_CONFIG_FILE") or DEFAULT_UNIFIED_CONFIG
    )
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            value = str(
                (payload.get("market_data") or {}).get("tushare_token") or ""
            ).strip()
            placeholder = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", value)
            if placeholder:
                value = os.getenv(placeholder.group(1), "").strip()
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
