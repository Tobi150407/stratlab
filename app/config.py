"""Central configuration. Every value can be overridden with an environment variable
prefixed ``SL_`` (e.g. ``SL_DATABASE_URL``). See ``.env.example``."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SL_", env_file=".env", extra="ignore")

    # --- core -------------------------------------------------------------
    env: str = "development"
    database_url: str = "postgresql+psycopg://strategylab:strategylab@localhost:5432/strategylab"
    api_token: str = "change-me"  # bearer token for mutating API calls
    log_level: str = "INFO"
    log_json: bool = True

    # --- market data ------------------------------------------------------
    # "simulated" (default, no key needed) or "twelvedata"
    data_provider: str = "simulated"
    twelvedata_api_key: str = ""
    twelvedata_base_url: str = "https://api.twelvedata.com"
    # instrument symbol -> provider symbol
    gold_provider_symbol: str = "XAU/USD"
    nasdaq_provider_symbol: str = "NDX"
    poll_interval_seconds: int = 20
    # a candle older than (timeframe + this grace) at evaluation time is stale
    stale_grace_seconds: int = 120
    # feed considered interrupted if no successful update for this long while market open
    feed_timeout_seconds: int = 300
    simulated_speedup: float = 1.0  # >1 only for demos

    # --- engine -----------------------------------------------------------
    heartbeat_interval_seconds: int = 15
    component_dead_after_seconds: int = 90
    default_quantity: float = 1.0

    # --- research ---------------------------------------------------------
    research_worker_concurrency: int = 1
    job_max_attempts: int = 3
    job_lease_seconds: int = 1800

    # --- notifications ----------------------------------------------------
    webhook_url: str = ""  # Slack / Discord / generic JSON webhook
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email_to: str = ""
    alert_cooldown_seconds: int = 900

    # --- deterioration detection -----------------------------------------
    deterioration_min_trades: int = 10
    deterioration_sharpe_drop: float = 0.5  # recent Sharpe below baseline by this much
    deterioration_max_drawdown_pct: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
