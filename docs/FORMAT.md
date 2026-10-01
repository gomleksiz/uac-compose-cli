# The `.uac.yaml` format

A `.uac.yaml` file is YAML describing **one deployment unit**: the objects
`uac-compose import` creates or reconciles together. It is written in one of two
styles, and `import` accepts either — or both mixed in one file, since any UAC
field name is always legal.

| style | chosen by | what it looks like |
|---|---|---|
| `compact` (default) | `uac-compose export --style compact` | short type names, readable names, idioms, inactive fields hidden |
| `standard` | `uac-compose export --style standard` | UAC's own field names and nesting, defaults and inactive fields removed |

`standard` was called `uac`, and that name is still accepted.

Both compile to exactly the same UAC objects.

## 1. Top level

A file opens with its directives:

```yaml
version: 1                       # the .uac.yaml format; a higher one is refused
style: compact                   # what this file is written in
create_only: [credentials, triggers]   # sections to create, never overwrite
```

`version:` is the format, not the controller release. A file that states a
higher one is refused rather than half-read.

Sections are maps keyed by the object's name. They are created in this order,
whatever order the file lists them in:

```yaml
business_services:     # `- Name` / `- Name: description` (or a mapping): services to create
user_groups:           # a parent group is created before its children
agent_clusters:        # type: Linux/Unix | Windows | IBM i
credentials:
variables:
virtual_resources:
database_connections:
peoplesoft_connections:
sap_connections:
email_connections:
custom_days:
calendars:
scripts:               # key is scriptName
event_templates:
tasks:                 # workflows are tasks too
triggers:
templates:             # copied from by like:, never created
create_only: [variables, credentials]   # sections to create if missing, never overwrite
```

Inside a section, objects are created in file order, except that a task a
`Launch Task` action or a workflow names is created before the thing that
names it, and a user group's `parent` is created before the group.

Agent clusters and user groups list their members by name. The controller
fills in each member's label itself (an agent's type, a user's display name,
a role's description), so those are not written:

```yaml
user_groups:
  Operators:
    parent: Everyone
    groupMembers: [ada, alan]      # user names
    groupRoles: [ops_a]            # role values
    permissions:
    - permissionType: Task
      nameWildcard: '*'
      opRead: true
      opswiseGroups: [Finance]
agent_clusters:
  Batch Cluster:
    type: Linux/Unix
    agents: [agent-a, agent-b]
    distribution: Round Robin
```

The controller refuses a whole group or cluster when **one** member does not
exist on it, so an unknown member is an error that stops the import before
anything is written. Agents belong to machines and are never in a file: a
cluster promoted to another controller usually needs its agent list edited.
A UAC Bundle can carry agent clusters (`export --bundle`) but has no place
for user groups.

## 2. Types

`type:` is resolved per section. In compact style it is the short name —
strip `task`/`trigger`, split the camel case:

| section | compact | uac |
|---|---|---|
| tasks | `Unix`, `Windows`, `File Monitor`, `Workflow`, `Universal` | `taskUnix`, … |
| triggers | `Time`, `Cron`, `File Monitor` (=`triggerFm`), `Universal Monitor` (=`triggerUm`) | `triggerTime`, … |

`File Monitor` is therefore a task under `tasks:` and a trigger under
`triggers:`. Raw names work in either style.

For credentials, virtual resources and email connections `type:` is UAC's own
field (`Standard`, `Renewable`, `Outgoing`).

## 3. The same task, both styles

Stored on the controller (after removing sysIds, audit fields and run stats):

```json
{"type": "taskUnix", "name": "Nightly Load", "agentVar": "${agent}",
 "command": "/opt/etl/load.sh", "exitCodeProcessing": "Success Exitcode Range",
 "exitCodes": "0", "retryMaximum": 3, "retryInterval": 300,
 "lfEnabled": true, "lfType": "Duration", "lfDuration": "00:02:00:00",
 "lfTime": "00:00", "lsEnabled": false, "lsTime": "00:00", "lsType": "Time",
 "twWaitType": "None", "twWaitDuration": "00:00:00:00", "logLevel": "Inherited"}
```

`--style standard` — defaults (`exitCodeProcessing`, `lsEnabled`, `logLevel`,
`twWaitType` …) and inactive fields (`lfTime` under a Duration late finish,
`lsTime`) are gone; every name is UAC's:

```yaml
version: 1
style: standard

tasks:
  Nightly Load:
    type: taskUnix
    agentVar: ${agent}
    command: /opt/etl/load.sh
    exitCodes: '0'
    retryMaximum: 3
    retryInterval: 300
    lfEnabled: true
    lfType: Duration
    lfDuration: 00:02:00:00
```

`--style compact` — the same fields, flat, with the var-pair and duration
idioms. `exitCodes: '0'` is left out because a create fills it in:

```yaml
version: 1
style: compact

tasks:
  Nightly Load:
    type: Unix
    agent: ${agent}              # compiles to agentVar, because it holds ${...}
    command: /opt/etl/load.sh
    retryMaximum: 3
    retryInterval: 300
    sla:
      late_finish:
        duration: 2h             # on, Duration, 00:02:00:00
```

## 4. Flat, with one block (compact)

A task's fields are written **flat**, in UAC's own names. Field groups were
tried over real converted objects and removed: they bought structure nobody
asked for, and a reader who wants UAC's own shape has `--style standard`.

The one block that stays is `sla:`, because late start, late finish and early
finish are three settings of one thing:

```yaml
tasks:
  Nightly Load:
    type: Unix
    agent: linux-01
    command: /opt/etl/load.sh
    retryMaximum: 3
    sla:
      late_start:
        time: '07:00'            # a Time constraint
      late_finish:
        duration: 2h             # a Duration constraint
```

Three things the block says without writing them:

- **The family prefix.** Inside `late_finish`, `lfDuration` is `duration` —
  the block has already said which family it is. The full UAC name is accepted
  on input, as everywhere else.
- **The flag.** Writing the block is what switches it on, so `lfEnabled: true`
  is not written. `enabled: false` inside one still wins, and a block with
  nothing else to say keeps its flag.
- **The type.** A block that states a `duration` **is** a Duration constraint.
  `Average Duration` is written out, because it reads the offset fields rather
  than the duration.

Only the half the constraint reads is written: with a Time constraint the
stored duration is always `00:00:00:00` (measured: 65 late starts, 67 late
finishes), and with a Duration one the time is dead the same way.

`--show-inactive` writes these fields **flat** instead. There the residue of a
constraint that is *off* is shown too, and `lfEnabled: false` is the type's own
default and so is not in the object at all — a block with no flag would read as
the exact opposite of what is stored.

A block holds only its own fields, so a typo inside one is an error. The
nesting is display only: the same fields may be written flat instead, and
either way the compiled object is identical.

A few fields are **renamed** where UAC's name is not the word people use
(`data/rules/aliases.yaml`). A rename is refused where the type already has a
field of that name, so it can always be read back:

| where | UAC | compact |
|---|---|---|
| every type | `opswiseGroups` | `business_services` |
| credential | `runtimeUser` | `user` |
| abort / system-operation / set-variable rows | `notifyOnLateFinish` | `on_late_finish` |

**A real extension parameter wins over an alias.** One Universal task on a live
controller carries a parameter called `business_services`; on that object the
UAC field keeps its own name, because two keys of one name could not be told
apart. Same rule as collapsing a parameter onto a real UAC field: the thing
that is really there wins.

### `like:` copies a definition

A `templates:` entry is a bag of fields — it needs no `type:` and may hold
anything. Whatever the definition states wins over what it copied, and several
sources apply **in the order written**:

```yaml
templates:
  standard-retry: {retryMaximum: 3, retryInterval: 300}
  batch-target:   {agent: linux-batch-01, credentials: etl-svc}
tasks:
  Load A:
    type: Unix
    like: [standard-retry, batch-target]   # later wins over earlier
    command: /opt/a.sh
  Load B:
    like: Load A                           # another object works too
    command: /opt/b.sh
    retryMaximum: 5                        # and the definition wins over both
```

Mappings merge; lists replace.

`export --suggest-templates` writes these for you. After the export, it looks
in each section for fields that several objects state with the same value
(the same agent, credentials, extension, business services...). Each such set
becomes a `templates:` entry, and the objects that hold it say `like:` in place
of those fields:

- An entry is written when at least **3** objects use it and it holds at least
  **2** fields. The set that saves the most written fields is taken first.
- An object copies one entry at most, so a reader looks in one place.
- `type:`, `comment:` and a workflow's `tasks:` stay on the object.
- The entry is named from its `template`, `agent`, `credentials`... values
  (`linux-01-etl-svc`), else `task-common`. It never takes an object's name.
  Rename it freely: the name is only what `like:` says.
- Display only: every rewritten object is compiled both ways before it is
  kept, and must be the same object. Measured over the corpus, one export per
  archive: 3,955 of 5,286 objects use an entry, 9,864 written fields saved,
  every object compiles unchanged.

The command prints one line per entry, and the total, on stderr.

## 5. Idioms (compact; each used only when lossless)

| idiom | written | stored as |
|---|---|---|
| name/value lists | `variables: [- region: eu]` | `[{name: region, value: eu}]` |
| custom fields | `customField1: nightly` | `{label: TAG, value: nightly}` (controller owns the label) |
| var pair | `agent: ${agent}` | `agentVar: ${agent}` |
| duration | `lfDuration: 1h 49m` | `00:01:49:00` (always four parts) |
| enum | `outputType: STDOUT` (also accepts `1` or `Standard Output`) | `STDOUT` |
| `email_on` (in `actions:`) | `email_on: [failure, late finish]` + `email_connection` / `email_template` | one notification with only a status (Failed/Success), late finish, connection, template; anything more stays raw |
| calendars | `businessDays: [Monday, Friday]`, `customDays: [Xmas]` | `{value: "Monday,Friday"}`, `[{name: Xmas}]` |
| quarters | `quarters: [Jan 1, Apr 1, Jul 1, Oct 1]` | the four `*QuarterStart: {day: '1', month: Jan}`; all four or none |
| permission rows | `- type: Script` + `ops: [create, read]` | `permissionType`, and `opCreate: true`, `opRead: true`; a `false` flag keeps its UAC name (1,265 rows on two live controllers) |
| custom days | `observed: {Sat: Fri}` | `observedRules` rows |
| event template attributes | `attributes: [- file_name: File Name]`; `type:` only when it is not `Text` | `[{name: file_name, label: File Name, type: Text}]`; an attribute called `name`, `label` or `type` keeps UAC's shape (6 of 163 rows on two live controllers) |
| `run_if` / `skip_if` | `run_if: 1st business day of month` | the 37-field `runCriteria` record |
| workflow `after` / `condition` | `after: [A, {B: failure}]`, `condition: A OR B` | `workflowEdges`, `conditionExpression` |
| extension parameters | `action: upload-file` | `choiceField3: {label, name, value}` |
| action row variables | `variables: [- run_id: ${ops_task_id}]` | `[{name, value}]` |
| set-variable runs (in `actions:`) | one `setVariableActions` row with `variables: [- count: ${x}]` | adjacent rows alike except `variableName` / `variableValue`; corpus: 95 of 609 objects, 988 rows to 776 |

When emit cannot express something with an idiom it writes the raw UAC field.
Every object is checked before it is written: its compact body must compile
back to the same object, or the uac body is written instead. Measured over the
corpus: 5,281 of 5,286 objects are compact; 5 workflows whose edges carry stale
aliases are written raw.

An action row is written with **what it is** first: `status`, then
`operation`. A Universal task's extension parameters are written by name, not
as `choiceField3` — `uac-compose learn` knows the mapping, and both spellings compile
to the same object.

## 6. Inactive fields

Fields whose feature is switched off are not written (`data/rules/inactive.yaml`):
`lfEnabled: false` hides every `lf*`; `retryMaximum: 0` hides the other retry
fields; `outputReturnType: NONE` hides the retrieval details;
`exitCodeProcessing: Success Output Contains` hides `exitCodes`; an email
notification that attaches nothing hides its line counts. On a trigger,
`dayStyle` hides the date fields of the other day styles (`dayStyle: Simple`
hides `dateNoun`, `dateQualifier`, `intervalStartingDate` …),
`restriction: false` hides every `restriction*`, and `restrictedTimes: false`
hides `enabledStart` / `enabledEnd`.

On a custom day (and a calendar's `localCustomDays` row), `ctype` hides the
date fields that type does not read: an Absolute Repeating Date hides
`dayofweek`, `relfreq`, `nthAmount`, `nthType`; a Relative Repeating Date hides
`day`, and `nthAmount` / `nthType` unless `relfreq: Nth`; a Single Date or List
of Dates hides all of them. `adjustment: None` hides `adjustmentAmount` and
`adjustmentType`. Measured on two live controllers, 119 custom days: every
hidden field held the create value.

`exitCodes: '0'` is left out too, under the mode a create fills it in — not
because it is inactive, but because `rules/oncreate.yaml` writes the same value
and comparison is one-directional. Under `Failure Exitcode Range`, where
nothing would fill it, it is written.

This is display only: import never clears them on the controller. `uac-compose export
--show-inactive` writes them anyway. Import warns when a file states a field
its own settings make inactive.

## 7. Workflows

```yaml
tasks:
  Nightly:
    type: Workflow
    tasks:
      Extract:                                   # member = existing or defined task
      Load orders:   {task: Load, alias: Load orders, after: Extract}
      Load (2):      {task: Load, after: Load orders}
      Notify:
        after: [{Load (2): success}, {Extract: failure}]
        condition: any                           # OR instead of AND
        run_if: business day
        position: [120, 40]
```

- The member key is a **handle**; `task:` says which task when one repeats.
- `after:` conditions: `success` (default), `failure`, `success/failure`,
  `exit code <expr>`, `approved`, `approval required`. Failure-type
  conditions are invalid from a Workflow, Timer or Manual task; approval ones
  only from an Approval task.
- `vertexId:` appears only when the ids are not 2, 3, 4…; ids start at 2.
- A workflow whose edges use variable conditions is written raw
  (`workflowVertices` / `workflowEdges`).
- An edge with more than a condition keeps its extras:
  `after: [{A: {condition: success, straightEdge: true}}]`.
- A run criterion that one line cannot say exactly stays a raw
  `runCriteria:` list on the member (8 of 774 in the corpus). Criteria with
  `vertexId: Any` stay on the workflow itself.

## 8. Business services

- `business_services:` defines services: import creates the ones the
  controller does not have. Each item is `- Name` or `- Name: description`:

  ```yaml
  business_services:
  - M
  - Q: my description
  ```

  A mapping (`Name: {description: ...}`) says the same and is used for a
  service that holds more than a description.
- No object joins the list. Each object states its own `business_services`;
  none is the default, so an object in no service states nothing.
- Export lists every service it exports (`--with-members`) or that an
  exported object names.
- `import --update` only rewrites objects inside the file's own services,
  or in none.

## 9. Where a file comes from

`uac-compose export` writes one from objects you name, or from **UAC's own Bundle**:

```bash
uac-compose export --bundle "MyBundle"
```

The Bundle's `get-report` decides the set (its own `followReferences` rule),
and its **Exclude On Existence** becomes the file's `create_only:`, written on
the first line:

```yaml
create_only: [triggers, calendars, custom_days, credentials, business_services]
```

Kinds a `.uac.yaml` file has no section for — agent clusters, reports, widgets,
email templates, Universal Templates — are reported and left out.

## 10. Annotations

- `comment:` anywhere — ignored.
- `create_only: true` on an object — create if missing, never update.
- `was: <name>` on an object (top level only) — the name it has on the
  controller now. When the key differs, import **renames** that object in
  place (same sysId, needs `--update`), and every reference in the same file
  to the old name follows. `export --purpose update` writes it on every object.
- YAML `#` comments work too, but are lost on the next export.

## 11. Never in a file

- Secrets: `runtimePassword`, `runtimePassPhrase`, `runtimeToken`,
  `defaultPwd` — refused on import. A credential's `providerParameters` with a
  redacted (blank) value is not exported at all; a `comment:` says why.
- `sysId`, audit fields, run statistics.
- `*VarCheck` / `use*Var` toggles — rejected by the controller.
