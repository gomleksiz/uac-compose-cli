# Filters and `find`

## Filters on fields

`export --where` (`-W`) and an `apply` step's `where:` keep only the objects
whose fields match. Case is ignored; a dotted path reaches rows.

```bash
uac-compose export '*Sleep*' -W type=linux -W 'agent=stone*' -W 'command*=sleep' -l
```

```yaml
  - update task: '*Sleep*'
    where: {type: linux, agent: 'stone*', command: {regex: 'sleep \d+'}}
    set: {agent: AGT-03}
  - delete: Task.find(name='*_old').where(type=linux).where(agent!='prod*')
```

| test | YAML | chain / `-W` |
|---|---|---|
| wildcard or equal | `agent: 'stone*'` | `agent='stone*'` |
| any of | `agent: [A, B]` | `agent=[A, B]` |
| not | `agent: {not: X}` | `agent!=X` |
| regex | `command: {regex: R}` | `command~R` |
| contains | `command: {contains: T}` | `command*=T` |

A field is a UAC field name, as an export in the standard style writes it,
or a dotted path through rows: `actions.emailNotifications.to`. A test on a
path holds when any row holds it.

All tests of a filter must hold. To find objects where one field **or**
another holds, run one filter for each field.

## Names, wildcards and regular expressions

A name with `*`, `?` or `[` is a wildcard. With `-E` (export) or `regex: true`
(a change-file step), every name is a regular expression: it is found
anywhere in the name, so use `^` and `$` to anchor it. Case is ignored. The
[cookbook](../COOKBOOK.md#8-regular-expressions) has many examples, such as
names with a date in them.

## Build a filter with `find`

`find` is an interactive session for writing a filter when you do not know
the field names or values yet. Start with a server filter, one
`list-advanced` call; then type tests and see what matches at once.

```
$ uac-compose find -t task
start> workflowname=Nightly WF       # a server filter: the session never reads every task
  uac task list-advanced workflowname=Nightly WF: 40 tasks in 1.2s
find> fields agent                    # which fields exist, how many have one, a sample
find> values agent                    # each value, with its count
find> command~sleep \d+               # a test, as -W takes it
  12 of 40 match (1 test)
find> step delete                     # the change-file step for this filter
- delete task: '*'
  from:
    workflowname: Nightly WF
  where:
    command:
      regex: sleep \d+
find> save changes.apply.yaml delete  # or add it to a change file
```

`help` lists every command; `filters` lists the server filters of the kind
and what each takes. A change-file step can use the same server filters as
`from:`, so it selects what the session showed:

```yaml
  - delete task: '*'
    from: {workflowname: Nightly WF}
    where: {command: {regex: 'sleep \d+'}}
```

### What a session understands

| type | does |
|---|---|
| `agent=AGT-01`, `command~sleep \d+`, ... | add a test: one per line, as `-W` takes it |
| `where: {agent: 'AGT*'}` | add tests in the change-file form |
| `fields [TEXT]` | the fields of the matches: how many have one, the most common value |
| `values FIELD` | each value of a field, with its count |
| `show NAME` | one object's fields and values |
| `list` / `list all` | the matches (15 / all) |
| `tests`, `undo`, `clear` | the tests now; drop the last one; drop them all |
| `from FILTER=VALUE` | one more server filter: the controller lists again |
| `filters` | the server filters of this kind, and what each takes |
| `step ACTION` | the filter as a change-file step (`delete`, `update`, `rename`) |
| `save FILE [ACTION]` | add that step to a change file (made if new) |
| `help`, `quit` | |

### Server filters

The first filter of a session, and a step's `from:`, go to the controller as
`uac <group> list-advanced` filters. Tasks, triggers, variables, virtual
resources and agent clusters have them; other kinds list all of their
objects.

| kind | filters |
|---|---|
| task | `taskname` (wildcards), `agentname` (exact), `type` (UI name: `Unix`, `Windows`...), `business_services`, `workflowname`, `workflowid`, `updated_time`, `updated_time_type`, `templateid`, `templatename` |
| trigger | `triggername` (wildcards), `type` (`Time`, `Cron`...), `business_services`, `enabled`, `tasks`, `description` |
| variable | `variablename` (wildcards), `scope`, `taskname`, `triggername`, `business_services` |
| virtual resource | `resourcename` (wildcards), `type`, `business_services` |
| agent cluster | `agentclustername` (wildcards), `type`, `business_services` |

A `from:` step sees the controller as it is before the change file runs: an
object an earlier step renamed is not found under its new name.
