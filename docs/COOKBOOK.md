# uac-compose cookbook

Short recipes for common jobs. Each recipe is a complete example you can copy.

- To **copy** objects to another controller: `export`, then `import`.
- To **change** objects where they are: write a change file, then `apply`.

Always look before you change: `export ... -l` lists what a name matches, and
`apply ... -n` shows every change and writes nothing.

---

## 1. Find objects

**All tasks with "Sleep" in the name**

```bash
uac-compose export '*Sleep*' -t task -l
```

**Only Linux tasks, on one agent, whose command has "sleep"**

```bash
uac-compose export '*' -t linux -W 'agent=AGT-01' -W 'command*=sleep' -l
```

`-W` (`--where`) tests a field. You can give it more than once.

| You want | Write |
|---|---|
| equal, or a wildcard | `-W 'agent=AGT*'` |
| one of a list | `-W 'agent=[AGT-01, AGT-02]'` |
| not | `-W 'agent!=AGT-01'` |
| contains | `-W 'command*=sleep'` |
| a regular expression | `-W 'command~sleep [0-9]+'` |

Case is always ignored.

**What are the field names?** Export one object in the standard style and
read it. The names in that file are the names `-W`, `where:` and `set:` use.
Or use `find` (below): `fields` lists them, `values FIELD` lists their values.

**Build a filter with `find`, when you do not know what to write**

```bash
uac-compose find -t task
```

1. Start with a server filter. `filters` shows them, for example
   `workflowname=Nightly WF`, `taskname=*Sleep*` or `type=Unix`.
2. Look: `fields` (or `fields email`) and `values agent`.
3. Type a test: `agent=AGT-01`, `command~sleep \d+`. See the count at once.
   `undo` drops the last test.
4. `step delete` prints the step for a change file. `save changes.apply.yaml delete`
   adds it to the file.

Then run `uac-compose apply changes.apply.yaml -n` to see the plan.

```bash
uac-compose export "Nightly Load" -s standard
```

---

## 2. The change file

A change file is a list of steps. They run in order.

```yaml
steps:
  - <action> <kind>: <names>
    where: <tests>      # optional: keep only the objects that pass
    set:   <values>     # the new values
```

- **action:** `update`, `delete`, `rename`, `replace`.
- **kind:** `task`, `trigger`, `variable`, `credential`, `script`,
  `calendar`, ... or a row inside a task: `email action`,
  `variable action`, `system action`, `abort action`, `snmp action`.
- **names:** one name, a list `[A, B]`, or a pattern `'*Nightly*'`.

Run it in two steps:

```bash
uac-compose apply changes.apply.yaml -n -v   # look: every change, nothing written
uac-compose apply changes.apply.yaml         # do it: shows the plan, asks, then writes
```

`apply` creates only users (see below). Before it writes, it checks each
step: every object must exist, the fields must be valid, and every object a
change names (a task to launch, a credential...) must exist.

### Create a user and add it to a group

The password is never in the file. Put it in an environment variable, and
name the variable in the step:

```yaml
steps:
  - create user: jdoe
    password_env: JDOE_PW
    set: {firstName: Jane, lastName: Doe, email: jane.doe@example.com}
    groups: [Everything Group]
```

```bash
read -rs JDOE_PW && export JDOE_PW     # type the password; it is not shown
uac-compose apply users.apply.yaml -n  # check: the user is new, the groups exist
uac-compose apply users.apply.yaml
```

A new user is active, and must change the password at first login. To
change either, add `active: false` or `passwordNeedsReset: false` under
`set:`.

---

## 3. Change fields

**One field on one task**

```yaml
steps:
  - update task: Nightly Load
    set: {command: load.sh --fast}
```

**Move every Linux task from one agent to another**

```yaml
steps:
  - update task: '*'
    where: {type: linux, agent: OLD-AGENT}
    set: {agent: NEW-AGENT}
```

**The same, written as a query**

```yaml
steps:
  - update: Task.find(name='*').where(type=linux).where(agent=OLD-AGENT)
    set: {agent: NEW-AGENT}
```

**Change a variable**

```yaml
steps:
  - update variable: DB_HOST
    set: {value: prod-db-02}
```

**The same settings on many tasks**

```yaml
templates:
  night:
    agent: AGT-02
    credentials: batch
steps:
  - update task: '*Nightly*'
    set: {like: night}
```

---

## 4. Actions inside tasks

A task's actions (email, set variable, system operation, abort, SNMP) are
rows. A row kind finds the rows in the tasks you name and changes only them.

**Launch a different task from a system action**

A task that launches "Create Service Now" when it fails should launch "Send
ServiceNow Event" instead:

```yaml
steps:
  - update system action: '*'            # look in every task
    where:
      operation: Launch Task
      task: Create Service Now           # the task it launches now
    set:
      task: Send ServiceNow Event        # the task it will launch
```

**Replace an e-mail address in every email action**

`edit:` changes part of a value, so the other addresses stay.

```yaml
steps:
  - update email action: '*'
    where: {to: {contains: jane.doe}}
    edit:
      to: {from: 'jane\.doe@example\.com', to: 'team@example.com'}
```

**Remove a set-variable action**

```yaml
steps:
  - delete variable action: 'Nightly*'
    where: {variableName: TMP_FLAG}
```

**Add a set-variable action**

```yaml
steps:
  - update task: 'Nightly*'
    add:
      actions.setVariableActions:
        - {status: Success, variableName: LAST_RUN, variableValue: '${_date}'}
```

**Add a system action from a template**

A row is written as export writes it in a bundle: the same field names, and
the short form `- name: value` for `variables`. `like:` copies a
`templates:` entry into the row; the row's own fields win over it.

```yaml
templates:
  snow:
    status: Failed
    operation: Launch Task
    description: Create ServiceNow
    task: Incident-Creation-ServiceNow
    variables:
      - failed_task_name: ${ops_task_name}
      - failed_task_id: ${ops_task_id}
steps:
  - delete system action: 'Nightly*'       # first remove the old row, so a
    where: {task: Incident-Creation-ServiceNow}   # second run adds no copy
  - update task: 'Nightly*'
    add:
      actions.systemOperations:
        - like: snow
        - like: snow
          status: Cancelled                # a second row: the same, on Cancelled
```

To see how a row is written, export a task that has one:
`uac-compose export "Some Task"`.

---

## 5. Workflows

**Use a new task in workflows, then delete the old one**

```yaml
steps:
  - replace task: Old Load
    with: New Load
    in: '*Nightly*'          # only these workflows; leave out for all
  - delete task: Old Load
```

Only the task in each step changes. The workflow keeps its shape.

---

## 6. Rename

**Rename one task**

```yaml
steps:
  - rename task: Nightly Load
    to: Nightly Load v2
```

**Add a prefix to many tasks**

```yaml
steps:
  - rename task: '*Sleep*'
    prefix: DEV_           # or suffix: _old, or replace: {from: Sleep, to: Wait}
```

A rename keeps the object (the same sysId), so workflows and triggers that
use it follow the new name.

---

## 7. Delete

**Delete test tasks, but not the Windows ones**

```yaml
steps:
  - delete task: 'TEST_*'
    where: {type: {not: windows}}
```

A task that a workflow still runs is not deleted: the plan names the
workflows. Replace it there first (section 5), or delete those workflows in
an earlier step.

**Delete a list of variables**

```yaml
steps:
  - delete variable: [TMP_A, TMP_B, TMP_C]
```

---

## 8. Regular expressions

A wildcard (`*`, `?`) is enough for most names. Use a regular expression
(regex) when the name has a shape: digits, a date, "this word or that word".

There are three places for a regex:

| Where | Write | Tests |
|---|---|---|
| the names | `regex: true` on the step | the object name |
| a field test | `where: {field: {regex: R}}` | any field, also `name` |
| a new name | `rename` with `replace: {from: R, to: T}` | a part of the name |

Rules:

- Case is ignored. Write `(?-i:...)` to make a part case-sensitive.
- The regex is **found anywhere** in the name; it does not have to match the
  full name. Use `^` for "starts with" and `$` for "ends with".
- Put a regex in **single quotes** and write `\d` with one backslash. In
  single quotes YAML keeps the text as it is.
- With `regex: true`, every name in the step is a regex, also in a list.
- `export ... -E -l` lists what a regex matches, before you change anything.

**Delete all triggers with a date in the name**

Names like `Daily 2024-01-31`, `Load_20240131`, `EOM 31.01.2024`,
`Run 01/31/2024`, `Payroll Jan2024` and `Close_2023_Q4`:

```yaml
steps:
  - delete trigger:
      - '(19|20)\d\d[-_./]?(0[1-9]|1[0-2])[-_./]?(0[1-9]|[12]\d|3[01])'  # 2024-01-31, 20240131
      - '(0?[1-9]|[12]\d|3[01])[./-](0?[1-9]|1[0-2])[./-](19|20)\d\d'    # 31.01.2024, 31-1-2024
      - '(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(19|20)\d\d'            # 01/31/2024
      - '(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ ]?(19|20)?\d\d'  # Jan2024, March-24
      - '(19|20)\d\d[-_ ]?q[1-4]'                                         # 2023_Q4
    regex: true
```

Look first. Try a regex in `find`, on the names of all triggers:

```
$ uac-compose find -t trigger
start> triggername=*
find> name~(19|20)\d\d[-_./]?(0[1-9]|1[0-2])[-_./]?(0[1-9]|[12]\d|3[01])
  12 of 253 match (1 test)
find> list all
```

Or list what the change file matches:

```bash
uac-compose export '(19|20)\d\d[-_./]?(0[1-9]|1[0-2])[-_./]?(0[1-9]|[12]\d|3[01])' -E -t trigger -l
uac-compose apply delete-dated.apply.yaml -n -v
uac-compose apply delete-dated.apply.yaml -B dated-triggers.uac.yaml   # save them, then delete
```

If you need them again: `uac-compose import dated-triggers.uac.yaml -u`.

A regex can find more than you want. `Job_v20240131x` also has a date in it.
To keep some triggers, add a `where:`:

```yaml
    where: {name: {not: 'KEEP_*'}}      # keep the triggers that start with KEEP_
```

**Delete only the triggers for a past year**

```yaml
steps:
  - delete trigger: '20(1\d|2[0-4])'    # 2010 to 2024
    regex: true
```

**The same, written as a query**

```yaml
steps:
  - delete: Trigger.find(name='*').where(name~'(19|20)\d\d-\d\d-\d\d')
```

**Names that start or end in a given way**

```yaml
steps:
  - update task: '^(tmp|test)_'         # starts with tmp_ or test_
    regex: true
    set: {agent: AGT-TEST}

  - delete variable: '_(old|bak|\d+)$'  # ends with _old, _bak or _<digits>
    regex: true
```

**A regex on a field, not on the name**

The name is a wildcard; the `where:` tests the fields.

```yaml
steps:
  - update task: '*'
    where:
      type: linux
      command: {regex: '^sleep \d+$'}   # the command is only "sleep <number>"
    set: {command: sleep 5}
```

**Rename with parts of the old name**

`( )` keeps a part; `\1`, `\2`, ... put it in the new name.
`Load_20240131` becomes `Load_2024-01-31`:

```yaml
steps:
  - rename task: '_\d{8}$'
    regex: true
    replace: {from: '_(\d{4})(\d{2})(\d{2})$', to: '_\1-\2-\3'}
```

Move a prefix to the end, `DEV_Nightly Load` becomes `Nightly Load (DEV)`:

```yaml
steps:
  - rename task: '^DEV_'
    regex: true
    replace: {from: '^DEV_(.*)$', to: '\1 (DEV)'}
```

**Change a part of a value in action rows**

`edit:` works on action rows only. For a task field, use `set:`.

```yaml
steps:
  - update email action: '*'
    where: {to: {regex: '@old-domain\.com'}}
    edit:
      to: {from: '@old-domain\.com', to: '@new-domain.com'}
```

---

## 9. Mass update with export and import

For big edits by hand: export, edit the file, import it back.

```bash
uac-compose export '*' -t variable -P update -y -o vars.uac.yaml
# edit vars.uac.yaml: change values; change a key to rename (keep its was: line)
uac-compose import vars.uac.yaml -n      # look
uac-compose import vars.uac.yaml -u      # update
```

---

## 10. Copy to another controller

```bash
uac-compose export "Nightly WF" -w -o nightly.uac.yaml -d -p source
uac-compose import nightly.uac.yaml -e ~/.uac/compose/source/extensions -n -p target
uac-compose import nightly.uac.yaml -e ~/.uac/compose/source/extensions -p target
```

`-w` brings what the workflow needs; `-d` also saves the Universal
Extensions it uses, so `import -e` can install them on the target.

---

## Tips

- `-n` first, always. Add `-v` to `apply -n` to see every object, not only
  the first 15 of each step.
- Add `-B before.uac.yaml` to `apply` to save the objects before the writes.
  `uac-compose import before.uac.yaml -u` puts them back. A row that apply
  added stays; apply warns about it. `-B before.json` saves the full objects
  as the controller returns them.
- A step that matches nothing is an error, and nothing is written.
- `apply` stops at the first write that fails and says how many ran.
- More than 15 matches: `export` and `apply` ask first; add `-y` in a script.
- Every example step above is also in `examples/changes.apply.yaml`.
