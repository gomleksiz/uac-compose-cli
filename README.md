# uac-compose — UAC bundles as readable files

`uac-compose` moves Stonebranch Universal Automation Center definitions between a
controller and small, readable YAML files (`.uac.yaml`), and back. No AI.

```bash
uac-compose learn                                   # read a controller once: cache + defaults
uac-compose export "Nightly WF" --with-members      # workflow, members, triggers, calendars → one file
uac-compose export "Nightly Load" --style standard  # UAC field names, defaults removed
uac-compose export "Nightly WF" --with-members --variablize-agents  # agent: ${agent_x} + the variables
uac-compose export --bundle "MyBundle"              # a UAC Bundle, as the controller resolves it
uac-compose export --bundle "MyBundle" --suggest-templates  # shared fields written once, as templates: + like:
uac-compose import nightly.uac.yaml --dry-run           # what would be created / reused / conflict
uac-compose import nightly.uac.yaml --update            # reconcile what differs
uac-compose freeze nightly.uac.yaml                     # save the Universal Extensions it needs
uac-compose export "Nightly WF" -w -o nightly.uac.yaml -d  # export and freeze in one step
uac-compose export '*Sleep*'                        # wildcards: every kind, case ignored
uac-compose export '*Sleep*' -t linux -w            # only Linux/Unix tasks, and what they need
uac-compose export -E '^DB_.*_v[0-9]+$' -t variable # a regular expression
uac-compose import nightly.uac.yaml --extensions ~/.uac/compose/default/extensions
```

New to it? Start with the [cookbook](docs/COOKBOOK.md).

## Install

```bash
pip install uac_compose-0.2.2-py3-none-any.whl     # Python 3.12 or newer
```

uac-compose runs the `uac` CLI for every call to the controller. It finds
`uac` on PATH (on Windows also `uac.exe`, `uac.cmd`, `uac.bat`). If it is
somewhere else, set `UAC_COMPOSE_UAC` to its full path:

```bat
set UAC_COMPOSE_UAC=C:\Tools\uac\uac.exe
```

## Documentation

The user guide is online at <https://uac-compose-cli.readthedocs.io/en/latest/>.
It is built from `docs/` with MkDocs (`mkdocs.yml`; Read the Docs builds it
with `.readthedocs.yaml`):

| page | what |
|---|---|
| [Install and set up](docs/install.md) | the `uac` CLI, profiles, `learn`, environment variables |
| [Cookbook](docs/COOKBOOK.md) | short recipes: find, change, rename, delete |
| [Export and import](docs/guide/export-import.md) | many objects at once, mass update, renames, UAC Bundles |
| [Filters and `find`](docs/guide/filters.md) | `-W` / `where:` tests, regular expressions, the `find` session |
| [Change files: `apply`](docs/guide/apply.md) | steps, action rows, rows in bundle syntax, backups, new users |
| [Universal Extensions](docs/guide/extensions.md) | `freeze` and moving Universal tasks |
| [File format](docs/FORMAT.md) · [Specification](docs/SPEC.md) | the reference |

To read the site on your machine:

```bash
pip install -r docs/requirements.txt -e .
mkdocs serve        # http://127.0.0.1:8000
```

For development: `docs/SPEC.md`, `docs/DEVELOPMENT-PLAN.md` (milestones and
acceptance tests), `docs/FORMAT.md` and `docs/LESSONS.md` (the traps `uac-ai`
already fell into).

**Status:** all eight milestones done, plus `export --bundle` and the reworked compact format (flat fields, `sla:`, richer `like:`, extension parameters by name). `make check-all` green.

## What is here

| path | what |
|---|---|
| `docs/` | the user guide (the documentation site) and the spec, plan, format, lessons |
| `mkdocs.yml`, `.readthedocs.yaml` | how the site is built, locally and on Read the Docs |
| `data/` | seed data copied from `uac_model`: OpenAPI spec, type registry, rules, corpus-derived templates for 8.0.2.0; `rules/inactive.yaml` is new |
| `examples/` | hand-written example files in both styles |

The sample exports used as the test corpus are **not** copied (real
production data). Tests read them from the folder `UAC_COMPOSE_CORPUS` names.

