# Changelog

## [0.8.0](https://github.com/bedrock-python/servicewright/compare/servicewright-v0.7.0...servicewright-v0.8.0) (2026-08-30)


### ⚠ BREAKING CHANGES

* MetricsSink.gauge() is abstract — a third-party metrics backend must implement it (set / inc / dec) or fail at construction.

### Features

* record scheduled-job runs and add a gauge instrument to the metrics seam ([#26](https://github.com/bedrock-python/servicewright/issues/26)) ([92d45cf](https://github.com/bedrock-python/servicewright/commit/92d45cfdbd2501602fb35978d18c42059e5bea83)), closes [#25](https://github.com/bedrock-python/servicewright/issues/25)

## [0.7.0](https://github.com/bedrock-python/servicewright/compare/servicewright-v0.6.0...servicewright-v0.7.0) (2026-08-29)


### Features

* ship the settings contract as pydantic-settings models ([#23](https://github.com/bedrock-python/servicewright/issues/23)) ([55a62db](https://github.com/bedrock-python/servicewright/commit/55a62db8f99de796135407c40dcd1eb21933aead))

## [0.6.0](https://github.com/bedrock-python/servicewright/compare/servicewright-v0.5.0...servicewright-v0.6.0) (2026-08-29)


### Features

* add run_sync() with event loop selection and a uvloop extra ([#20](https://github.com/bedrock-python/servicewright/issues/20)) ([c070211](https://github.com/bedrock-python/servicewright/commit/c070211940d4c0ce057daf3f303233c1b0ca509b)), closes [#19](https://github.com/bedrock-python/servicewright/issues/19)

## [0.5.0](https://github.com/bedrock-python/servicewright/compare/servicewright-v0.4.0...servicewright-v0.5.0) (2026-08-29)


### Features

* **observability:** make the concrete sinks importable from servicewright.adapters.observability ([#17](https://github.com/bedrock-python/servicewright/issues/17)) ([0a04252](https://github.com/bedrock-python/servicewright/commit/0a04252938654f14dbfc451525c460a628845f14)), closes [#16](https://github.com/bedrock-python/servicewright/issues/16)

## [0.4.0](https://github.com/bedrock-python/servicewright/compare/servicewright-v0.3.0...servicewright-v0.4.0) (2026-08-28)


### Features

* **observability:** let KeyRedactor exempt exact field names with safe_keys ([#14](https://github.com/bedrock-python/servicewright/issues/14)) ([a1b7ecf](https://github.com/bedrock-python/servicewright/commit/a1b7ecf9dc059d1eb6da0eb6d747c1755a0959c1)), closes [#11](https://github.com/bedrock-python/servicewright/issues/11)
* **observability:** pass sentry_sdk.init options through SentryErrorTrackingSink ([#12](https://github.com/bedrock-python/servicewright/issues/12)) ([b434a21](https://github.com/bedrock-python/servicewright/commit/b434a212296cfa01a5f62be99e2a92f6355ca647)), closes [#9](https://github.com/bedrock-python/servicewright/issues/9)


### Bug Fixes

* **core:** make the observability section protocols read-only so narrowed settings satisfy them ([#13](https://github.com/bedrock-python/servicewright/issues/13)) ([ebc2c05](https://github.com/bedrock-python/servicewright/commit/ebc2c0528894ea550245ea88ad8726c39b751627)), closes [#8](https://github.com/bedrock-python/servicewright/issues/8)
* **observability:** keep stdlib extra fields in the structlog sink ([#10](https://github.com/bedrock-python/servicewright/issues/10)) ([ae85ffb](https://github.com/bedrock-python/servicewright/commit/ae85ffbea19758c5c6af49484b45d25e8fb52897))

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
