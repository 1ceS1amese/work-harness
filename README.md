<a id="readme-top"></a>

<div align="center">
  <h1>Work Harness</h1>
  <p>Multi-agent collaboration around a shared blackboard, built with LangGraph.</p>
  <p>
    <a href="docs/blackboard-design.md"><strong>Explore the architecture »</strong></a>
    <br /><br />
    <a href="#getting-started">Quick Start</a>
    &middot;
    <a href="https://github.com/1ceS1amese/work-harness/issues/new?template=bug.yml">Report Bug</a>
    &middot;
    <a href="https://github.com/1ceS1amese/work-harness/issues/new?template=feature.yml">Request Feature</a>
  </p>
</div>

[![CI][ci-shield]][ci-url]
[![Python][python-shield]][python-url]
[![License: MIT][license-shield]][license-url]
[![Status: Alpha][status-shield]][status-url]

**English** | [简体中文](README.zh-CN.md)

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li><a href="#about-the-project">About the Project</a>
      <ul><li><a href="#built-with">Built With</a></li></ul>
    </li>
    <li><a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#installation">Installation</a></li>
        <li><a href="#other-deployment-options">Other Deployment Options</a></li>
      </ul>
    </li>
    <li><a href="#usage">Usage</a>
      <ul>
        <li><a href="#web-workbench">Web Workbench</a></li>
        <li><a href="#command-line">Command Line</a></li>
        <li><a href="#http-api">HTTP API</a></li>
        <li><a href="#token-accounting">Token Accounting</a></li>
      </ul>
    </li>
    <li><a href="#roadmap">Roadmap and Current Status</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
    <li><a href="#contact">Contact</a></li>
    <li><a href="#acknowledgments">Acknowledgments</a></li>
  </ol>
</details>

## About the Project

Work Harness is a framework and workbench for running, inspecting, and recovering multi-agent tasks.
Each run owns a shared blackboard and a root goal. Knowledge sources read committed state and
propose new information; a LangGraph controller chooses the next eligible sources from the remaining
goal gaps. The execution path grows from what is on the board instead of requiring a complete task
plan up front.

The blackboard contains only three kinds of knowledge objects:

| Object | Meaning | Role in collaboration |
| --- | --- | --- |
| **Fact** | A claim confirmed within its stated scope | Supplies evidence for subsequent work and acceptance checks |
| **Intent** | A goal or direction still to explore | Records acceptance criteria and unresolved work |
| **Hint** | Experience, a hypothesis, or a suggestion | Guides exploration without being treated as confirmed evidence |

Independent knowledge sources may use rules, observations, verification logic, or private LangGraph
Agent subgraphs. They exchange versioned board objects; private model conversations remain inside
those subgraphs. The kernel checks evidence, provenance, conflicts, and object validity before
accepting changes.

Current capabilities include:

- **State-driven collaboration:** bounded parallel activation, per-Intent concurrency, retries,
  timeouts, human input, and explicit completion criteria.
- **Inspectable execution:** goal gaps, object history, evidence, attachments, a knowledge graph,
  live run updates, and durable blackboard event queries.
- **Recovery:** SQLite for one application process, or PostgreSQL with separate API and Worker
  processes, durable jobs, leases, cancellation, and failure recovery.
- **Configurable models:** custom OpenAI-compatible endpoints, Chat Completions and Responses,
  the DeepSeek protocol dialect, per-Agent routing, prompts, Tools, and Skills.
- **Legacy compatibility:** the previous planning, execution, review, and synthesis workflow
  remains available through `engine=legacy`.

New runs default to **`blackboard`**. The built-in product catalog currently explores candidate drafts
and verifies their storage. Domain-specific truth or business actions need matching Tools and
verifiers; model confidence alone does not establish a Fact.

### Built With

| Layer | Technology |
| --- | --- |
| Agent execution | Python, LangGraph |
| API and contracts | FastAPI, Pydantic |
| Workbench | Vue 3, TypeScript, Vite, Element Plus |
| Persistence | SQLite, PostgreSQL, LangGraph checkpoints |
| Desktop packaging | pywebview, PyInstaller, Windows WebView2 |
| Verification | pytest, Vitest, Ruff, GitHub Actions |

See the [blackboard design](docs/blackboard-design.md),
[persistence contract](docs/persistence.md), and [legacy Agent design](docs/multi-agent-design.md)
for implementation details.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Getting Started

### Prerequisites

For the current source version, install Python **3.11+**, [uv](https://docs.astral.sh/uv/),
Node.js **22+**, npm, and Git. Model-backed tasks also need a compatible endpoint, credential, and
model ID supporting chat, structured output, and tool calling.

The default `setup` profile starts in configuration mode until a real model service is configured.
Deterministic Mock Providers are available only through the explicit `local` development profile;
they are fixtures for engineering checks.

### Installation

Clone the project and install the locked dependencies:

```bash
git clone https://github.com/1ceS1amese/work-harness.git
cd work-harness
uv sync --locked --extra dev
npm --prefix frontend ci
npm --prefix frontend run build
uv run work-harness check-config
uv run work-harness serve --host 127.0.0.1 --port 8000
```

Open [the local workbench](http://127.0.0.1:8000/), then:

1. Go to **Settings → Models** and configure your Provider endpoint, credential, and model ID.
   Choose the API family and structured-output mode supported by your service.
2. Assign the model to the default group or individual Agents, then save.
3. Restart the service to apply staged settings. Model readiness and task admission remain
   unavailable until configuration is usable.
4. Create a conversation, enter a goal, and explicitly select a completion criterion.

Embedded settings and run data live under `var/` by default. UI-managed credentials are encrypted
in the Provider store; its separate key file is needed for restoration. The
[environment example](.env.example) documents environment-based setup. Source CLI commands do not
automatically load `.env`; export the variables into the process when using that configuration path.

### Other Deployment Options

**Docker Compose — one application process:**

```bash
./scripts/compose.sh up
# Configure the model in the workbench, then apply the saved settings.
./scripts/compose.sh reload
```

**PostgreSQL — separate API and Workers:**

```bash
./scripts/compose.sh init-distributed
# Fill in .env.distributed and deploy/runtime.distributed.env before starting.
./scripts/compose.sh distributed
./scripts/compose.sh scale-workers 3
```

Distributed instances require matching configuration. Provider, prompt, and runtime edits through
the settings UI are disabled in this mode. SQLite is limited to one application process; use
PostgreSQL for multiple execution Workers. See the [deployment guide](docs/docker-deployment.md)
for requirements, HTTPS, backups, restoration, and rollback.

**Windows desktop and Python wheel:** packaging workflows are included. Consult the
[release page](https://github.com/1ceS1amese/work-harness/releases) for version-specific artifacts.
The desktop application targets Windows 10/11 x64 with WebView2; the wheel contains the
backend and bundled configuration, without the workbench frontend. Follow the
[Windows guide](docs/windows-executable.md) and the notes for the selected release. Current source
changes do not imply that an existing EXE or image contains them.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Usage

### Web Workbench

Choose a goal, completion criteria, and any relevant Skills or Agent assignments. Open the run
details to inspect remaining goal gaps, confirmed facts, exploration directions, hints, and their
relationships. Human input is attached to the relevant Intent and board version.

The built-in **saved candidate draft** criterion verifies that content was saved and can be read.
It does **not** certify factual accuracy, answer quality, or completion of an external action.
A free-text goal is never silently converted into that weaker acceptance condition.

Settings are available at `/settings?section=models`, `/settings?section=prompts`, and
`/settings?section=runtime`. Some prompt and review controls apply to the legacy workflow; blackboard
Agents retain their own private execution logic.

### Command Line

With a model configured:

```bash
# Default blackboard engine; explicitly accept a stored candidate draft.
uv run work-harness run \
  --goal "Draft a project introduction" --acceptance-template saved-draft

# Explicitly select the legacy planning/review workflow.
uv run work-harness run --engine legacy --goal "Review this delivery plan"

# Inspect configuration and extension contracts.
uv run work-harness check-compatibility
uv run work-harness skills list
uv run work-harness skills validate
```

For custom acceptance, use `--blackboard-spec goal.json`; the file must contain `topic` and typed
`acceptance` criteria supported by the registered knowledge sources. See the
[task contract](src/work_harness/domain/models.py) and [blackboard design](docs/blackboard-design.md).
CLI task execution uses embedded mode; submit distributed runs through the API.

For an offline smoke run with deterministic fixtures, use this POSIX-shell command:

```bash
WORK_HARNESS_PROFILE=local uv run work-harness run \
  --goal "Exercise the blackboard contract" --acceptance-template saved-draft
```

Mock completion verifies the execution contract, not real-model quality. A dedicated development
checkout avoids mixing fixture runs with existing Provider overrides or application data.

### HTTP API

Submit a run with explicit acceptance criteria:

```bash
curl --request POST http://127.0.0.1:8000/v1/runs \
  --header 'Content-Type: application/json' \
  --data '{
    "goal": "Draft a project introduction",
    "blackboard": {
      "topic": "document",
      "acceptance": [{
        "id": "saved-draft",
        "description": "Save a readable candidate draft; accuracy is not verified",
        "claim_key": "draft.content",
        "claim_type": "delivery",
        "scope": {"verification": "stored_content_only"},
        "required_checks": ["content_stored"]
      }]
    }
  }'
```

The response contains a `run_id`. The example uses the default local configuration; authenticated
deployments also require the deployment API key.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health/live` · `/health/ready` | Process liveness and deployment readiness |
| `GET` | `/v1/capabilities` | Available Agents, Tools, Skills, engine defaults, and acceptance templates |
| `POST` | `/v1/runs` | Submit a run; supports an idempotency key |
| `GET` | `/v1/runs/{run_id}` | Status, output, and recorded usage |
| `GET` | `/v1/runs/{run_id}/events` | Run updates over SSE |
| `GET` | `/v1/runs/{run_id}/blackboard` | Board summary and goal gaps |
| `GET` | `/v1/runs/{run_id}/blackboard/objects` | Objects at a selected board revision |
| `GET` | `/v1/runs/{run_id}/blackboard/events` | Durable blackboard event history |
| `POST` | `/v1/runs/{run_id}/inputs` | Versioned human input for a blackboard Intent |
| `DELETE` | `/v1/runs/{run_id}` | Cancel a run |

Interactive API documentation is available at `/docs` when enabled by the selected profile.

### Token Accounting

Input, output, and cached-input tokens are recorded separately. Cached tokens are already part of
input tokens, so the total is **input + output**. Missing or incomplete usage stays explicitly
unconfirmed; duplicate receipts and recovery do not count the same call twice.

There is no cumulative Token or monetary allowance blocking execution. `max_output_tokens` controls
only the length of one model response. Timeouts, execution steps, activation counts, retries, and
concurrency limits still bound the work. See [Agent configuration](config/agents.yaml) and
[runtime configuration](config/runtime.yaml).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Roadmap

**Current source status — 2026-10-22:** the blackboard core, bounded parallel controller, web/API/CLI
entry points, knowledge graph, human input, SQLite/PostgreSQL recovery, backup/rollback paths, and
default-engine switch are implemented. Token accounting has been separated from spending limits.
The detailed implementation record remains in the [existing design plan](docs/blackboard-design.md).

Source validation covers SQLite and real PostgreSQL 16 API/Worker execution. Backend regression and
follow-up checks cover accounting, failures, recovery, and compatibility; the latest recorded frontend
suite has **57 passing tests**, with type checking and a production build completed. A historical
six-task comparison with `gpt-5.6-sol` passed **6/6** tasks on blackboard and **1/6** on legacy. That is a
limited benchmark, not a general quality claim, and it predates the latest Token refactor. See the
[development record](docs/development-status.md) for dates, test runs, and evaluation scope.

Remaining work includes:

- Rebuilding and validating Windows release artifacts and repeating container restore checks for
  the current source revision.
- Broader real-task evaluation, business Tools, domain verifiers, and reusable experience across runs.
- Server-side conversation synchronization, user identity, and tenant isolation.
- Shared editable Provider and application settings for distributed deployments.

Existing runs retain their engine and execution identity. Resuming a historical run requires the
matching execution version and configuration. To stop admitting new blackboard runs, set
`WORK_HARNESS_WORKFLOW_ENGINE=legacy` and `WORK_HARNESS_ACCEPT_NEW_BLACKBOARD_RUNS=false`, then restart
all application instances consistently. This does not migrate old checkpoints.

The deployment API key is a shared service credential, not a user or tenant boundary. See
[SECURITY.md](SECURITY.md) and the [distributed queue contract](docs/distributed-run-queue.md) before
expanding a deployment. Proposed features and known issues belong in
[GitHub Issues](https://github.com/1ceS1amese/work-harness/issues).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Open an issue for a
substantial change, create a focused branch or fork, and submit a pull request with the behavior
change and relevant validation. Keep English and Chinese README content aligned.

Common development checks:

```bash
uv run ruff check src tests scripts
uv run pytest
npm --prefix frontend run type-check
npm --prefix frontend test
npm --prefix frontend run build
```

PostgreSQL tests require `WORK_HARNESS_TEST_DATABASE_URL` pointing to a dedicated test database;
they skip when it is absent. The [contribution guide](CONTRIBUTING.md) covers database, packaging,
and deployment checks. Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## License

Distributed under the [MIT License](LICENSE). Copyright © 2026 Work Harness contributors.

## Contact

- Project: [1ceS1amese/work-harness](https://github.com/1ceS1amese/work-harness)
- Questions and feedback: [issue templates](https://github.com/1ceS1amese/work-harness/issues/new/choose)
- Changes and releases: [CHANGELOG.md](CHANGELOG.md) · [GitHub Releases](https://github.com/1ceS1amese/work-harness/releases)

## Acknowledgments

- [Best-README-Template](https://github.com/othneildrew/Best-README-Template) for the README structure.
- [LangGraph](https://github.com/langchain-ai/langgraph), [FastAPI](https://fastapi.tiangolo.com/), and
  [Pydantic](https://docs.pydantic.dev/) for the execution and API foundations.
- [Vue](https://vuejs.org/), [Element Plus](https://element-plus.org/),
  [pywebview](https://pywebview.flowrl.com/), and [PyInstaller](https://pyinstaller.org/) for the workbench
  and desktop tooling.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

[ci-shield]: https://github.com/1ceS1amese/work-harness/actions/workflows/ci.yml/badge.svg
[ci-url]: https://github.com/1ceS1amese/work-harness/actions/workflows/ci.yml
[python-shield]: https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white
[python-url]: pyproject.toml
[license-shield]: https://img.shields.io/badge/License-MIT-blue?style=flat-square
[license-url]: LICENSE
[status-shield]: https://img.shields.io/badge/Status-Alpha-orange?style=flat-square
[status-url]: docs/development-status.md
