"""Infrastructure warmers (async)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .kafka import KafkaProducerWarmer
    from .postgres import PostgresWarmer
    from .redis import RedisWarmer
else:
    # Optional kafka
    try:
        from .kafka import KafkaProducerWarmer
    except ImportError:
        KafkaProducerWarmer = None

    # Optional postgres
    try:
        from .postgres import PostgresWarmer
    except ImportError:
        PostgresWarmer = None

    # Optional redis
    try:
        from .redis import RedisWarmer
    except ImportError:
        RedisWarmer = None

__all__ = [
    "KafkaProducerWarmer",
    "PostgresWarmer",
    "RedisWarmer",
]
