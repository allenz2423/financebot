"""
Delilah Financial OS - Secure Configuration Module

This module provides type-safe configuration management using Pydantic Settings.
All secrets are loaded from environment variables with strict validation.
"""

import os
import hashlib
from pathlib import Path
from functools import lru_cache
from typing import Optional, List, Set
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
    ValidationError,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecuritySettings(BaseSettings):
    """Security-related configuration settings."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # Application Security
    SECRET_KEY: SecretStr = Field(
        default=...,
        description="Cryptographic secret key for sessions and tokens"
    )
    ALLOWED_HOSTS: Set[str] = Field(
        default={"localhost", "127.0.0.1"},
        description="Allowed hostnames for request validation"
    )
    CORS_ORIGINS: List[str] = Field(
        default=["http://localhost:3000"],
        description="Allowed CORS origins"
    )
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = Field(
        default=60,
        ge=1,
        le=1000,
        description="Maximum requests per minute per user"
    )
    RATE_LIMIT_BURST: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Burst limit for rate limiting"
    )
    
    # SSRF Protection
    SSRF_ALLOWED_SCHEMES: Set[str] = Field(
        default={"https", "http"},
        description="Allowed URL schemes for external requests"
    )
    SSRF_BLOCKED_IP_RANGES: List[str] = Field(
        default=[
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "0.0.0.0/8",
            "224.0.0.0/4",
        ],
        description="IP ranges to block for SSRF protection"
    )
    
    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v):
        if isinstance(v, str):
            return set(h.strip() for h in v.split(",") if h.strip())
        return set(v)
    
    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return list(v)
    
    @field_validator("SSRF_BLOCKED_IP_RANGES", mode="before")
    @classmethod
    def parse_blocked_ips(cls, v):
        if isinstance(v, str):
            return [r.strip() for r in v.split(",") if r.strip()]
        return list(v)


class DatabaseSettings(BaseSettings):
    """Database configuration settings."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    DB_PATH: str = Field(
        default="data/finances.db",
        description="Path to SQLite database file"
    )
    DB_BUSY_TIMEOUT: int = Field(
        default=10000,
        ge=1000,
        le=60000,
        description="SQLite busy timeout in milliseconds"
    )
    DB_POOL_SIZE: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Database connection pool size"
    )
    DB_MAX_OVERFLOW: int = Field(
        default=10,
        ge=0,
        le=50,
        description="Maximum overflow connections"
    )
    
    @model_validator(mode="after")
    def ensure_db_directory(self):
        db_dir = Path(self.DB_PATH).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        return self


class DiscordSettings(BaseSettings):
    """Discord bot configuration."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    DISCORD_TOKEN: SecretStr = Field(
        default=...,
        description="Discord bot token"
    )
    DISCORD_CHANNEL_ID: int = Field(
        default=1539128341301301320,
        description="Default Discord channel ID"
    )
    
    @field_validator("DISCORD_CHANNEL_ID")
    @classmethod
    def validate_channel_id(cls, v):
        if v < 0:
            raise ValueError("Channel ID must be positive")
        return v


class PlaidSettings(BaseSettings):
    """Plaid API configuration."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    PLAID_CLIENT_ID: Optional[str] = Field(
        default=None,
        description="Plaid client ID"
    )
    PLAID_SECRET: Optional[SecretStr] = Field(
        default=None,
        description="Plaid secret key"
    )
    PLAID_ENV: str = Field(
        default="production",
        pattern="^(sandbox|development|production)$",
        description="Plaid environment"
    )
    PLAID_ACCESS_TOKENS: List[str] = Field(
        default=[],
        description="Plaid access tokens"
    )
    
    LOW_BALANCE_THRESHOLD: float = Field(
        default=150.00,
        ge=0,
        description="Low balance alert threshold"
    )
    PLAID_POLL_INTERVAL_SECONDS: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Plaid sync poll interval"
    )
    
    @field_validator("PLAID_ACCESS_TOKENS", mode="before")
    @classmethod
    def parse_tokens(cls, v):
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return list(v)


class SearchSettings(BaseSettings):
    """Search service configuration."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    SEARXNG_URL: str = Field(
        default="http://localhost:8080/search",
        description="SearxNG search URL"
    )
    OLLAMA_URL: str = Field(
        default="http://localhost:11434/api/chat",
        description="Ollama API URL"
    )
    ADVISOR_MODEL: str = Field(
        default="gemma4:26b",
        description="Advisor LLM model"
    )
    
    SEARCH_CONCURRENCY: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Concurrent search operations"
    )
    SEARCH_HTTP_TIMEOUT: float = Field(
        default=45.0,
        ge=5.0,
        le=120.0,
        description="HTTP timeout for search requests"
    )
    SEARCH_CACHE_TTL_SECONDS: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Search cache TTL"
    )
    
    # SSRF Protection for search
    MAX_RESEARCH_CRAWL_DEPTH: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Maximum crawl depth for research"
    )
    MAX_RESEARCH_LINKS_PER_PAGE: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum links to extract per page"
    )


class SandboxSettings(BaseSettings):
    """Sandbox execution environment configuration."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    SANDBOX_URL: str = Field(
        default="http://localhost:8001",
        description="Sandbox service URL"
    )
    SANDBOX_TIMEOUT_SECONDS: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Sandbox execution timeout"
    )
    SANDBOX_MAX_CODE_LENGTH: int = Field(
        default=50000,
        ge=1000,
        le=1000000,
        description="Maximum code length for sandbox execution"
    )
    SANDBOX_ALLOWED_MODULES: Set[str] = Field(
        default={
            "json", "math", "random", "datetime", "collections",
            "re", "typing", "dataclasses"
        },
        description="Allowed Python modules in sandbox"
    )
    
    @field_validator("SANDBOX_ALLOWED_MODULES", mode="before")
    @classmethod
    def parse_modules(cls, v):
        if isinstance(v, str):
            return set(m.strip() for m in v.split(",") if m.strip())
        return set(v)


class DelilahSettings(BaseSettings):
    """Main application settings combining all configuration groups."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # Build information
    DELILAH_BUILD: str = Field(
        default="2026-08-24-deterministic-verification-v3",
        description="Build version identifier"
    )
    
    # Timezone
    TIMEZONE: str = Field(
        default="America/New_York",
        description="Application timezone"
    )
    
    # Feature flags
    PLAYWRIGHT_ENABLED: bool = Field(
        default=True,
        description="Enable Playwright for web scraping"
    )
    PLAYWRIGHT_HEADLESS: bool = Field(
        default=True,
        description="Run Playwright in headless mode"
    )
    
    # Advisor limits
    MAX_TOOL_CALLS_PER_ROUND: int = Field(
        default=0,  # 0 = unlimited
        ge=0,
        description="Max tool calls per round (0=unlimited)"
    )
    MAX_TOTAL_TOOL_CALLS: int = Field(
        default=100,
        ge=0,
        description="Max total tool calls (0=unlimited)"
    )
    
    # PDF processing
    PDF_MAX_PAGES: int = Field(
        default=6,
        ge=1,
        le=20,
        description="Maximum PDF pages to process"
    )
    PDF_RENDER_SCALE: float = Field(
        default=2.0,
        ge=1.0,
        le=4.0,
        description="PDF render scale factor"
    )
    
    # Image processing
    IMAGE_MAX_DIMENSION: int = Field(
        default=1568,
        ge=512,
        le=4096,
        description="Maximum image dimension"
    )
    
    @field_validator("TIMEZONE")
    @classmethod
    def validate_timezone(cls, v):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(f"Invalid timezone: {v}")
        return v
    
    @field_validator("PLAYWRIGHT_ENABLED", "PLAYWRIGHT_HEADLESS", mode="before")
    @classmethod
    def parse_bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() not in {"0", "false", "no", "off"}
        return bool(v)


@lru_cache(maxsize=1)
def get_security_settings() -> SecuritySettings:
    """Get cached security settings."""
    try:
        return SecuritySettings()
    except ValidationError as e:
        # Generate a secure random key if not provided
        if "SECRET_KEY" in str(e):
            import secrets
            os.environ["SECRET_KEY"] = secrets.token_hex(32)
            return SecuritySettings()
        raise


@lru_cache(maxsize=1)
def get_database_settings() -> DatabaseSettings:
    """Get cached database settings."""
    return DatabaseSettings()


@lru_cache(maxsize=1)
def get_discord_settings() -> DiscordSettings:
    """Get cached discord settings."""
    return DiscordSettings()


@lru_cache(maxsize=1)
def get_plaid_settings() -> PlaidSettings:
    """Get cached plaid settings."""
    return PlaidSettings()


@lru_cache(maxsize=1)
def get_search_settings() -> SearchSettings:
    """Get cached search settings."""
    return SearchSettings()


@lru_cache(maxsize=1)
def get_sandbox_settings() -> SandboxSettings:
    """Get cached sandbox settings."""
    return SandboxSettings()


@lru_cache(maxsize=1)
def get_settings() -> DelilahSettings:
    """Get cached main settings."""
    return DelilahSettings()


def get_runtime_source_hash() -> str:
    """Get SHA256 hash of the current source file for runtime verification."""
    try:
        source_file = Path(__file__).resolve()
        return hashlib.sha256(source_file.read_bytes()).hexdigest()
    except Exception:
        return "<unavailable>"


# Export all settings for easy import
__all__ = [
    "SecuritySettings",
    "DatabaseSettings",
    "DiscordSettings",
    "PlaidSettings",
    "SearchSettings",
    "SandboxSettings",
    "DelilahSettings",
    "get_security_settings",
    "get_database_settings",
    "get_discord_settings",
    "get_plaid_settings",
    "get_search_settings",
    "get_sandbox_settings",
    "get_settings",
    "get_runtime_source_hash",
]
