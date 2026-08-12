"""Core exceptions for the microservice runtime."""

from __future__ import annotations


class ServiceWrightError(Exception):
    """Base class for all servicewright runtime errors."""


class CleanupTimeoutError(ServiceWrightError, TimeoutError):
    """Raised when graceful cleanup does not finish within the allotted timeout."""


class WarmupTimeoutError(ServiceWrightError, TimeoutError):
    """Raised when infrastructure warmup does not finish within the allotted timeout."""


class DrainTimeoutError(ServiceWrightError, TimeoutError):
    """Raised when an entrypoint does not drain in-flight work within the grace window."""


class WarmupError(ServiceWrightError):
    """Base exception for infrastructure warmup errors."""


class RedisWarmupError(WarmupError):
    """Raised when Redis warmup fails."""


class KafkaProducerWarmupError(WarmupError):
    """Raised when Kafka producer warmup fails."""


class PostgresWarmupError(WarmupError):
    """Raised when Postgres warmup fails."""
