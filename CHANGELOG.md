# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Planned

- Real-provider contract validation with manually supplied credentials.
- A production-grade shared database and task queue for multi-instance deployments.
- Server-side conversations, tenant isolation, and organization identity integration.

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
