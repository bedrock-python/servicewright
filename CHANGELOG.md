# Changelog

## [0.3.0](https://github.com/bedrock-python/servicewright/compare/servicewright-v0.2.0...servicewright-v0.3.0) (2026-08-28)


### Features

* let the framework's DI integration own the HTTP request scope ([#5](https://github.com/bedrock-python/servicewright/issues/5)) ([2674140](https://github.com/bedrock-python/servicewright/commit/2674140080b31538ccdf8d3b8aeee2b8455d273f)), closes [#4](https://github.com/bedrock-python/servicewright/issues/4)

## [0.2.0](https://github.com/bedrock-python/servicewright/compare/servicewright-v0.1.0...servicewright-v0.2.0) (2026-08-14)


### Features

* add Masker seam with ValueRedactor, ChainRedactor, per-surface redactors and span-attribute redaction ([47e40e8](https://github.com/bedrock-python/servicewright/commit/47e40e8699ce64cc2565589e9cf82d55efad065b))


### Documentation

* point the README API reference link at its new url ([08748d5](https://github.com/bedrock-python/servicewright/commit/08748d55b607ce6adfc98458fc33a55755cde576))
* rewrite the documentation site with concepts, adapters, blueprints and runbooks ([db5508c](https://github.com/bedrock-python/servicewright/commit/db5508c467fa2858efde0210d7169ddb57075589))

## 0.1.0 (2026-08-12)

Initial release of servicewright.

### Features

* `Host` + `Entrypoint` model — one `AppSpec` runs any number of transports (HTTP, gRPC, scheduler, daemon, one-shot) under a single lifecycle
* Kubernetes-correct shutdown — readiness flips to false before draining, each entrypoint finishes in-flight work inside a grace window, the DI scope closes last
* Failure is visible — an essential entrypoint that dies mid-serve propagates out of `run()`, so the process exit code distinguishes a crash from a graceful stop
* Stop-aware startup — a signal during warmup or bind abandons startup instead of binding ports and reporting ready on a terminating process
* Signal escalation — the first SIGINT/SIGTERM drains gracefully, a second exits immediately with `128 + signum`
* Transport-neutral error taxonomy — one `ServiceError` renders as an RFC 9457 problem document over HTTP and as the mapped `grpc.StatusCode` over gRPC, with one masking rule and a pluggable renderer
* Transport-neutral correlation store — one request id in the logs, in outbound propagation and in the response, bridged into structlog contextvars and OTel Baggage
* DI-agnostic two-tier scopes — `AppScope` for process-lifetime singletons, a fresh `UnitScope` per request/RPC/job; dishka adapter included
* Pluggable observability — metrics, tracing, logging and error tracking as kernel protocols with prometheus, OpenTelemetry, Sentry, structlog and stdlib backends, plus `register_sink` for your own
* Priority-grouped, fail-fast warmup and a health registry driving both the HTTP probes and the gRPC health service
* Zero hard dependencies — `pip install servicewright` pulls nothing; every framework binding lives behind an extra
