# `uac-compose` — a small, standalone UAC bundle tool (build spec)

## Context

`uac-ai` (the `uac_model` project) grew a bundle format (`.uac.yaml`) that is its
most useful part, but it sits inside a large AI codebase. `uac-compose` is a **new,
AI-free, deliberately small tool**, written from scratch, with exactly four
commands:

| command | does |
|---|---|
| `uac-compose learn`  | read a controller once: cache definitions, derive per-type defaults |
| `uac-compose export` | write definitions to a bundle — `compact` (readable) or `uac` (UAC syntax, defaults removed) |
| `uac-compose import` | apply a bundle idempotently (create / reuse / update / conflict) |
| `uac-compose freeze` | export the Universal Extensions a bundle needs, so `import` can install them |
| `uac-compose apply`  | run a change file: update, rename, delete objects in place (§9a) |
| `uac-compose find`   | build and test a change-file filter, one line at a time (§9b) |

No `dev` commands, no project files, no functions, no library tree, no
editor schema, no LLM. The build order with acceptance criteria is in
`DEVELOPMENT-PLAN.md`; the file format on its own is in `FORMAT.md`; the
reasons behind the rules are in `LESSONS.md`.

Paths below that start with `rules/`, `src/uac_ai/` or `templates/` are in
`uac_model`; its data files are copied to `data/`. The AI 2.0 field
documentation is not in this repo: `data/uacdocs.yaml` is what was distilled
from it.

Assumed stack: Python 3.12, `uv`, `click`, `pydantic` v2, PyYAML (custom
loader), `pytest`. Transport is the `uac` CLI behind a small interface.

---

## 1. Principles

1. **Lossless core.** `inflate(simplify(x, t), t) == normalize(x)` for every
   object. Readability is a layer on top and never changes the object.
2. **An idiom is used only when lossless**; otherwise the raw UAC field is
   written. Any UAC field name is always legal in a file.
3. **References travel by name, never sysId.**
4. **Compare one-directionally**: "does the controller agree with everything
   the file states?", never "are they equal?".
5. **Never default or guess a value that names another object** (agent,
   credential, calendar, connection, task).
6. **Per-type behavior is data** (templates, rules YAML), one generic engine.
7. **"Not found" ≠ "the call failed".**
8. **Refuse secrets** — a bundle is a file in git.

## 1a. What the Stonebranch AI 2.0 docs give us

The AI component ships hand-written field documentation for creating tasks:
4 skills (create-task, create-agent-task, create-distributed-agent-task,
create-database-task) and 23 per-type agents. Every field is a table row:
`| API field | Display name | Type / Values | Req. | Notes |`. That is
vendor knowledge the OpenAPI spec lacks (0 defaults, 0 enums), and it is the
best basis for compaction because it describes what the **controller**
does, not what one corpus happens to contain.

Distill it once into a shipped data file, `data/uacdocs.yaml` (a small
one-off script plus hand review; not a tool command). Per task type, per
field:

```yaml
common:                       # create-task skill: every task type
  twWaitType:
    category: wait_delay       # UI form category → compact group
    label: Wait To Start       # UI display name
    values: {None: 0, Time: 1, Relative Time: 4, Duration: 2, Seconds: 3}
    default: None
  twWaitTime:
    category: wait_delay
    format: HH:MM
    applies_when: {twWaitType: [Time, Relative Time]}
  outputType:
    values: {STDOUT: 1, STDERR: 2, FILE: 3}
    ui_labels: {Standard Output: STDOUT, Standard Error: STDERR, File: FILE}
taskUnix:
  include: [common, agent_task, distributed_agent_task]
  runAsSudo: {category: command_options, applies_when: {commandOrScript: [Command]}}
rules:                         # cross-field rules the docs state
  exclusive: [[broadcastCluster, agent], [broadcastCluster, agentCluster],
              [waitForOutput, outputFailureOnly]]
  one_of: {agent_target: [agent, agentVar, agentCluster, agentClusterVar,
                          broadcastCluster, broadcastClusterVar]}
  var_pairs_never_both: true
  never_send: ["*VarCheck", "use*Var"]
  read_only: [version, exportReleaseLevel, exportTable, created, createdBy, ...]
```

What each piece is used for in `uac-compose` (these replace corpus statistics
wherever the docs speak):

| docs content | uac-compose use |
|---|---|
| **Default `X`** | primary source of defaults removed on export — portable across controllers, correct by construction because the controller applies the same default on create. `rules/documented.yaml` in `uac_model` already extracted these (561 applied): copy it. |
| **UI categories** (General, Retry Options, Wait/Delay, Time Options → Late Start/Late Finish/Early Finish, Critical Path, Execution Options, Self-Service, Agent Selection, Command vs Script, Command Execution Options, Exit-Code Processing, Automatic Output Retrieval) | the **compact groups** — named as UAC users already see them on the task form. |
| **"applies when" / "required when"** (e.g. `twWaitTime` only for Time/Relative Time; `restrictionPeriod*` only when `executionRestriction` ≠ None; `outputReturnFile` only when `outputReturnType` = FILE; `exitCodes` vs `outputType`+`exitCodeText` by `exitCodeProcessing`; `userEstimatedDuration` with Average Duration) | **candidate** inactive-field rules (§6.5a), measured before use; and **required-when** validation on import. |
| **value maps** `1=\`Command\``, `(UI "…")` | closed enum lists per field: validate on import; normalize an integer id or UI label to the name (the controller accepts all three, GET returns the name — normalizing is what makes compare say `reuse`). |
| **formats** (`DD:HH:MM:SS`, `HH:MM`, `yyyy-MM-dd`, `1,5,22-30`) | validate; the duration idiom (§6.4). |
| **var pairs, never both, never `*VarCheck`/`use*Var`** | validation + the var-pair idiom (§6.4). |
| **agent selection rules** (at least one target; agent+cluster allowed together; broadcast exclusive) | validation. |
| **workflow edge conditions** (`prompts/base/workflow-build.md`: Success default, Failure, Success/Failure, Exit Code + expression, Approved, Approval Required; Failure not valid from Workflow/Timer/Manual; Approved only from Approval) | `after:` vocabulary and validation. |
| **read-only field list** | exclusions (matches `exclusions.yaml`). |

Limits (so learn stays necessary): the docs cover **tasks only** — no trigger,
calendar, credential… fields; no agent doc for `taskUniversal` or
`taskWorkflow`; **no sub-object fields** (notifications, actions, run
criteria). For those, `learn`'s derived templates remain the default source,
under the presence rule (§5.4). A documented default still goes into a
template only where every object of the type has the field, or the round trip
breaks (measured: 5286 → 5268 for `exitCodes`).

A default decides what a file leaves out, so it must mean the same at both
ends of a promotion. **The defaults are the ones uac-compose ships, on every
controller**; a profile never supplies its own. Where the docs speak, the
shipped value is the documented one. Where they do not, it is the value
measured over the corpus -- still one file, still the same everywhere.

`learn` still derives this controller's own defaults, but only to **report**
where they differ from the shipped ones: those are exactly the fields a file
from this controller states instead of leaving out. What `learn` contributes
to reading and writing objects is the Universal Extension map, which is
properly local and names slots rather than defaulting them.

## 2. Layout

```
uac-compose/
  data/                      # read-only, ships in the wheel; copied from uac_model
    registry/types.yaml      # fields, field_types, sub_fields per type
    uacdocs.yaml             # distilled from the AI 2.0 skills/agents (§1a)
    rules/exclusions.yaml groups.yaml guards.yaml oncreate.yaml documented.yaml
    templates/8.0.2.0/       # fallback defaults (+ _sub/, _extensions.json)
  src/uac-compose/
    cli.py                   # the four commands
    paths.py                 # data root; workspace root (~/.uac/compose or UAC_COMPOSE_WORKSPACE)
    registry.py              # load types.yaml; var_pairs(), reference_fields()
    transport.py             # uac CLI: list/get/create/update, ext export/import/get
    learn.py                 # fetch → cache, references, derive
    core/normalize.py simplify.py inflate.py extensions.py derive.py templates.py
    style/uac.py             # UAC names, defaults removed
    style/compact.py         # aliases, idioms, groups, guards
    workflow.py              # members, after/condition, run_if, position
    runcriteria.py           # run_if/skip_if grammar
    yamlio.py                # loader (bool fix, strip comment/create_only), dumper
    compile.py               # file → UAC objects (either style, or mixed)
    emit.py                  # UAC objects → file
    importer.py              # validate, decide, compare, merge, order, oncreate
    freeze.py                # extension export/checksum/import
  tests/
```

The registry and corpus templates are **shipped data**, not generated by the
tool. Regenerate them with a throwaway script if the spec ever changes.

Workspace (per profile = per controller, never shared):
```
<workspace>/<profile>/
  source.json          # release, fetched_at, counts
  cache/<kind>.json
  templates/<release>/  # _extensions.json: this controller's extension map
  references.json      # agents, clusters, credentials, calendars… names only
  extensions/          # frozen zips + manifest.json
```
Profile: `--profile` → `UAC_COMPOSE_PROFILE` → `default`. Read env per call, not at
import. Config errors print as a one-line message, not a traceback.

## 3. Transport (`transport.py`)

Rules that each cost a bug in `uac-ai`:
- **Name argument is a table** (`NAME_ARGS`): default `<group>name`
  (`taskname`, `credentialname`, `customdayname`); exceptions
  `universal-event-template→templatename`, `virtual-resource→resourcename`,
  `business-service→busservicename`, connections→`connectionname`.
- Connections are two levels deep (`uac connection database get`).
- **Absent** only when the controller says `Response Code : 404` + "does not
  exist". `No usage found for argument` = malformed call → raise. Anything
  else → raise with the **first** stderr line.
- `business-service create` needs `name=` as a positional arg beside `--input`.
- Strip the routing key from every payload before writing.

## 4. `uac-compose learn`

1. **Fetch** each kind into `cache/`. Criterion is "does the call return
   complete definitions", not the verb:
   - `list-advanced`: tasks (incl. workflows), triggers, variables.
   - `list`: scripts, event templates, calendars, custom days, credentials,
     business services, virtual resources, the 4 connection kinds.
   - Stamp a routing `type` onto answers that carry none. Credential, virtual
     resource and email connection carry **their own** `type` → move it to
     `_uacType` first, then stamp.
   - Release = the one most objects carry → `source.json`.
2. **References**: agents & clusters (their `list` is a summary) plus names of
   credentials, calendars, event templates, connections, tasks →
   `references.json`. Identity fields only; never `runtimeUser`.
3. **Extension map**: `uac universal-template list` gives each parameter's
   slot (`"Choice Field 3"→choiceField3`), label, choices. Merge observed usage
   over it.
4. **Derive templates** for this profile (§5.4).
5. **Self-check**: round trip every cached object, print `N/N ok` and failures
   by type. Nothing is written on failure except the report.

`--refresh` re-fetches; otherwise learn reuses the cache. Named lookups in
`export`/`import` always fall back to a live `get`, and `import` always reads
live before writing.

## 5. Core engine

### 5.1 normalize
- Drop `exclusions.yaml` keys — root-only (`exportReleaseLevel`, `exportTable`,
  `retainSysIds`, `changeHistory`, `summary`, `version`, `created*`,
  `updated*`, `firstRun`, `lastRun*`, `runCount`, run-time stats,
  `enabled/disabledBy/Time`, `numberOfTasks`, `taskIds`) and any-depth
  (`sysId`). **Exact names only** (`httpVersion` must survive).
- Drop `None, "", [], {}`; keep `0`/`false`; always keep `type`, `name`.
- Drop unset extension slots (keys ⊆ `label, name, nameTitle, valueTitle`).

### 5.2 simplify / inflate
- Drop a field equal to the template default (except `type`, `name`).
- Recurse into a dict iff default is a dict **or** a sub-template path starts
  below it (`should_descend`); lists at a sub-template path diff per item.
- `inflate` is the exact inverse and **imports the same `should_descend`**.

### 5.3 Universal extension slots
`choiceField3: {label, name: action, value: upload-file}` → `action:
upload-file`. Key on `name` (labels repeat); never collapse onto a real UAC
field of the type (`type`, `credentials` collide often); only plain
`{label,name,value}` collapses, table slots stay raw. Boolean slots hold real
booleans.

### 5.4 derive templates (inside `learn`)
Default = most frequent value if support ≥ 0.6 and instances ≥ 3, **and**:
1. field present on **every** instance (else inflate resurrects it —
   why `exitCodes` has no default on Unix);
2. nested shape constant (edge `condition` varies);
3. never `name`, never a reference field (name half of each `x`/`xVar` pair,
   plus `template`, `opswiseGroups`, `calendar`, `connection`…);
4. never `actions.systemOperations.operation`, `observedDayOfWeek`;
5. fallback sub-template `virtualResources: {amount: 1}` if none derived.
Profiles with no templates use the shipped corpus ones.

### 5.5 Coercion on compile
Number ↔ digit-string to the spec's declared type at top level (a string field
written as `100` comes back `'100'` and never matches again). Never coerce
bools.

## 6. The file

### 6.1 Two styles, one compiler
- `--style compact` (default): short types, idioms, group blocks, inactive
  fields hidden.
- `--style uac`: UAC field names and nesting, `type: taskUnix`, defaults
  removed. Nothing else.
- `import` accepts either, or a mix. Invariant:
  `compile(emit(x, compact)) == compile(emit(x, uac)) == simplify(x)`.

### 6.2 Sections and creation order
```yaml
business_services:   # list or mapping = create these; objects state their own
credentials:
variables:
virtual_resources:
database_connections: / peoplesoft_connections: / sap_connections: / email_connections:
custom_days:
calendars:
scripts:
event_templates:
tasks:               # workflows included
triggers:
templates:           # used by like:, never created
create_only: [sections]
```
Created in that order. Inside `tasks:`: file order, stable topological sort
so launched tasks and workflow members come before what references them (a
cycle takes the first waiting item). Never iterate a `set`.

Key = object name (script: `scriptName`). `type:` is resolved per section
(`File Monitor` is a task under `tasks:`, a trigger under `triggers:`);
aliases are mechanical (strip `task`/`trigger`, split camel case) with
overrides for `triggerFm/Um/Em/Tm/Vm/Zm`. Raw names always work.

### 6.3 YAML I/O
- Bool resolver restricted to `true`/`false` (`on: success`!).
- Strip `comment:` and `create_only:` at every depth on load.
- Deterministic dump; quote values that would reload as another type (a bare
  `=` fails to load); `position: [x, y]` in flow style; `120.0` → `120`.

### 6.4 Compact idioms (lossless or raw)
- **Name/value lists** (`variables`, `httpHeaders`, `urlParameters`,
  `providerParameters`): `- instance_name: ${x}` (list, names repeat). Rows
  with extra keys keep UAC shape.
- **Custom fields / event template refs**: value only (controller owns the
  label).
- **`email_on:`** only when the notification is just
  status/lateFinish/connection/template; otherwise raw.
- **`run_if` / `skip_if`**: one-line grammar for `runCriteria`
  (`business day`, `holiday`, `custom day X`, `mon, fri`, `weekdays`,
  `1st business day of month`, `every monday of month`, `${V} != EU`,
  `... and ...`, `... at run time`). Unrenderable → raw.
- **Calendars**: `businessDays: [Monday, …]`, `customDays: [name]`, custom day
  `observed: {Sat: Fri}`. Refuse the wrong weekday vocabulary (custom day
  `Sun`, calendar `Sunday`). `quarters: [Jan 1, Apr 1, Jul 1, Oct 1]` for the
  four quarter starts. Short fixed-word lists are written on one line.
- **Flag lists** (`data/rules/aliases.yaml` `flags:`): a user group permission
  row writes its true `op*` flags as `ops: [create, read, ...]`.
- **Var-pair idiom** (from the docs' pair rule): emit `agent: ${ac}` for a
  stored `agentVar: ${ac}`; compile routes a value containing `${` to the
  `*Var` half, a literal to the plain half. Lossless because UAC never fills
  both halves and no reference name contains `${`; if a stored plain field
  does contain `${`, emit raw. Only for the reference pairs (not
  `systemOperations.task`, whose names may contain variables).
- **Duration idiom**: emit `lfDuration: 1h 49m` for `00:01:49:00`; compile
  accepts `Nd Nh Nm Ns` or the 4-part form and always writes all four
  `DD:HH:MM:SS` parts (controller rejects `00:01:49`). Applies only to the
  fields the docs mark `DD:HH:MM:SS`, never `lf/efOffsetDuration` (plain
  integer + unit).
- **Enum values**: always emitted as the value-map name; compile accepts the
  name, the integer id, or the UI label, and normalizes to the name.
- **Nothing that invents or hides fields**: no `fail_after`-style idiom. SLA is
  written as UAC stores it, just inside its group.

### 6.5 Groups & guards (compact only)
- **Groups follow the UI categories from the docs**, so a UAC user
  recognizes them: `general`, `execution` (Agent Selection + credentials —
  not called `agent`, which is a field name, and 14 task types have no agent
  but still name a credential), `command_options` (Command Execution
  Options; `command`/`script` themselves stay top level), `exit_codes`
  (Exit-Code Processing), `output` (Automatic Output Retrieval), `retry`
  (Retry Options + `retryExitCodes`), `wait` (Wait/Delay), `time`
  (`late_start` / `late_finish` / `early_finish` / estimated), `critical_path`,
  `restriction` (Execution Options), `self_service`. Per-type fields the
  docs list outside any category (e.g. `runAsSudo`) stay at the top level.
  Types/fields the docs don't cover fall back to `uac_model`'s
  `rules/groups.yaml` (triggers: `schedule_time`, `launch`, …; `resources`,
  `actions`).
- Groups are **disjoint**; a block may only hold its own fields (catches
  typos). `variables` mapping = group, list = field.
- `like: name` copies a block or whole object from another object or a
  `templates:` entry.
- **Guards** (§6.5a): hide inactive-branch fields. The measured rules come
  from `uac_model`'s `rules/guards.yaml`; the docs' `applies_when` notes add
  candidates that are measured before use. **Display only**, never in
  simplify — 22 corpus objects carry residue and dropping it would change the
  object. Compare is one-directional, so hidden residue never conflicts.
- `--style uac` has no groups: UAC's own field names minus defaults. Guards
  apply to it too (§6.5a).

### 6.5a Inactive-field rules ("if `lfEnabled` is false, don't show `lf*`")

UAC never clears a field when the feature that owns it is switched off: a
task with `lfEnabled: false` still stores `lfType`, `lfTime`,
`lfDuration`… These are **17.5%** of all simplified fields in the corpus
(7.4 per Unix task). Export hides them.

Rules live in `data/rules/inactive.yaml`, in two lists:

- `rules:` — **measured** (ported from `uac_model`'s `rules/guards.yaml`, each
  with its corpus evidence). Used by export.
- `candidates:` — taken from the AI 2.0 docs' "applies when" notes
  (`commandOrScript: Command` hides `script`; `twWaitType: Duration` hides
  `twWaitTime`; Time/Duration hides the `lfOffset*` fields; …). **Not used
  until measured** and promoted.

```yaml
- when: {lfEnabled: [false, null]}        # all listed guards must match; null = absent
  hide: [lfType, lfTime, lfDayConstraint, lfDuration, lfNthAmount,
         lfOffsetType, lfOffsetPercentage, lfOffsetDuration, lfOffsetDurationUnit]
- when: {lfEnabled: [true], lfType: [Duration, Average Duration]}   # inactive branch counts
  hide: [lfTime, lfDayConstraint, lfNthAmount]                      # 112/112
- when: {outputReturnType: [NONE, null]}  # NOT waitForOutput: that one is live (varies 310x)
  hide: [outputReturnFile, outputReturnNline, outputReturnSline, outputReturnText,
         outputFailureOnly]
- when: {retryMaximum: [0, "0", null]}
  hide: [retryInterval, retryIndefinitely, retrySuppressFailure, retryExitCodes]
- when: {exitCodeProcessing: [Success Output Contains, Failure Output Contains]}
  hide: [exitCodes]
```
Rules are matched on any type that has the fields, since they belong to the
field family, not the type.

Before / after on a real-shaped Unix task:

```yaml
# raw (defaults removed)            # exported
type: taskUnix                       Nightly Load:
name: Nightly Load                     type: Unix
agent: linux-01                        execution: {agent: linux-01}
command: /opt/etl/load.sh              command: /opt/etl/load.sh
exitCodes: '0'                         exit_codes: {exitCodes: '0'}
lsTime: '00:00'        # residue
lfType: Duration       # residue
lfDuration: 00:02:00:00 # residue
twWaitDuration: 00:00:30:00 # residue
```

Load-bearing rules:
1. **Display only.** Never part of `simplify`: 22 corpus objects carry
   residue that is *not* constant (e.g. `lsType: Duration` with late start
   off), so dropping it in the core would change the object and break the
   round trip.
2. **Safe on import because compare is one-directional**: a hidden field is
   simply not stated, so the stored residue never reads as a conflict, and an
   `--update` merge leaves it where it is.
3. **Every rule is measured** before it is added: over the corpus, with the
   guard off, each hidden field must be constant or near-constant (only
   residue, never a live setting). Write the count next to the rule.
4. Default on in **both** styles; `export --show-inactive` turns it off (for
   someone who wants the exact stored state).
5. Import validation uses the same file in reverse for **required-when**
   (`lfEnabled: true` + `lfType: Time` needs `lfTime`) and warns when a file
   states a field its own guard makes inactive (`lfTime` with `lfEnabled:
   false`) — likely a mistake, since it does nothing.

### 6.6 Workflows
- Members under the workflow's `tasks:`; key = handle (normally the task
  name; repeats use `task:`). `alias:` only if the vertex has one.
- Read edges and run criteria **by vertexId, never by name**.
- `after:` (`A`, `[A, B]`, `{A: failure}`) builds edges; `condition:` builds
  `conditionExpression` (absent=AND, `any`=Or, else `A OR (B AND C)`; quote
  names with spaces).
- An edge with a variable condition → emit the whole workflow raw.
- Emit in execution order. `position: [x, y]`; `vertexId:` only when not
  2,3,4… (and then on all members); ids start at 2.
- An undefined member = existing task on the controller; import checks it
  exists.

### 6.7 Secrets
Refuse `runtimePassword`, `runtimePassPhrase`, `runtimeToken`, `defaultPwd`
with a message saying where secrets belong. Never emit a `providerParameters`
list containing a blank (redacted) value.

### 6.8 Business services
List = default membership for objects without `opswiseGroups`; mapping =
create only. Types without `opswiseGroups` (variable, custom day, event
template, business service) are never assigned one. Emit hoists only services
shared by **every** object.

## 7. `uac-compose export`

```
uac-compose export NAME... [--type KIND] [--style compact|uac] [--with-members]
                        [--variablize-agents] [--parallel N] [--export-extensions]
                        [--out FILE]
```
- `--where TEST` (`-W`, repeatable): the `where.py` tests on every match;
  with it, every NAME goes through the live list, and no NAME means `'*'`.
- A NAME holding `*`, `?` or `[` is a wildcard; with `-E` every NAME is a
  regex (`re.search`). Both ignore case. Patterns match a live `uac <group>
  list` of the kinds `-t` allows; `-t` is a comma list of kinds and of
  task/trigger/cluster types (`typealias.to_uac`). Exact names keep the
  `find` path. More than 15 matches: ask on a terminal, else refuse without
  `-y`; `-l` lists and stops. Matches are (name, kind) pairs, so one name in
  two kinds is taken from both, and `-w` walks from every match.
- `--purpose update` (`-P`): every object gets `was: <its name>`, services come
  as a mapping, and no `create_only:` is written: the file is for changing
  these objects where they are, renames included (§8).
- `--export-extensions` (`-d`, `-dex`, `--download-extensions`): after the
  file is written, freeze the extensions its Universal tasks name, as
  `freeze FILE` does, into `<workspace>/<profile>/extensions`.
- Every object is read live, one `uac ... get` each (0.5 s on a real
  controller). `--parallel N` (default 4) reads N at a time, as `freeze`
  does; `--with-members` reads a generation of members at a time. The
  objects, the warnings and the file come out in the same order for every N.
- A plain line per object goes to stderr as it arrives
  (`[12/46] taskUnix 'X'  0.5s`), then `read 46 object(s) in 9.1s`. Under
  `--with-members` the total is what has been found so far. Without `--out`
  the file is on stdout, so the lines never mix into it.
- Look the name up across kinds (task, trigger, script, event template,
  calendar, custom day, credential, variable, virtual resource, business
  service, connections); cache first, live `get` fallback. Same name in two
  kinds → require `--type`.
- `--with-members`: workflow members (recursive), their triggers, monitor
  tasks, calendars → custom days, scripts, event templates, credentials,
  connections, virtual resources.
- Every export marks the sections in `data/rules/create_only.yaml`
  `create_only:` (services, user groups, agent clusters, credentials,
  variables, virtual resources, connections); a `--bundle` export adds the
  Bundle's own excludeOnExistence.
- `--variablize-agents`: every `agent` with an `agentVar` twin (object, action
  rows, step rows) is written as `agent: ${agent_<name>}`, and the file gets a
  variable `agent_<name>` holding the agent. Set the variable per controller
  to move the tasks. Broker fields (`brokerRef` and the like) have no twin and
  stay as they are.
- Pipeline: normalize → simplify → (compact: collapse extensions → idioms →
  groups → guards → workflow) → dump.
- **Acceptance**: any exported file, imported back to the same controller,
  decides `reuse` for every object.

## 8. `uac-compose import`

```
uac-compose import FILE [--dry-run] [--update] [--extensions DIR] [--update-extensions]
                        [--parallel N]
```
- **Rename** (`was: old` on key `new`, per kind): `old` exists and `new` does
  not → `rename`, an update with old's sysId and the new name (needs
  `--update`; create_only refuses it). `new` exists and `old` does not →
  as usual, noted as already renamed. Both exist → conflict, even with
  `--update`. Neither → create, with a note. After compile, every reference
  in the file to a renamed `(kind, old)` points at `new`
  (`references.rename`, the same table as `named`, plus workflow vertices),
  each rewrite a note. What the controller links by sysId follows by itself.
- Progress on stderr, as `export` prints it: what step runs, a line per
  object read (`--parallel N`, default 4) and per workflow member asked for,
  a line per extension installed and per object written, with the time each
  took. Findings list errors before warnings; decisions stay on stdout.
  A Universal task whose extension is neither on the target nor frozen is
  one error, not one per parameter.
1. Load → resolve `like:`/`templates:` → compile → validate.
2. **Validate** (errors): known types; field names vs registry; extension
   parameter names and choice values; sub-object field names (exempt
   `workflowEdges.condition`, `jobSteps`, nested condition/criteria);
   `agent: ${x}` must be `agentVar`, never both halves; secrets;
   `systemOperations` row without `operation`.
   From `uacdocs.yaml`: enum value not in the field's value map;
   bad format (`DD:HH:MM:SS`, `HH:MM`, `yyyy-MM-dd`, exit-code ranges);
   a required-when field missing (`twWaitTime` for Time, `restrictionPeriod`
   for Run/Skip/Hold, `outputReturnFile` for FILE, `exitCodeText` for the
   output-contains modes, `userEstimatedDuration` for Average Duration);
   no execution target on an agent task; broadcast cluster combined with
   agent/cluster; `waitForOutput` + `outputFailureOnly` both true; a workflow
   edge condition not valid for its source type (Failure from a
   Workflow/Timer/Manual, Approved from a non-Approval task).
   **Warnings** (`??`, never fail): unknown agent/credential/calendar/event
   template/launched task — checked against `references.json` **plus what the
   file itself defines**, with close matches.
3. **Extensions** (if `--extensions`): missing on target + frozen zip →
   import; missing, no zip → warn; checksum differs → warn, or overwrite with
   `--update-extensions`.
4. **Decide** each object (live `get`): `create` · `reuse` (agrees) · `update`
   (differs + `--update`) · `conflict` (differs) · `keep` (differs but
   `create_only`, checked before `--update`).
5. **Compare**: stored object collapsed, not simplified; only fields the file
   states; lists of dicts unordered; ignore canvas coordinates.
6. Any `conflict` → print all, write nothing, exit 1. `--dry-run` stops here.
7. **Write** in creation order:
   - create: `inflate` → `oncreate.yaml` fills (`exitCodes: "0"` when
     `exitCodeProcessing` is the exit-code mode or absent) → strip routing
     key / restore `_uacType` → POST. Print each filled field.
   - update: merge file over stored (simplified), dicts merge, lists replace,
     stating one half of an `x`/`xVar` pair clears the other; only within the
     file's own business services; keep stored workflow coordinates → PUT.
8. Print every finding in full, one per line.

## 9. `uac-compose freeze`

```
uac-compose freeze [FILE] [--out DIR] [--refresh] [--parallel 4]
```
- Without FILE: every extension on the profile; with FILE: only those its
  `taskUniversal`/`taskUniversalMonitor` `template:` names (not
  `taskEmail.template`).
- `uac universal-template export` per name → zip; `manifest.json` records a
  **content checksum** (strip `sysId`, `exportReleaseLevel`, icon fields,
  `minReleaseLevel`) — there is no version field.
- Skip unchanged: cheap `get` + checksum; `--refresh` re-exports all.
- Threads for parallelism; one failure never stops the batch.
- Plain progress lines (`[3/27] OK name <checksum16>`), no redrawn bar.

## 9a. `uac-compose apply`

```
uac-compose apply FILE [-n] [-y] [-v] [-j N]
```
A change file: ordered steps that **update, rename or delete** existing
objects. The one thing it creates is a user (`create user`); any other
`create` is refused. Example: `examples/changes.apply.yaml`.
- A step's first key is `<delete|update|rename> <kind>`, the kind any `-t`
  word; its value one name, a list, or a pattern (the export rules: wildcard,
  or `regex: true`; `type:` narrows as `-t`).
- `update`: `set:` (compact fields, `like:` from the file's `templates:`),
  and row operations on child lists by dotted UAC path: `remove` (rows
  matching the given fields), `change` (`where` + `set`), `add` (rows). In
  that order, after `set`; the other rows stay. No match is a warning.
  An `add` row and a `change` `set` are bundle syntax: each goes through
  `compile_object` as a row of that object (short forms such as
  `variables: [- name: value]`), after `compile.apply_like` applies its
  `like:` from `templates:` (the object rules: in order, own fields win).
  `remove` and `where` test stored UAC fields.
- `create user: NAME` + `password_env: VAR` (required) + `set:` (UAC
  UserWsData fields, from `data/openapi.json`) + `groups:` (user group names).
  The password is read from the environment, at plan time to check it is
  set and again at write time; it is never in the file, the plan output or
  a backup (rule 8; `userPassword` is in `validate.SECRETS`). The plan
  refuses a user that exists, a group that does not, and an empty
  variable. `active: true` and `passwordNeedsReset: true` unless `set:` says
  otherwise. The
  write is `uac user create`, then `uac user add-group` for each group. A
  backup skips the new user: it has no earlier state.
- `rename`: `to` (one object), `prefix`, `suffix` or `replace {from, to}`;
  may also carry `set`/rows. An update with the stored sysId: in place.
- `replace task: OLD` with `with: NEW` (must exist) and `in:` (workflow
  names or patterns, default `'*'`): the workflows `list-workflow-list`
  names for OLD (plus those earlier steps changed), within `in:`; the
  vertex, the edge ends (`taskName`) and the run/skip criteria rows (`task`,
  by `vertexId`, sysId kept) all move to NEW (`references.swap_task`); each
  gets NEW in those vertices; vertex ids, places and edges stay. No
  workflow runs OLD → error.
- `where:` on any step, or the chained head `<action>: Kind.find(...).where(...)`
  (`where.py`): field tests that must all hold. `field: wildcard`, `[a, b]`,
  `{not: x}`, `{regex: r}`, `{contains: t}`; chain `=`, `!=`, `~`, `*=`,
  `=[a, b]`. Case ignored; dotted paths reach rows; `type` takes `-t` words.
  Candidates are read live: one `get` each, or one `list-advanced` above 200
  of a kind (measured: 4,973 tasks in 41.1 s). For `replace` it filters the
  workflows.
- Row kinds (`ROW_KINDS`): `email action`, `variable action`, `system action`,
  `abort action`, `snmp action` name the rows of a task's action list.
  `update <row kind>: TASKS` + `where:` (row tests, required) + `set:` and/or
  `edit: {field: {from: REGEX, to: TEXT}}`; `delete <row kind>` removes the
  rows. Only tasks holding a matching row change; none → error. The `where`
  of `change:` / `remove:` uses the same tests.
- Plan first, in order, reading live, over an overlay of what earlier steps
  do; every object must exist, a renamed name must be free, `set` and rows
  pass `validate.static`, and every name a change adds (`references.named`:
  a task a system action launches, a credential...) must exist, live or made
  by an earlier step. Agents and users are left to the controller.
  A task that a workflow still runs is not deleted (`uac task
  list-workflow-list`, 0.4 s, not every workflow read: 48 s on 650; then
  over the overlay; ones the same or an earlier step deletes, or where a
  `replace` swapped it out, do not count). Print the plan (15 per step; `-v` all), ask (no
  terminal: needs `-y`; `-n` stops), then write one at a time. The first
  failed write stops the rest and says how many ran.
- `--backup FILE` (`-B`), after the yes and before the first write: every
  object a change writes, once, as the controller had it before the file
  (`Planner.live`, keyed by the name before any rename: `Change.origin`).
  A backup that fails stops the run with nothing written; an existing FILE
  is refused; `-n` writes none. `--backup-format compose|json`, default from
  the suffix (`.json`). compose: an `export -P update -s standard` bundle
  whose `was:` is each object's name after the file, so `import -u` undoes
  it (update back, rename back by sysId, create the deleted); every field a
  change touches is stated with its old value even when a default (import
  compares only what a file states), and what import cannot undo -- rows
  added, since import never removes one -- is a warning. json: the raw
  objects with `after` (updated / renamed to X / deleted). No secret in
  either (`validate.SECRETS` left out).

## 9b. `uac-compose find`

```
uac-compose find -t KIND [FILTER=VALUE ...] [-W TEST ...] [-p PROFILE]
```
An interactive session (lines from stdin, so a pipe works too) that builds a
filter a change file can use. `find.Session.handle(line)` does one line.
- Start: a kind with `list-advanced` filters (`data/rules/list_filters.yaml`,
  from `uac <group> list-advanced --help`, notes measured live) needs one
  server filter first: one `list_kind(kind, filters)` call, complete
  objects. The session never reads every object. A kind without filters
  lists all of its objects. `-t linux` keeps that type and writes `type:`.
- A line is a test, as `-W` takes it (one test per line, so a regex may hold
  `(`, `)` and `,`), or `where: {...}` in the YAML form. All tests hold.
- Help: `fields [TEXT]` (dotted paths of the matches, normalized: count and
  most common value), `values FIELD`, `show NAME`, `list [all]`, `filters`.
  Session: `tests`, `undo`, `clear`, `from FILTER=VALUE` (list again).
- `step ACTION` prints `- <action> <kind>: '*'` + `type:` + `from:` (the
  server filters) + `where:` (`where.to_yaml`, `parse` reversed; two tests on
  one field are refused). `save FILE [ACTION]` makes a change file or adds
  the step when `steps:` is its last key.
- `from:` on a step (delete, update, rename, row kinds): the candidates are
  `list_kind(kind, from)` in one call, then names, `type:` and `where:`
  narrow them. Unknown filters are refused on read. The answer is the
  controller before the file: an object an earlier step deleted is dropped,
  one it renamed is not found under its new name.

## 10. Tests

- Fixture: `SampleExports/*.zip` (real data — never in the wheel or public).
- Corpus invariants: round trip N/N; `compile(emit(x, style)) == simplify(x)`
  for both styles; grouped == flat; extension collapse reversible;
  run-criteria render/parse reversible.
- Unit tests named for each rule above (bool resolver, vertexId ≥ 2, repeated
  workflow task, raw-workflow fallback, quoted join names, secret refusal,
  twin clearing, NAME_ARGS, 404 vs malformed, create_only vs --update).
- `FakeTransport` for import tests; one opt-in live test: export → import =
  reuse; mutate → conflict → `--update` → reuse.

## 11. Build order

0. Distill `uacdocs.yaml` from the AI 2.0 docs; test that every documented
   field exists in the registry and every documented default is in its own
   value map.
1. Core engine + shipped data → round trip N/N over the zips (test only).
2. `learn` (transport, cache, references, derive).
3. `export --style uac` + `import` (decide/compare/create/update). Acceptance:
   export → import = reuse.
4. Compact style: aliases, pairs, groups, guards, `like:`.
5. Workflows + run criteria.
6. Reference sections (credentials, variables, calendars, scripts, event
   templates, connections, services) + `--with-members`.
7. Validation + warnings.
8. `freeze` + `import --extensions`.

## 12. Copy from `uac_model` (data; read code for behavior only)

AI 2.0 docs (source for `uacdocs.yaml`; not in this repo): the task skills,
the per-type agents, and the workflow-build and exit-code-processing prompts.

Data: `rules/documented.yaml` (defaults already extracted from those docs),
`registry/types.yaml`, `src/uac_ai/simplify/exclusions.yaml`,
`rules/groups.yaml`, `rules/guards.yaml`, `rules/oncreate.yaml`,
`templates/8.0.2.0/`. Reference code: `simplify/differ.py`,
`simplify/extensions.py`, `bootstrap/derive.py`, `bundle.py`, `apply.py`,
`joins.py`, `runcriteria.py`, `pairs.py`, `groups.py`, `ingest/live.py`,
`extensions_freeze.py`.

## 13. This project folder

```
uac-compose/
  README.md                  # what uac-compose is, the 4 commands, where to start
  CLAUDE.md                  # short guidance for the new repo: principles (§1),
                             #   layout, "round trip must stay N/N", data-not-code
  .gitignore                 # workspace/, .env, *.zip, .venv, __pycache__
  docs/
    SPEC.md                  # this document (full spec)
    DEVELOPMENT-PLAN.md      # milestones 0–8 from §11, each with tasks,
                             #   files to create, acceptance criteria, tests
    FORMAT.md                # the .uac.yaml format reference: sections, styles,
                             #   idioms, groups, inactive rules, examples
    LESSONS.md               # pitfall checklist + the "why" behind each rule,
                             #   excerpted from uac_model/CLAUDE.md
  data/                      # seed data, copied from uac_model
    openapi.json
    registry/types.yaml
    rules/exclusions.yaml groups.yaml guards.yaml oncreate.yaml documented.yaml
    rules/inactive.yaml      # §6.5a, initial version from guards.yaml + docs
    templates/8.0.2.0/       # incl. _sub/ and _extensions.json
  examples/
    unix-task.uac.yaml           # same task in both styles, before/after inactive rules
    unix-task.uac.uac.yaml
    workflow.uac.yaml            # members, after/condition, run_if
```

Not copied: `SampleExports/*.zip` (real production data) — `DEVELOPMENT-PLAN.md`
and tests point at a local folder via an env var
(`UAC_COMPOSE_CORPUS`) instead. No code, no `pyproject.toml` (you write it), no
`git init` yet.
