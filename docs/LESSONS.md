# Lessons from uac_model

Every rule here was learned from a real failure in `uac-ai`
(the older `uac_model` project, whose `CLAUDE.md` has the long form). Keep this
list open while building `uac-compose`.

## Pitfall checklist

| # | trap | what happens | rule |
|---|---|---|---|
| 1 | YAML 1.1 booleans | `on: success` loads as `True: success`; same for `yes/no/off` | bool resolver accepts only `true`/`false` |
| 2 | bare `=` in a value | YAML 1.1 `value` tag, file will not load | quote by asking YAML, not by pattern |
| 3 | number written into a string field | controller stores `'100'`, file says `100`, never `reuse` again | coerce to the spec's declared type (top level, never bools) |
| 4 | `'True'` in a boolean extension slot | comes back `True`, conflicts forever | boolean slots hold real booleans |
| 5 | iterating a `set` | creation order changes per process; works today, fails tomorrow | keep order in lists |
| 6 | case-insensitive filesystem | writing `Calendar/x` into `calendar/` then pruning deleted 100 fresh files | compare `(st_dev, st_ino)`, not paths |
| 7 | vertex id 1 | controller rejects it | vertex ids start at 2 |
| 8 | workflow edges keyed by name | `Load → Load` collapses, a step disappears | read edges and criteria by vertexId |
| 9 | join name with a space | `APPROVAL - IT` tokenised into three; file would not load | quote names with spaces/parens/keywords |
| 10 | edge with a variable condition | flattened to an unconditional edge (150 edges, 64 workflows) | write that workflow raw |
| 11 | defaulting a reference | a derived default put one agent's **sysId** on every new Unix task | never default a field that names another object |
| 12 | defaulting a sometimes-absent field | `inflate` invents it on objects that never had it (5286 → 5268) | default only fields present on every instance |
| 13 | defaulting a field with varying shape | merges invent keys | no default when nested shape varies |
| 14 | defaulting `systemOperations.operation` | a stated `Launch Task` was simplified away and failed on another controller | never default a payload field that decides meaning |
| 15 | statistical default ≠ controller default | `httpAuth: Basic` added to every Web Service task | prefer documented (vendor) defaults |
| 16 | 404 from a malformed call | wrong name argument → "absent" → create → "duplicate value" | `No usage found for argument` means malformed, not absent |
| 17 | last line of stderr | it is the usage listing | reason = first stderr line |
| 18 | name argument derived from group | `virtual-resource` uses `resourcename`, `business-service` `busservicename` | a table, not a rule |
| 19 | routing `type` sent to the controller | POST carries a field the schema lacks | strip it on every write path (create **and** update) |
| 20 | overwriting a credential's own `type` | lose `Standard`/`Resolvable` | move it to `_uacType` before stamping |
| 21 | blank vault parameter written back | `--update` replaces lists; the blank clears the secret | never emit a parameter list with a redacted value |
| 22 | both halves of a var pair after update | controller keeps the variable, ignores the name, reports `updated` | stating one half clears the other |
| 23 | `*VarCheck` / `use*Var` | `Unrecognized property` | refuse on import, name the `*Var` field |
| 24 | warning about objects defined in the same file | "unknown credential" for one defined two lines up | check references against controller **plus** the file |
| 25 | a validator that rejects valid objects | it gets turned off entirely | measure false positives over the corpus first |
| 26 | idiom that is not lossless | `email_on` dropped recipients on 85 of 143 tasks | an idiom only when every field fits; else raw |
| 27 | idiom that invents a field | `fail_after` synthesised an abort action; emit then swallowed real ones | no idiom may add a field the user did not write |
| 28 | hiding inactive fields in simplify | 22 objects carry non-constant residue; round trip breaks | inactive rules are display only |
| 29 | `outputReturn*` guarded by `waitForOutput` | `outputReturnNline` varies 310 times with waiting off | guard is `outputReturnType: NONE` |
| 30 | business services hoisted as a union | one object's service reached all the others | hoist the intersection |
| 31 | mapping of services implied joining | 74 lines of `opswiseGroups: []` in one export | a mapping only creates |
| 32 | services on types without `opswiseGroups` | tool wrote a file its own validator rejected | never assign / never opt out (variable, customDay, event template) |
| 33 | observed-only extension map | valid parameters reported as invented on a small controller | read the declaration (`universal-template list`), merge observed over it |
| 34 | `taskEmail.template` treated as an extension | wrong object kind | only Universal / Universal Monitor `template` names an extension |
| 35 | redrawn progress bar | looks like a hang in a pipe/log | plain progress lines |
| 36 | env var captured at import | `.env` loaded later never reaches it | read workspace env per call |
| 37 | release from the spec | reads another controller's templates | release from the data (`source.json`) |
| 38 | `exclusions` by prefix | `version` would kill `httpVersion` | exact key names only |

## The invariants

1. `inflate(simplify(x, t), t) == normalize(x)` — all objects, all types.
2. `compile(emit(x, style)) == simplify(x)` — both styles.
3. `compile(grouped) == compile(flat)`.
4. `export` then `import` against the same controller decides `reuse` for
   every object.

A change that lowers a count is a bug in the change, not in the test.

## Facts worth not re-deriving

- The OpenAPI spec has **0 defaults and 0 enums** (0 of 7,469 properties).
  Defaults come from the AI 2.0 docs and from the data; enums from the docs.
- The discriminator mappings in the spec are empty; read each subtype's
  `xml.name`. `indesca` is the legacy name of `taskUcmd`.
- Workflows reference tasks by **name**; triggers name their tasks and
  monitors by **name**. `taskIds` duplicates `tasks` by sysId — excluded.
- `numberOfTasks` equals the vertex count — excluded.
- Run statistics (`runTime`, `avg/max/minRunTime`…) are not definition.
- `list-advanced` answers in the export shape; `list` does too for scripts,
  calendars, custom days, credentials, business services, event templates.
  Agents and clusters' `list` is a summary.
- The controller returns no credential secrets, ever.
- Custom-field and event-template-reference labels are overwritten by the
  controller (verified live): write the value only.
- `holiday`/`period` on a custom day restate `category` and are derived by
  the controller.
- `import` of a Universal Template upserts by name; there is no version field.

## A default is a promise between two controllers

`uac-compose` once derived its defaults from each controller it learned, and used
them to decide what a file could leave out. A real promotion then moved a SAP
task from one controller to another and changed it: the source dropped
`sapView` because *its* default was Legacy View, and the target filled in
Enhanced View because *its* default was that. No conflict was raised, no
warning printed, and 3 more objects went the same way. A learned default is a
statistic about one controller. A file that leaves a field out against one is
only readable on the controller it came from -- the opposite of what a
promotion file is for.

So: **the defaults are the ones uac-compose ships, on every controller.** Where the
vendor documents a default, the shipped value is the vendor's (560 values
confirmed, 2 spelt differently in prose; a test holds this). Where the docs
say nothing, the value is still one file, still the same at both ends.

What a controller *is* asked about is its Universal Extension map: which
templates it has, and which slot each parameter sits in. That is genuinely
local, and it names fields rather than defaulting them.

A controller that habitually disagrees with a shipped default is not
overruled and not hidden: `uac-compose learn` lists every such field, and a file
exported from it **states** them rather than leaving them out. The file gets
a little longer (2.5% on the demo bundle) and means the same thing anywhere.

## A switch that is off on the target

A file may state a value and leave its switch out, because the switch holds
the default. `body: Approved` travels; `bodyFilter: Contains` does not,
because `Contains` is the default. If someone has since set `bodyFilter` to
`None` on the target, UAC discards `body` on the way in, and the same
difference is reported on every run, for ever.

The switch belongs to the target. An import does not change it. So `import`
says once that the value will not be stored, and stops calling it a conflict:
`data/rules/guards.yaml`, read against the **stored** object only.

Measured before the rule was written: 13 corpus Email Monitors, 31 switches
off, and in all 31 the governed value is empty -- the controller always clears
it. `bodyBeginMarker` and `bodyEndMarker` are **not** in the rule: 1 object
keeps both while `bodyFilter` is `None`, and no field in the corpus predicts
which monitors hold markers. Why the controller sometimes refuses to store
them is still unexplained.

## The cache says which kind, never what the object holds

`export` used to read the object out of the `learn` cache and only ask the
controller when the name was not cached. A user exported `Approval Task -
Copy` and got the task as it had been at the last `learn`: the version it was
copied from, four named approvers instead of the group it now carries. The
file looked perfectly normal. Nothing warned.

A bundle file is a promise about a controller, so its content comes from that
controller. The cache still resolves the **kind** of a name, which is free and
saves probing fifteen kinds in turn; it never supplies the content.

Cost, measured: one `get` is under a second, and a 136-object Bundle export
takes 75s. That same live read caught a second stale object in the demo
bundle -- a workflow vertex renamed since the last `learn`.

An object the cache has and the controller does not is still written, and
said out loud: `tasks/T: not on the controller; wrote the cached copy`.

## The extension map must describe the controller as it will be

A Universal task is validated against the target's extension map. When the
target has never had that extension, every parameter is refused on its own
line -- 17 lines for one task -- and not one of them says the real cause.

Worse, the cause was often not even true. `import --extensions` had installed
`CS AWS S3` on the target and left the stored map alone, so the extension
*was* there and yarb did not know.

So `import` re-reads `universal-template list` before it validates, and saves
what it reads. One call: 1.8s for 30 extensions, 4.8s for 276. With
`--extensions DIR` the zips in that folder are read too (`template.json`
declares exactly what a controller declares), because an extension this run is
about to install is not on the target yet.

`uac-compose show <extension>` answers the question those 17 lines raise:
what does this controller know about this extension? It reads the profile's
map, falls back to the controller, and suggests near names when there is no
such extension.

## Agent clusters and user groups: ask the controller, not the statistics

Neither kind is in the corpus or in the vendor documentation, so the rules
were measured on live controllers (139 objects on one, 6 on the other), and
the defaults were asked of the controller itself: an object created with a
name only on a test controller, read back, and deleted. Every cluster type
gets the same 8 defaults; a user group gets 1. A permission row cannot be
created bare -- the controller demands one operation and a business-service
scope -- so its template holds only the 6 fields it filled in around those.

What was verified live, each by a create on the test controller:

- A member given by name alone is accepted, and the controller fills in the
  agent's `type`, the user's display `name`, the role's `description`. So
  the file writes plain lists of names.
- A group naming 7 users the controller lacks, and a cluster naming 8 agents
  it lacks, are refused whole -- with "Create Agent Cluster failed." and
  nothing more for the cluster. The same cluster with no agents, or one real
  one, is created. So an unknown member is an error before anything is
  written (`refused_when_missing` in data/rules/references.yaml), not a
  warning that lets an import stop half-way.
- `role` values are not universal: `ops_user` does not exist.

Over all 145 live objects: `inflate(simplify(x)) == normalize(x)` and
`compile(emit(x)) == simplify(x)` in every style, no fallback to raw rows;
export then import into the source decides `reuse` for all 139; a promotion
to the test controller created both, re-import decided `reuse`, and export
from the target gave back the identical file.

Cluster state (`currentCount`, `lastAgentUsed`, `suspended`...) and each
member's suspension are excluded as runtime: `suspend` and `resume` are
commands, not fields a promotion should carry. No other type uses the names.
