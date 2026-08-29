# API reference: adapters

Every adapter lives behind an [extra](../getting-started/installation.md#extras). Importing one
without its extra raises an `ImportError` naming what to install.

## `adapters.builtin`

Zero-dependency entrypoints, also re-exported from the top-level package.

::: servicewright.adapters.builtin
    options:
      heading_level: 3

## `adapters.fastapi`

::: servicewright.adapters.fastapi
    options:
      heading_level: 3

## `adapters.litestar`

::: servicewright.adapters.litestar
    options:
      heading_level: 3

## `adapters.grpc`

::: servicewright.adapters.grpc
    options:
      heading_level: 3

## `adapters.apscheduler4`

::: servicewright.adapters.apscheduler4
    options:
      heading_level: 3

## `adapters.apscheduler3`

The public surface is identical to `apscheduler4`; only `ScheduledJob.coalesce` differs
(`bool` instead of `CoalescePolicy`).

::: servicewright.adapters.apscheduler3
    options:
      heading_level: 3

## `adapters.dishka`

::: servicewright.adapters.dishka
    options:
      heading_level: 3

## `adapters.observability`

The author-facing sink ABCs and the five built-in backends — `PrometheusMetricsSink`, `OtelTracingSink`,
`SentryErrorTrackingSink`, `StructlogLoggingSink`, `StdlibLoggingSink` — importable from here (each needs
its extra). Selected by name, they are resolved lazily through the registry.

::: servicewright.adapters.observability
    options:
      heading_level: 3

## `adapters.warmers`

::: servicewright.adapters.warmers.postgres.PostgresWarmer
    options:
      heading_level: 3

::: servicewright.adapters.warmers.redis.RedisWarmer
    options:
      heading_level: 3

::: servicewright.adapters.warmers.kafka.KafkaProducerWarmer
    options:
      heading_level: 3

## `adapters.health`

::: servicewright.adapters.health.postgres.PostgresHealthCheck
    options:
      heading_level: 3

::: servicewright.adapters.health.redis.RedisHealthCheck
    options:
      heading_level: 3
