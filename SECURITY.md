# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| the latest release | ✅ fixes ship as the next patch or minor |
| older | ❌ upgrade to the latest release first |

## Known advisories in dependencies

| Advisory | Where | Status |
|---|---|---|
| APScheduler 4 `JSONSerializer` / `CBORSerializer` deserialize job data without validating it | the `apscheduler4` extra, every published 4.x alpha | not reachable from `servicewright` |

There is no patched APScheduler 4 to upgrade to — `4.0.0a6` is both the newest release and a
vulnerable one. The adapter does not reach the affected code: it builds `AsyncScheduler()` with
no arguments, so the job store is the in-memory one that serializes nothing, and neither
`ScheduledJob` nor the entrypoint accepts a data store or a serializer. A caller who builds
their own scheduler with a persistent job store owns that risk; see
[the scheduler adapter](https://bedrock-python.github.io/servicewright/adapters/scheduler/).

## Reporting a vulnerability

**Please do not report security vulnerabilities via public GitHub Issues.**

Report it privately through GitHub, by
[opening a draft security advisory](https://github.com/bedrock-python/servicewright/security/advisories/new),
or send an email to **shalaevad.alexey@gmail.com**. Either way, include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact and affected versions

We aim to acknowledge reports within **48 hours** and provide a fix within **7 days**
for critical issues.

Once the fix is released, we will credit you in the release notes unless you prefer
to remain anonymous.
