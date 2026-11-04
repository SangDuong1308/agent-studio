"""Centralised configuration.

Every service reads its config from environment variables through this single
``Settings`` object. Using ``pydantic-settings`` gives us typed, validated
config with a clear source of truth (the ``.env`` file in local dev, real env
vars in Kubernetes).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = Field(
        default="postgresql+asyncpg://agent:agent@localhost:5432/agentstudio",
        alias="DATABASE_URL",
    )

    # --- RabbitMQ ---
    rabbitmq_url: str = Field(
        default="amqp://guest:guest@localhost:5672/",
        alias="RABBITMQ_URL",
    )

    # --- Environment ---
    # "production" turns on fail-fast checks (e.g. refuses the default JWT secret).
    app_env: Literal["local", "production"] = Field(default="local", alias="APP_ENV")

    # --- Security ---
    # Shared secret for service-to-service calls (gateway -> agent-runtime). When
    # empty the check is skipped, which is only acceptable for local development.
    internal_api_key: str = Field(default="", alias="INTERNAL_API_KEY")
    # Comma-separated list of browser origins allowed to call the gateway.
    cors_origins: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    # Per-process limits (requests per window). Behind several replicas each one
    # keeps its own counters; move to Redis if you need a global limit.
    rate_limit_auth_per_minute: int = Field(
        default=10, alias="RATE_LIMIT_AUTH_PER_MINUTE"
    )
    rate_limit_query_per_minute: int = Field(
        default=20, alias="RATE_LIMIT_QUERY_PER_MINUTE"
    )
    rate_limit_create_per_hour: int = Field(
        default=10, alias="RATE_LIMIT_CREATE_PER_HOUR"
    )
    max_agents_per_user: int = Field(default=20, alias="MAX_AGENTS_PER_USER")

    # --- Auth ---
    jwt_secret: str = Field(default="change-me", alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=60, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )

    # --- LLM provider ---
    llm_provider: Literal["vertex", "fake"] = Field(
        default="fake", alias="LLM_PROVIDER"
    )
    google_cloud_project: str = Field(default="", alias="GOOGLE_CLOUD_PROJECT")
    # Gemini 3.x models are served from the global endpoint, not us-central1.
    google_cloud_location: str = Field(
        default="global", alias="GOOGLE_CLOUD_LOCATION"
    )
    vertex_chat_model: str = Field(
        default="gemini-3.5-flash-lite", alias="VERTEX_CHAT_MODEL"
    )
    vertex_embedding_model: str = Field(
        default="text-embedding-005", alias="VERTEX_EMBEDDING_MODEL"
    )
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    # Cap on tokens the chat model may generate per answer (cost + latency guard).
    llm_max_output_tokens: int = Field(default=2048, alias="LLM_MAX_OUTPUT_TOKENS")
    # Seconds before a model call is abandoned, and how many times it is retried.
    llm_timeout_seconds: float = Field(default=45.0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=2, alias="LLM_MAX_RETRIES")

    # --- Saga ---
    # A saga that makes no progress for this long is declared failed and rolled
    # back (covers a lost message, a crashed worker, or a poisoned message).
    saga_timeout_seconds: int = Field(default=300, alias="SAGA_TIMEOUT_SECONDS")
    # How often the orchestrator looks for such stuck sagas.
    saga_reaper_interval_seconds: int = Field(
        default=30, alias="SAGA_REAPER_INTERVAL_SECONDS"
    )

    # --- Retrieval ---
    retrieval_top_k: int = Field(default=4, alias="RETRIEVAL_TOP_K")
    # Cosine distance (0 = identical, 2 = opposite). Chunks farther than this are
    # treated as not relevant and are not shown to the model.
    # Measured with text-embedding-005 on a small corpus: relevant questions score
    # 0.32-0.53, small talk 0.58-0.61, off-topic 0.62-0.67. Re-check this when
    # the embedding model or typical documents change.
    retrieval_max_distance: float = Field(default=0.55, alias="RETRIEVAL_MAX_DISTANCE")

    # --- LangFuse ---
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", alias="LANGFUSE_HOST"
    )

    # --- Inter-service ---
    agent_runtime_url: str = Field(
        default="http://localhost:8003", alias="AGENT_RUNTIME_URL"
    )

    # --- Logging ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def assert_production_safe(self) -> None:
        """Fail fast on unsafe defaults. Call once at service start-up."""
        if self.app_env != "production":
            return
        problems: list[str] = []
        if self.jwt_secret in {"", "change-me"} or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be set to a random value of 32+ characters")
        if not self.internal_api_key:
            problems.append("INTERNAL_API_KEY must be set")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must list explicit origins, not '*'")
        if problems:
            raise RuntimeError("Unsafe production config: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so every import shares one validated Settings instance."""
    return Settings()
