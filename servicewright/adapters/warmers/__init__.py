"""Infrastructure warmers (async).

Every warmer is duck-typed on the client it is handed and soft-imports its SDK,
so importing this package never requires the ``kafka``, ``postgres`` or ``redis``
extra and the three names are always the classes themselves.
"""

from .kafka import KafkaProducerWarmer as KafkaProducerWarmer
from .postgres import PostgresWarmer as PostgresWarmer
from .redis import RedisWarmer as RedisWarmer

__all__ = [
    "KafkaProducerWarmer",
    "PostgresWarmer",
    "RedisWarmer",
]
