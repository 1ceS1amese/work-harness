# Contributing to Work Harness

Thank you for helping improve Work Harness. Bug reports, documentation improvements,
tests, Provider adapters, Tools, and focused feature pull requests are welcome.

## Before you start

- Search existing issues and pull requests before opening a duplicate.
- Use an issue for behavior changes or architecture proposals before investing in a large patch.
- Use GitHub Discussions for open-ended questions when Discussions are enabled.
- Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md), never a public issue.
- Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) in all project spaces.

## Development setup

Required tools are Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22+, and npm.

```bash
uv sync --extra dev --locked
npm --prefix frontend ci
uv run work-harness check-config
```

## Making a change

1. Create a focused branch from `main`.
2. Keep Provider-specific behavior behind adapters and logical model routes.
3. Preserve the homogeneous-agent contract and structured Pydantic boundaries.
4. Never commit API keys, `.env` files, SQLite databases, prompts containing private data,
   or raw model responses from non-public datasets.
5. Add or update tests for behavior changes.
6. Update `CHANGELOG.md` for user-visible changes.
7. Update English and Chinese README content together when the public interface changes.

## Required checks

```bash
uv run ruff check src tests scripts
uv run pytest
uv run work-harness check-compatibility
uv run work-harness eval
npm --prefix frontend run type-check
npm --prefix frontend test -- --run
npm --prefix frontend run build
docker compose --profile public config --quiet
python scripts/check_release.py
```

The Mock evaluation checks workflow contracts only; it is not evidence of real-model quality.

## Pull requests

Pull requests should be small enough to review, explain the user-visible outcome, list the
checks run, and call out migrations or security consequences. Maintainers may ask to split
unrelated work. By contributing, you agree that your contribution is licensed under the
project's MIT License.

## Maintainer release process

1. Update the version in `pyproject.toml`, `src/work_harness/__init__.py`, and
   `frontend/package.json`.
2. Move user-visible entries from `Unreleased` into a dated section in `CHANGELOG.md`.
3. Run all required checks and `uv build` locally.
4. Push a signed `vX.Y.Z` tag and create a GitHub Release from that exact tag.
5. Publish the Release. GitHub Actions validates the tag/version match, attaches the Python
   distributions with provenance, and publishes the multi-platform image to GHCR.

The release workflow intentionally does not publish to PyPI and requires no repository API
secret. GitHub's scoped `GITHUB_TOKEN` is used for release assets and GHCR.
