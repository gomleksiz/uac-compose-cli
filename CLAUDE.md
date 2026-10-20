# CLAUDE.md

`uac-compose` exports Stonebranch UAC definitions to readable `.uac.yaml` bundle files
and imports them back. Eight commands: `learn`, `export`, `import`, `freeze`,
`show`, `apply`, `find`, `publish` (to the Composer Hub, `hub.py`; the site is the
separate `composer-web` project).
No AI features. Keep it small: do not add commands, project files or
`dev` subcommands without being asked.

Read first: `docs/SPEC.md` (spec), `docs/DEVELOPMENT-PLAN.md` (milestones and
acceptance tests), `docs/FORMAT.md`, `docs/LESSONS.md`.

## Rules that must not be broken

1. `inflate(simplify(x, t), t) == normalize(x)` for every corpus object
   (5,286 / 5,286). A change that lowers the count is a bug in the change.
2. `compile(emit(x, style)) == simplify(x)` for both styles.
3. Readability (groups, idioms, inactive-field hiding) is a layer over the
   core and never changes an object. Inactive-field rules are display only.
4. An idiom is used only when lossless; otherwise emit the raw UAC field.
5. References travel by name, never sysId. Never default a field that names
   another object.
6. Comparison on import is one-directional: only fields the file states.
7. Per-type behavior lives in `data/` (registry, templates, rules,
   `uacdocs.yaml`), not in code.
8. Never write or accept a secret in a bundle.
9. A new inactive-field rule or validation rung is measured over the corpus
   first; write the count next to it.

## Data

- `data/` is read-only seed data copied from the older `uac_model` project. Never
  hand-edit `data/openapi.json`.
- `data/uacdocs.yaml` was distilled once from the vendor field
  documentation, which is not in this repo; edit it by hand.
- The test corpus is **real production data** at `UAC_COMPOSE_CORPUS` (a
  local folder). Never copy it into this repo and never paste object
  contents into commits, issues or docs.

## Commands

```bash
make check        # ruff + mypy --strict + fast tests
make test-corpus  # the invariants over the real exports
make check-all    # both; run before calling a milestone done
```
