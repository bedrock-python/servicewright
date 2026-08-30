# Production checklist

Go through this once per service. Most of it is a single line of configuration, and each item maps
to a failure someone has already had.

## Lifecycle

- [ ] `drain_grace_seconds` is at least as long as your slowest normal request or job.
- [ ] `terminationGracePeriodSeconds` > `drain_grace_seconds + cleanup_timeout_seconds + slack`.
- [ ] `cleanup_timeout_seconds` leaves room for the telemetry flush (Sentry and OTLP exports go
      over the network).
- [ ] Every entrypoint's `essential` flag matches what you want: a failure that must kill the
      process is `True`; a nice-to-have sidecar is `False`.
- [ ] Long loops wait on the `stop` event instead of sleeping blindly.
- [ ] `pre_shutdown` hooks flush anything that must not be lost — the app scope is still open there.

## Probes

- [ ] Liveness points at `livez`, **not** `readyz`. A database blip must not restart the fleet.
- [ ] Readiness has a short period (`5s`) and a low `failureThreshold` (`2`).
- [ ] Liveness has a much looser `failureThreshold` than readiness.
- [ ] A socket-less worker has *some* way to be observed: the standalone metrics server, or an
      HTTP entrypoint dedicated to probes.
- [ ] gRPC services use the `grpc:` probe — the health service is registered for you.

## Dependencies

- [ ] Every hard dependency has a **warmer** (fails startup) *and* a **health check** (fails
      readiness).
- [ ] Warmer timeouts are well under the 60-second warmup ceiling.
- [ ] Optional dependencies use `raise_on_failure=False` so they cannot block a deploy.
- [ ] Health checks are cheap, or `HealthRegistry(readiness_cache_ttl=...)` is set.
- [ ] Checks that need container-resolved objects are registered in a `pre_start` hook.

## Errors

- [ ] Every domain error subclasses `ServiceError` with an explicit `kind`.
- [ ] Anything containing internal detail — SQL, upstream payloads, stack context — is
      `public=False`.
- [ ] The error codes clients depend on are treated as API surface and covered by a test.
- [ ] If you ship a custom renderer, it never raises: non-primitive `params` go through
      `to_json_safe`.
- [ ] A masked 500 still carries the request id (it does by default — do not move the
      unhandled-error layer).

## Observability

- [ ] `ObsConfig` selects exactly the backends whose extras are installed in the image.
- [ ] `KeyRedactor` is configured, with your own sensitive key names added.
- [ ] Log level is `INFO` in production, and `use_json=True`.
- [ ] Tracing `sample_ratio` is sane for your traffic (`1.0` is a bill, not a setting).
- [ ] Sentry `environment` and `release` are set — the release comes from `get_app_version()`.
- [ ] Metrics are actually scraped: either `/system/metrics` on the API port, or the standalone
      server, and the scrape config matches.
- [ ] Dashboards use the frozen metric names (`grpc_requests_total`, `scheduler_job_runs_total`, HTTP
      instrumentator metrics),
      not names you invented in a recorder and might rename.

## Security

- [ ] gRPC `enable_reflection` is **off** in production, or gated on the environment.
- [ ] gRPC `enable_channelz` is **off** — it exposes peer addresses and per-socket counters on the
      production port.
- [ ] CORS lists explicit origins. `allow_credentials=True` with `allow_origins=["*"]` is rejected
      at construction, but a bare `["*"]` is still worth a second look.
- [ ] Secrets reach the process through settings, not through code — sink setup happens before the
      DI container exists.
- [ ] `max_receive_message_length` and request size limits are set for public endpoints.
- [ ] The image does not install extras you do not use (`[all]` is a development convenience).

## Scheduling

- [ ] The scheduler runs at `replicas: 1`, or every job takes a distributed lock.
- [ ] Every job sets `max_instances` — usually `1`.
- [ ] `misfire_grace_time` reflects what a missed run actually means for that job.
- [ ] Long jobs fit inside `drain_grace_seconds`, or checkpoint so a retry can resume.
- [ ] Jobs are idempotent. They will run twice eventually.

## Batch jobs

- [ ] The job's spec skips warmers it does not need.
- [ ] `backoffLimit` and `ttlSecondsAfterFinished` are set.
- [ ] Long-running jobs checkpoint, so eviction costs one batch and not the whole run.
- [ ] The job exits non-zero on failure — verify it, do not assume it.

## Testing

- [ ] Use cases are tested without servicewright at all.
- [ ] At least one test builds the real app (`build_app()`) and hits a route.
- [ ] Readiness is tested in both states: 503 before ready, 200 after.
- [ ] Lifecycle ordering is asserted somewhere (`FakeEntrypoint.events`).
- [ ] Integration tests bind `port=0` and read `bound_port` — no hardcoded ports.
- [ ] Migrations have their own tests.

## Deployment

- [ ] `maxUnavailable: 0`, `maxSurge: 1` for the rolling update.
- [ ] Resource requests and limits are set; the process is single-threaded async, so CPU limits
      below `1` will show up as latency.
- [ ] The image runs as a non-root user.
- [ ] API and worker are separate deployments built from the same spec, if they scale differently.
- [ ] Logs go to stdout as JSON and the collector parses them.

## Before the first real traffic

- [ ] Send a `SIGTERM` to a running pod and watch the order: `readyz` → 503, then drain, then exit
      `0`.
- [ ] Kill a dependency and confirm readiness goes red **without** the pod restarting.
- [ ] Break the database DSN and confirm the pod refuses to start rather than serving 500s.
- [ ] Trigger a domain error and check the response body, the status and the logged request id.
- [ ] Grep one `request_id` end to end across services.

## Next

- [Runbooks](runbooks.md) — for when one of these bites anyway.
- [Kubernetes](kubernetes.md) — the manifests these items refer to.
