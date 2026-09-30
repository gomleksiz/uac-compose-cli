# Export and import

`export` writes definitions to a `.uac.yaml` file; `import` applies a file
to a controller. Import decides for each object: **create** it, **reuse** it
(it is the same), **update** it (with `-u`), or report a **conflict**.
Nothing is written until every object is decided, and `-n` only shows the
decisions.

```bash
uac-compose export "Nightly WF" --with-members -o nightly.uac.yaml
uac-compose import nightly.uac.yaml -n -p target    # create / reuse / conflict
uac-compose import nightly.uac.yaml -p target       # create what is missing
uac-compose import nightly.uac.yaml -u -p target    # also update what differs
```

- `--style compact` (the default) writes short names and idioms; `--style
  standard` writes UAC's own field names and nesting, defaults removed. The
  [file format](../FORMAT.md) describes both.
- `--with-members` (`-w`) also exports what the objects need: a workflow's
  tasks, their triggers, calendars, credentials, scripts...
- `--variablize-agents` (`-a`) names each agent through a variable, so the
  target can use its own agents.
- `--suggest-templates` (`-T`) writes fields that several objects share once,
  as a `templates:` entry, and gives the objects `like:` it.

## Selecting many objects, and mass update

A name holding `*`, `?` or `[` is a wildcard; with `-E` every name is a
regular expression. Case is ignored. The names come from a live
`uac <group> list` of every kind (7,167 names in 5.5 s on a large
controller), narrowed by `-t`: a kind (`task`, `credential`, `script`,
`variable`...), a type (`linux`, `windows`, `timer`, `workflow`...), or a
comma list of both. Exact names still work as before, and `-w` follows the
members of whatever matched.

Up to 15 matches are exported at once. More, and export shows how many of each
kind match and asks which to take; with no terminal to ask, it stops unless
`-y` says take them all. `-l` only lists the matches.

Mass update: export, edit, import back.

```bash
uac-compose export '*' -t variable -l                  # see what would be taken
uac-compose export '*' -t variable -y -o vars.uac.yaml # every variable
# edit vars.uac.yaml
uac-compose import vars.uac.yaml -n                    # dry run: what differs
uac-compose import vars.uac.yaml -u                    # update them
```

## Renaming objects

Export with `-P update`: every object gets `was: <its name>`. Change the keys
(a new name, a prefix, a suffix), leave `was:`, and import with `-u`.

```bash
uac-compose export '*Sleep*' -t timer -P update -y -o sleep.uac.yaml
sed -i '' 's/^  \([^ ].*\):$/  DEV_\1:/' sleep.uac.yaml   # prefix every key with DEV_
uac-compose import sleep.uac.yaml -n     # rename   tasks/DEV_Sleep 10  (was 'Sleep 10')
uac-compose import sleep.uac.yaml -u
```

The object is renamed in place (same sysId), so workflows and triggers on the
controller keep pointing at it. A workflow or trigger in the same file that
names the old name is changed to the new one, and import says so.

## Exporting a UAC Bundle

A **Bundle** is UAC's own promotion unit: a named set of objects moved between
controllers. `--bundle` exports one.

```bash
uac-compose export --bundle "MyBundle" --out mybundle.uac.yaml
```

The set comes from `bundle get-report`, which applies the Bundle's own
`followReferences` rule — so the file holds exactly what a promotion would
carry. **That rule is the controller's, not ours.** It reaches a workflow's
members and the credentials and scripts they name, and it stops there: a
global variable a task reads is not pulled in, because a `${name}` is resolved
at run time and is not part of the definition. `--with-members` walks
references ourselves and is refused together with `--bundle`, because the two
would answer the same question differently.

The Bundle's **Exclude On Existence** becomes `create_only:` in the file — the
two say the same thing, *create it there if it is missing, never overwrite what
is already there*. It is written on the first line, where a reader meets it
before the objects it governs:

```yaml
create_only: [triggers, calendars, custom_days, variables, credentials, ...]
```

Every object in those sections is a seed: on import it is created when missing,
and when it differs it is reported as `keep` rather than rewritten — even under
`--update`. The whole rule is written, not just the sections this export
happened to fill, so an object added to the file later is covered too. Business
services are always on the list: UAC has no label for them, and a service is
shared by everything filed under it, so a promotion must never rewrite one.

A Bundle reaches further than a `.uac.yaml` file can: agent clusters, reports,
widgets, email templates, Universal Templates. Those are **named and counted**
rather than dropped in silence, and the rest is still exported:

```
?? not exported (5 universal template(s)): this format has no section for it
?? not exported: no event_template named 'SQL ODBC: Duration'
```

(The second is a Universal Template's own event, which the report lists under a
qualified name; it is created with the extension, not as an object.)

Measured on a live controller: the `MyBundle` Bundle exports 22 objects and every
one of them decides `reuse` when imported back; `OtherBundle` exports 52 of 54, the
other two being those extension events.
