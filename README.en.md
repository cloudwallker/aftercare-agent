# Aftercare Agent

[简体中文](README.md) | English

A durable after-sales agent with human approval, crash recovery, and idempotent simulated refunds. Built with Python 3.12, FastAPI, PostgreSQL, SQLAlchemy, and httpx.

## What it does

- Investigates orders using three read-only tools and proposes a refund.
- Binds human approval to an immutable, versioned proposal.
- Runs work in a separate worker with durable checkpoints, leases, and bounded retries.
- Reconciles uncertain merchant responses using the original idempotency key.
- Streams ordered task events over SSE with reconnection support.
- Runs offline with MockProvider or connects to a Chat Completions compatible HTTP model.

**v0.1.0 is a local simulation MVP.** It does not process real payments. The full suite passed 236 tests; live external-model calls have not been verified. Mock evaluation scores describe a fixed protocol baseline, not model quality.

## Quick start

Requirements: Docker Compose v2 with `!override` support, Python 3.12 and uv for host scripts.

```powershell
Copy-Item .env.example .env
docker compose run --rm --build migrate
docker compose run --rm --build seed
docker compose up -d --build --wait api worker
```

Only copy the example environment on first setup; preserve existing configuration. API documentation: http://127.0.0.1:8000/docs. Use the customer or reviewer Bearer token from your local environment. Merchant and PostgreSQL ports are not exposed to the host.

The example credentials are deliberately public, local-only demo values. Replace them and configure database roles accordingly before any deployment outside a trusted local machine. Do not commit local environment files or real model credentials.

## Repeatable demonstrations

```powershell
uv run python scripts/demo.py --scenario happy-path
uv run python scripts/demo.py --scenario lost-response
uv run python scripts/demo.py --scenario worker-restart
uv run python scripts/evaluate.py --mode mock
```

Demonstrations use isolated test projects and unique simulated orders. Services are stopped afterward; volumes and demo history are retained. Do not run two demonstrations against the same project concurrently.

To inspect an existing refund without changing it:

```powershell
uv run python scripts/demo.py --scenario inspect-refund --task-id <task-uuid>
```

This queries the original merchant key without issuing a refund or modifying local state.

## Optional HTTP model

Configure `MODEL_BASE_URL`, `MODEL_API_KEY`, and `MODEL_NAME` in your environment. The base URL must include the service's required prefix, such as `/v1`. Set `MODEL_MODE=http` for the worker, then recreate its container. For isolated evaluation, run `uv run python scripts/evaluate.py --mode http`; this may incur provider charges.

The adapter disables parallel tool calls and reconstructs assistant/tool message pairs from durable history. Missing configuration fails explicitly; there is no silent fallback to Mock. Credentials and raw model error responses are not stored in task events.

## Tests

```powershell
uv sync --locked
uv run ruff check .
uv run python scripts/check_release.py
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml run --rm --build tests
docker compose -p aftercare-test -f compose.yaml -f compose.test.yaml down
```

The integration suite requires real PostgreSQL; it does not substitute SQLite. See [test results](docs/test-results.md), [acceptance mapping](docs/acceptance.md), and [release notes](CHANGELOG.md).

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/aftercare/` | API, agent, task runtime, approvals, and refund executor |
| `src/mock_merchant/` | Isolated simulated merchant and idempotent ledger |
| `migrations/`, `docker/` | Database migrations and local role initialization |
| `scripts/` | Demonstrations, evaluation, and release checks |
| `tests/` | Unit, PostgreSQL integration, and TCP/process tests |
| `docs/` | Design, runtime contracts, acceptance evidence, and reports |

## Boundaries and license

No frontend, real payment integration, production-scale validation, or automatic manual-review resolution is included. Safety of repeated external calls depends on the merchant idempotency protocol; this is not a global exactly-once guarantee.

MIT licensed; see [LICENSE](LICENSE). Dependencies retain their own licenses. Architecture inspirations are described in [references](docs/references.md); their implementation code was not copied.
