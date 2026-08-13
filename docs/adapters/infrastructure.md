# Infrastructure

Ready-made [warmers](../concepts/warmup.md) and [health checks](../concepts/health.md) for the
usual dependencies. They are all duck-typed on the client you pass in, so wrappers and cluster
clients work as long as the shape matches.

```python
spec.warmers.append(PostgresWarmer(session_manager))
spec.health.add_check("postgres", PostgresHealthCheck(session_maker))
```

## Postgres

```bash
pip install "servicewright[postgres]"
```

```python
from servicewright.adapters.health.postgres import PostgresHealthCheck
from servicewright.adapters.warmers.postgres import PostgresWarmer
```

| | Signature | Expects |
| --- | --- | --- |
| Warmer | `PostgresWarmer(session_manager, query="SELECT 1", timeout=10.0, priority=0, raise_on_failure=True)` | an object with `get_session()` returning an async context manager |
| Check | `PostgresHealthCheck(session_maker, timeout=5.0)` | a callable returning an async context manager, e.g. `async_sessionmaker` |

!!! warning "The two take different objects"

    The warmer calls `session_manager.get_session()` — the shape used by
    `sqlalchemy-postgres-kit`. The health check calls `session_maker()` directly — the shape of a
    plain SQLAlchemy `async_sessionmaker`. Pass each what it asks for.

Both run a trivial query and translate failures: the warmer raises `PostgresWarmupError` (which
aborts startup unless `raise_on_failure=False`), the check returns `False`.

## Redis

```bash
pip install "servicewright[redis]"
```

```python
from servicewright.adapters.health.redis import RedisHealthCheck
from servicewright.adapters.warmers.redis import RedisWarmer
```

| | Signature |
| --- | --- |
| Warmer | `RedisWarmer(redis_client, max_connections=None, timeout=10.0, priority=0, raise_on_failure=True)` |
| Check | `RedisHealthCheck(client, timeout=5.0)` |

The warmer pings once to verify connectivity, then fires `max_connections` concurrent pings
(default 10) to actually fill the pool. Any failed ping raises `RedisWarmupError` — a pool that is
half-broken at startup is not something to discover under traffic.

Both accept anything with an awaitable `ping()`, so `Redis`, `RedisCluster` and your own wrapper
all fit. Cluster clients returning a per-node dict are handled: every node must report healthy.

If [redis-client-kit](https://pypi.org/project/redis-client-kit/) happens to be installed, the
warmer uses its richer health check instead of a bare ping.

## Kafka

```bash
pip install "servicewright[kafka]"
```

```python
from servicewright.adapters.warmers.kafka import KafkaProducerWarmer
```

`KafkaProducerWarmer(producer, timeout=10.0, priority=0, raise_on_failure=True)` fetches cluster
metadata through the producer's client, which is what forces the connection and the metadata cache
to be ready before the first real send. Failures become `KafkaProducerWarmupError`.

There is no bundled Kafka health check — a producer that cannot reach the cluster usually should
not make the pod unready on its own. Register your own check if your service disagrees.

## Import paths

Import from the **submodule**, as shown above. The package-level convenience imports fall back to
`None` when an extra is missing:

```python
from servicewright.adapters.warmers import KafkaProducerWarmer   # None without [kafka]
from servicewright.adapters.warmers.kafka import KafkaProducerWarmer   # ImportError, as it should
```

`servicewright.adapters.health` exports nothing at package level for the same reason — importing
it never requires an extra.

## Writing your own

Neither is worth a base class. A warmer is one method and a priority:

```python
from servicewright import AsyncWarmer


class SearchIndexWarmer(AsyncWarmer):
    def __init__(self, client: SearchClient) -> None:
        super().__init__(raise_on_failure=False)   # nice to have, not required
        self._client = client

    @property
    def priority(self) -> int:
        return 10

    async def warmup(self) -> None:
        await self._client.load_index()
```

A health check is one method and no base class at all:

```python
class SearchIndexCheck:
    def __init__(self, client: SearchClient) -> None:
        self._client = client

    async def check(self) -> bool:
        return await self._client.is_ready()
```

```python
spec.warmers.append(SearchIndexWarmer(client))
spec.health.add_check("search", SearchIndexCheck(client))
```

Give both a timeout of their own. The warmup phase has a 60-second ceiling and readiness probes
have whatever interval Kubernetes was configured with — neither is a good place to hang.
