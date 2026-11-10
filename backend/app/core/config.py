from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", ROOT_DIR / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    revolut_x_api_key: str = ""
    revolut_x_private_key_path: str = ""
    revolut_x_base_url: str = "https://revx.revolut.com"
    revolut_x_region: str = "UK"
    revolut_x_timeout: float = 10.0

    database_url: str = "postgresql+asyncpg://crypto:crypto@localhost:5433/crypto_terminal"

    market_poll_interval: float = 3.0
    ticker_poll_interval: float = 3.0
    stale_data_seconds: float = 30.0
    history_candles: int = 1000

    paper_starting_balance: Decimal = Decimal("10000")
    paper_fee_rate: Decimal = Decimal("0.0009")

    order_status_poll_interval: float = 0.5
    order_status_timeout: float = 15.0
    live_connection_max_age_seconds: float = 600.0

    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def credentials_configured(self) -> bool:
        return bool(self.revolut_x_api_key and self.revolut_x_private_key_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
