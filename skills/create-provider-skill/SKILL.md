---
name: create-provider-skill
description: Create or update modular Work Harness Skills that declare compatible providers, required capabilities, tools, and routing preferences. Use when adding a task capability or making it selectable across providers.
---

# Create Provider Skill

Create each runtime Skill under `skills/<skill-id>/` with `SKILL.md`, `agents/openai.yaml`, and `agents/work-harness.yaml`. Read [references/manifest-schema.md](references/manifest-schema.md) before editing a runtime manifest.

## Workflow

1. Choose a lowercase hyphenated Skill ID and write a precise trigger-oriented `description` in `SKILL.md` frontmatter. Keep frontmatter limited to `name` and `description`.
2. Write task instructions in imperative form. Put reusable operational detail in `references/` only when it keeps `SKILL.md` focused.
3. Declare the logical model, capability requirements, allowed tools, compatible providers, preferences, and optional function scope in `agents/work-harness.yaml`.
4. Confirm every compatible Provider is an enabled candidate of the declared logical model and truthfully exposes every required capability.
5. Confirm every declared Tool is registered and allowed for all assignable worker roles that may receive the Skill.
6. Run `work-harness skills validate` and the test suite. Resolve incompatibilities instead of weakening the manifest.

Use `preferred_providers` for default routing. A task-level Provider override remains authoritative only when it is in the Skill's computed available-provider set. Multiple Skills can share a subtask only when they use one logical model and have at least one common available Provider.
