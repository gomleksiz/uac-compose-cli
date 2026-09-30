# Install and set up

## Install

uac-compose needs Python 3.12 or newer.

```bash
pip install uac_compose-0.2.2-py3-none-any.whl
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
