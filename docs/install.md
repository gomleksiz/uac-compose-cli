# Install and set up

## Install

uac-compose needs Python 3.12 or newer.

```bash
pip install uac_compose-0.3.0-py3-none-any.whl
uac-compose --version
```

## The uac CLI

uac-compose talks to the controller through the Stonebranch `uac` CLI: every
read and write is one `uac` call. Install and configure `uac` first, and make
sure `uac task list` works.

uac-compose finds `uac` on PATH. On Windows it also finds `uac.exe`,
`uac.cmd` and `uac.bat`. If `uac` is somewhere else, set `UAC_COMPOSE_UAC`
to its full path:

=== "Windows"

    ```bat
    set UAC_COMPOSE_UAC=C:\Tools\uac\uac.exe
    ```

=== "Linux / macOS"

    ```bash
    export UAC_COMPOSE_UAC=/opt/uac/bin/uac
    ```

## Profiles

A profile is a `uac` CLI profile: one controller and its login. Give it with
`-p` (`--profile`) on any command, or set `UAC_COMPOSE_PROFILE`. Without
either, the profile is `default`.

```bash
uac-compose export "Nightly WF" -w -p source -o nightly.uac.yaml
uac-compose import nightly.uac.yaml -p target -n
```

## Learn a controller

`learn` reads a controller once. It caches the definitions and works out the
default value of each field for each type, so an export can leave the
defaults out. Run it once per profile, and again with `-r` when the
controller changes much.

```bash
uac-compose learn -p source
```

You do not need `learn` for Universal Extension tasks. `export`, `import`,
`apply` and `freeze FILE` read the controller's extensions each time they
connect (about 2 to 5 seconds), and save the map in the workspace. After
`import --extensions` installs an extension, import reads them again.

The cache, the frozen extensions and the reports go in the workspace:
`~/.uac/compose/<profile>/`, or the folder that `UAC_COMPOSE_WORKSPACE`
names.

## Environment variables

`uac-compose` with no command lists them too.

| variable | what | default |
|---|---|---|
| `UAC_COMPOSE_PROFILE` | the profile when `-p` is not given | `default` |
| `UAC_COMPOSE_WORKSPACE` | where the cache, extensions and reports go | `~/.uac/compose` |
| `UAC_COMPOSE_UAC` | the full path to the `uac` CLI, when it is not on PATH | `uac` on PATH |
| `UAC_COMPOSE_DATA` | the shipped data folder, when the package is imported from a zip and cannot read its own `_data` | the package's `_data` |
| `UAC_COMPOSE_HUB_URL` | the Composer Hub for `hub:` references and `publish` | `https://composer.husey.in` |
| `UAC_COMPOSE_HUB_TOKEN` | the hub API token for `publish`; it wins over the file | the file `~/.config/uac-compose/hub-token` |

## Use as a library

A program can run export, import and apply without the `uac` CLI. Install
the `api` extra: it adds `uac-api`, which calls the controller's REST API in
the same process.

```bash
pip install 'uac-compose[api]'
```

```python
from pathlib import Path

from uac_compose import service
from uac_compose.learn import Workspace
from uac_compose.transport_api import ApiTransport

api = ApiTransport.connect("https://uc.example.com", token="...")  # or user=, password=
ws = Workspace(Path("/tmp/uac-compose"))  # any folder; the extension map is read each run

result = service.import_bundle(api, ws, Path("nightly.uac.yaml").read_text(),
                               dry_run=True, out=print, say=print)
text = service.export_bundle(api, ws, ["Nightly*"], kind="task", yes=True).text
```

- `say` gets the progress lines and `out` the results.
- A failure raises `service.ServiceError`, with the same text the command
  line shows.
- The functions never ask a question:
  - export of more than 15 matches needs `yes=True` (or a `choose`
    callback);
  - a real apply needs `yes=True` (or a `confirm` callback).
