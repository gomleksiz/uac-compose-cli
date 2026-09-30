# uac-compose

`uac-compose` moves Stonebranch Universal Automation Center (UAC) definitions
between a controller and small, readable YAML files (`.uac.yaml`), and back.
It can also change objects where they are, with a change file. No AI.

```bash
uac-compose learn                                   # read a controller once: cache + defaults
uac-compose export "Nightly WF" --with-members      # workflow, members, triggers, calendars → one file
uac-compose export "Nightly Load" --style standard  # UAC field names, defaults removed
uac-compose export --bundle "MyBundle"              # a UAC Bundle, as the controller resolves it
uac-compose import nightly.uac.yaml --dry-run       # what would be created / reused / conflict
uac-compose import nightly.uac.yaml --update        # reconcile what differs
uac-compose freeze nightly.uac.yaml                 # save the Universal Extensions it needs
uac-compose find -t task                            # build and test a filter, one line at a time
uac-compose apply changes.apply.yaml -n             # the plan of a change file; nothing is written
```

## The commands

| command | does |
|---|---|
| `learn` | read a controller once: cache its definitions and the defaults of each type |
| `export` | write definitions to a bundle file |
| `import` | apply a bundle: create, reuse, update, or report a conflict |
| `freeze` | save the Universal Extensions a bundle needs, so `import` can install them |
| `show` | show the parameters of a Universal Extension on a controller |
| `apply` | run a change file: update, rename and delete objects in place |
| `find` | build and test a filter for a change file, interactively |

Every command is described in the [command reference](reference/commands.md).

## Where to go next

- New to it? [Install](install.md) it, then read the [cookbook](COOKBOOK.md):
  short recipes to find, change, rename and delete objects.
- To copy objects to another controller: [export and import](guide/export-import.md).
- To change objects where they are: [change files](guide/apply.md).
- To select objects: [filters and `find`](guide/filters.md).
- To write or read a bundle by hand: the [file format](FORMAT.md).

!!! warning "Always look first"
    `export ... -l` lists what a name matches, and `apply ... -n` and
    `import ... -n` show every change and write nothing.
