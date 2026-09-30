# Change files: `apply`

A change file is a list of steps that run in order. It updates, renames and
deletes objects that exist; it never creates them.

```yaml
version: 1
steps:
  - delete task: '*_tmp'
    type: linux
  - update task: Nightly Load
    set: {command: load.sh --fast}
    add:
      actions.setVariableActions:
        - {status: Success, variableName: LAST_RUN, variableValue: '${_date}'}
  - rename task: '*Sleep*'
    prefix: DEV_
  - replace task: Old Load        # in every workflow named *Nightly*
    with: New Load
    in: '*Nightly*'
```

```bash
uac-compose apply changes.apply.yaml -n   # the plan: every object, every change
uac-compose apply changes.apply.yaml      # the plan, a question, then the writes
```

Every step is checked first, reading live: each object must exist, and
fields and rows must be valid. Nothing is written until the whole plan holds.
The writes then run one at a time and stop at the first error. See
`examples/changes.apply.yaml` for every kind of step.

## Steps

A step's first key is `<action> <kind>: <names>`:

- **action:** `delete`, `update`, `rename`, `replace`.
- **kind:** `task`, `trigger`, `variable`, `credential`, `script`,
  `calendar`, ... or a row inside a task: `email action`, `variable action`,
  `system action`, `abort action`, `snmp action`.
- **names:** one name, a list `[A, B]`, a wildcard `'*Nightly*'`, or with
  `regex: true` a regular expression.

Other keys narrow or change it:

| key | does |
|---|---|
| `type:` | keep one type: `linux`, `windows`, `timer`... |
| `where:` | field tests that must hold ([filters](filters.md)) |
| `from:` | server filters: the candidates come from one `list-advanced` call |
| `set:` | new field values, in bundle syntax; `like:` copies a `templates:` entry |
| `add:` / `remove:` / `change:` | rows of a list, by dotted path: `actions.systemOperations` |
| `to:` / `prefix:` / `suffix:` / `replace:` | the new name (rename) |
| `with:` / `in:` | the task to put in, and the workflows to look in (replace) |

The same filter can also be written as one line:

```yaml
  - delete: Task.find(name='*_old').where(type=linux).where(agent!='prod*')
```

`find` builds steps for you: see [filters and `find`](filters.md).

## Action rows as objects

Email, variable, system, abort and SNMP actions can be found and changed as
objects of their own, inside the tasks a step names:

```yaml
steps:
  - update email action: '*'                  # look in every task
    where: {to: {contains: jane.doe}}          # the rows to change
    edit:
      to: {from: 'jane\.doe@example\.com', to: 'team@example.com'}
  - delete variable action: 'Nightly*'
    where: {variableName: 'TMP_*'}
```

`set:` replaces a field of the row; `edit:` changes part of it. Run with `-n`
first: the plan lists every task and row it would change.

## Rows in bundle syntax

An `add:` row, and the `set:` of a changed row, are written as export writes
them in a bundle: the same field names, and the short form `- name: value`
for `variables`. A row may say `like:` to copy a `templates:` entry; its own
fields win over the template.

```yaml
templates:
  snow:
    status: Failed
    operation: Launch Task
    task: Incident-Creation-ServiceNow
    variables:
      - failed_task_name: ${ops_task_name}
steps:
  - delete system action: 'Nightly*'        # remove the old row first, so a
    where: {task: Incident-Creation-ServiceNow}  # second run adds no copy
  - update task: 'Nightly*'
    add:
      actions.systemOperations:
        - like: snow
```

To see how a row is written, export a task that has one.

## Backup before the writes

`--backup FILE` (`-B`) saves every object the plan changes, as it is before
the first write. If the backup cannot be written, nothing is changed. The file
must be new: a backup is never overwritten. `-n` writes no backup.

```bash
uac-compose apply changes.apply.yaml -B before.uac.yaml   # compose: import -u undoes it
uac-compose apply changes.apply.yaml -B before.json       # json: the objects as UAC returns them
uac-compose import before.uac.yaml -u                     # undo
```

The format comes from the file name (`.json` is json, else compose), or from
`--backup-format compose|json`.

- **compose**: a bundle in the standard style. Each object has `was:` set to
  its name after the change, so `import -u` updates it back, renames it back
  (same sysId), or makes it again if it was deleted. Every field the plan
  changes is written with its old value, also a default. Import never removes
  a row, so a row the plan adds stays; `apply` warns about it.
- **json**: `{profile, file, written, objects: [{kind, name, after, object}]}`,
  where `after` is `updated`, `renamed to X` or `deleted`. The full object.

Neither format holds a secret (`runtimePassword` and the like are left out),
so a deleted credential comes back without its password.
