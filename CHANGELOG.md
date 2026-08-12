# Changelog

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
