# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Planned

- Real-provider contract validation with manually supplied credentials.
- A production-grade shared database and task queue for multi-instance deployments.
- Server-side conversations, tenant isolation, and organization identity integration.

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
