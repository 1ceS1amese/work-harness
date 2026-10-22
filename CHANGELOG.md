# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Coordinator-controlled LangGraph Worker and Review subgraphs with private per-invocation
  checkpoints, validated task handoffs, and public Agent interaction records in run details.
- Versioned upstream artifact delivery, targeted repair feedback, transitive downstream
  invalidation, cumulative Agent budgets, and model/tool step limits.
- Workflow revision and effective Prompt content in distributed execution fingerprints, plus
  explicit rejection of incompatible checkpoint resumes.
- A safe first-run `setup` profile and API readiness gate that reject task creation until every
  logical work stage has an available real-model route.
- A dedicated `/settings` route with separate model, Agent prompt, and safe runtime-policy
  sections, plus durable prompt/runtime overrides stored outside the release bundle.
- A native Windows desktop shell backed by WebView2, with a windowed single-file executable that
  starts and stops the local API without opening a console or external browser.
- Bounded run admission with configurable waiting capacity, HTTP `429`/`Retry-After` overload
  responses, capacity-aware crash recovery, and scheduler counters in health output.
- A PostgreSQL `ReliableRunStore` with atomic Run/Job submission, globally bounded admission,
  `SKIP LOCKED` claims, renewable leases and fencing tokens, Graph revision routing, bounded retry,
  dead-letter listing/replay, cancellation, and worker heartbeats.
- Separate `api`, `worker`, and one-shot `migrate` roles with PostgreSQL LangGraph checkpoints,
  cross-instance LISTEN/NOTIFY SSE wakeups, readiness based on live compatible Workers, and a real
  PostgreSQL concurrency/migration/failure integration suite.
- A distributed Compose override, safe configuration templates, Worker scaling/status/logging,
  PostgreSQL backup commands, and an operational design contract.

### Changed

- Mock Providers now require the explicit `local` development profile instead of being enabled by
  default in source, Compose, Docker, or the Windows executable.
- Provider settings now use user-facing Chinese names, hide YAML/storage and adapter identifiers,
  guide first-run configuration, and assign one selected model across all work stages by default.
- The settings route is loaded as a separate frontend chunk and replaces the previous model-only
  drawer; saved settings are included in Compose backups and require an explicit restart.
- Windows packaging now verifies the PE GUI subsystem and reports startup failures through a
  native dialog while writing diagnostics to the application data directory.
- Idempotent retries continue to resolve to their original run while the admission queue is full,
  and settings files from before the queue fields remain loadable through additive defaults.
- Distributed runs now persist validated input before returning `202`, resume from shared
  checkpoints after lease reclaim, reject stale acknowledgements, and stop crash loops at the
  configured attempt limit.
- Provider, prompt, and runtime mutations through the UI are disabled in distributed mode until
  those configuration stores have a shared consistency contract.
- Distributed execution fingerprints now canonicalize unordered configuration sets, so API and
  Worker processes remain revision-compatible across independent Python hash seeds; the Compose
  helper also detects Docker Desktop's WSL socket when the standard socket is not mapped.

### Fixed

- Dependent Workers now receive upstream artifacts, repairs receive prior output and review
  feedback, and stale successful artifacts cannot satisfy a new repair round. Unchanged artifacts
  retain their existing reviews; partial Worker results terminate instead of looping indefinitely.
- Embedded runs now report `running` immediately after an approved human-gate resume instead of
  briefly regressing to the stale `waiting_human` checkpoint; authenticated SSE clients fall back
  to polling when a proxy closes a stream cleanly.
- OpenAI-compatible Chat Completions and Responses endpoints now append their protocol path before
  an existing query string, supporting custom gateways that require URL query parameters.
- Scheduler-owned artifact and review routing fields are no longer trusted from model output, and
  the artifact schema now honors the full configurable 200,000-character runtime limit.
- Settings updates merge atomically against the latest persisted document, and browser conversation
  recovery validates corrupt history, migrates legacy Agent assignments, and keeps the 30-item cap.

### Planned

- Real-provider contract validation with manually supplied credentials.
- Target-environment PostgreSQL load testing and production capacity calibration.
- Server-side conversations, tenant isolation, and organization identity integration.
- Shared editable Provider/settings persistence and a durable event replay log.

## 0.2.2 - 2026-08-19

### Added

- Dual Provider credential channels: environment injection for automated deployments and
  authenticated UI entry with Fernet-encrypted SQLite persistence for interactive installs.
- Separate liveness and Provider readiness endpoints, production Compose initialization and
  validation commands, resource limits, hardened Caddy headers, and credential-key-aware backups.

### Changed

- The workbench now uses a low-saturation gray/white square visual system and opens Provider
  settings when a production installation is waiting for model configuration.
- OpenAI-compatible Providers may start without a credential so administrators can complete the
  first-run setup through the authenticated UI; model calls remain unavailable until readiness.

## 0.2.1 - 2026-08-19

### Added

- Run-level and per-Provider concurrency backpressure, with scheduler statistics in health output.
- Run-scoped caching for idempotent Tools and cache-hit visibility in Tool call records.
- ASGI-native request-size and per-client rate guards, deterministic Trace sampling, and a
  repeatable backend benchmark reporting throughput, percentiles, model calls, and RSS.

### Changed

- The browser workbench now uses a QQ/WeChat-style group-chat layout with conversation and member
  sidebars, left/right avatar bubbles, and conversational Chinese labels for agent functions,
  workflow stages, skills, and model routing.
- Ordinary low- and medium-risk runs now use one comprehensive review per artifact by default,
  while high/critical-risk and research/decision runs retain independent fact, logic, and
  constraint reviews.
- SSE snapshots are now driven by LangGraph node updates instead of polling complete checkpoints
  every 250 milliseconds.
- LangGraph `Send` payloads contain only node-local inputs, reducing repeated state fan-out.

## 0.2.0 - 2026-08-19

### Added

- A Windows x64 single-file executable containing the API, browser workbench, runtime
  configuration, prompts, schemas, evaluation cases, and built-in Skills.
- A desktop launcher that opens the local workbench and stores checkpoints, runs, logs, and
  Provider Profiles outside the temporary executable bundle.
- Reproducible PyInstaller packaging, a PowerShell build command, SHA-256 output, artifact
  attestation, and automatic executable attachment to GitHub Releases.

### Changed

- Frozen builds resolve read-only runtime resources from their bundle while respecting explicit
  project roots and external persistence-path overrides.

## 0.1.0 - 2026-08-18

### Added

- A bounded LangGraph workflow for normalization, planning, parallel work, review,
  repair, human approval, and synthesis.
- Four homogeneous assignable agents with per-task functions and per-agent model routes.
- Persistent run, checkpoint, and Provider Profile stores backed by SQLite.
- OpenAI-compatible and DeepSeek-dialect adapters for Chat Completions and Responses.
- Provider capability parity, Tool adapters, modular Skills, and compatibility reports.
- A Vue group-chat workbench with Provider routing, SSE updates, and deployment API keys.
- Single-instance Docker Compose deployment, optional Caddy HTTPS, health checks, and backups.
