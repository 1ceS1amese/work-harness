# Work Harness Skill manifest

Runtime Skills add `agents/work-harness.yaml` beside the standard `SKILL.md` metadata.

```yaml
schema_version: 1
version: 1.0.0
logical_model: worker-fast
required_capabilities: [chat, structured_output, tool_calling]
allowed_tools: [workspace_read]
compatible_providers: ["*"]
preferred_providers: [mock-secondary, mock, compatible]
applies_to: [risk]
```

Fields:

- `schema_version`: Manifest contract version. The current value is `1`.
- `version`: Skill semantic version in `major.minor.patch` form.
- `logical_model`: Logical model whose candidate set contains every selectable Provider.
- `required_capabilities`: Provider features the Skill needs. These are added to the model request.
- `allowed_tools`: Exact Tool names the Skill may advertise to a Worker.
- `compatible_providers`: Use `["*"]` for any enabled Provider that passes the declared capability contract, or list explicit Provider names for a restricted Skill.
- `preferred_providers`: Ordered subset used when a task does not override the Provider.
- `applies_to`: Optional worker functions: `analysis`, `research`, `execution`, or `risk`. An empty list applies the Skill to every subtask.

Compatibility is fail-closed. Startup rejects an unknown logical model, Provider, or Tool; a missing Provider capability; a Tool unavailable to an assignable worker; or a Skill with no enabled compatible candidate. Composition additionally requires a shared logical model and at least one common available Provider.
