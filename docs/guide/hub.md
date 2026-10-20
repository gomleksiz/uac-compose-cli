# Share bundles: the Composer Hub

The [Composer Hub](https://composer.husey.in) keeps bundles like PyPI keeps
Python packages: each has a name, version numbers, and a SHA-256 for each
file. Anyone can browse and download. To publish, you need an account (an
admin approves it) and an API token.

## Install a bundle from the hub

`import` takes a hub reference or a URL where it takes a file. The references
work like pip's:

| reference | means |
|---|---|
| `hub:nightly-transfer` | the latest published version |
| `hub:nightly-transfer==1.0` or `hub:nightly-transfer@1.0` | exactly 1.0 |
| `'hub:nightly-transfer>=1.0,<2'` | the highest 1.x (quote `<` and `>` in a shell) |
| `'hub:nightly-transfer<=1.2'` | the highest up to 1.2 |
| `https://composer.husey.in/d/nightly-transfer==1.0` | the same file, by URL |

```bash
# always a dry run first: what would be created, reused or in conflict
uac-compose import -n hub:nightly-transfer==1.0 --sha256 05d9c27d...
uac-compose import hub:nightly-transfer==1.0 --sha256 05d9c27d...
```

- Each download is checked against the SHA-256 the hub gives. A file that
  does not match is refused before it is read.
- `--sha256` also pins the file, like pip's `--require-hashes`. Copy the
  value from the package page. With it, a changed hub cannot change your file.
- A pre-release (`1.1rc1`) is used only when the reference names one.

!!! warning "Read a bundle before you import it"
    A bundle can hold scripts and commands that run on your agents. Look at
    the YAML on the package page, then import with `-n`.

## Requirements

The package page lists what the target controller needs before import:

- **Universal Extensions.** The hub finds them in the file (the `template:`
  of each Universal task), so the list is always complete. Save them with
  [`freeze`](extensions.md) and install them with `import --extensions`.
- **The owner's list.** Things the hub cannot find: a database connection,
  a credential, an agent with a tool on it. The owner writes these with
  `--require`, or on the site.

## Publish a bundle

1. [Sign up](https://composer.husey.in/signup) and wait for an admin to
   approve the account.
2. Create an API token on the [tokens page](https://composer.husey.in/tokens).
   It is shown once. Put it in `UAC_COMPOSE_HUB_TOKEN`, or save it in a file:

    ```bash
    mkdir -p ~/.config/uac-compose
    printf '%s' 'cmp_...' > ~/.config/uac-compose/hub-token
    chmod 600 ~/.config/uac-compose/hub-token
    ```

    When the variable is set, it wins over the file. The token is never a
    command-line option, so it does not stay in the shell history.

3. Publish:

    ```bash
    uac-compose publish nightly.uac.yaml nightly-transfer==1.0 \
        -d "Moves the nightly files and tells the team." \
        --tag "file transfer" \
        --require "An SFTP credential named Partner SFTP"
    ```

    The command prints the page, the SHA-256 and the install command.

| option | does |
|---|---|
| `-d TEXT` / `--description-file FILE` | one paragraph about the bundle |
| `--tag TAG` | a category (up to 5); repeat for more |
| `--require TEXT` | a requirement for the package page; repeat for more |
| `--draft` | upload a draft that only you see; check it on the site, then publish it there |

### A published version never changes

- `name==1.0` always names one file. To change a bundle, publish a new
  version number.
- You can delete a version, but its number is never used again.
- A draft can change until you publish it. On the site, the draft page shows
  the bundle as others will see it, with an editor.

### What the hub refuses

`publish` checks the file before it uploads, and the hub checks it again:

- a file that `import` cannot read: not valid YAML, an unknown section, a
  format version that is too new;
- a secret with a value (`runtimePassword`, `runtimeToken`, ...): set
  secrets on the controller, never in a bundle;
- a file larger than 1 MB.

## Settings

| variable | what | default |
|---|---|---|
| `UAC_COMPOSE_HUB_URL` | the hub for `hub:` references and `publish` | `https://composer.husey.in` |
| `UAC_COMPOSE_HUB_TOKEN` | the API token for `publish`; it wins over the file | `~/.config/uac-compose/hub-token` |

The UC Compose Universal Extension has the same two actions: Bundle Source
**Composer Hub** for Import, and the **Publish** action.
