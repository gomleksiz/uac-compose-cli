# uac-compose — development plan

Nine milestones, each shippable and each ending in a test that proves it.
Do them in order: every later milestone assumes the invariant of the earlier
ones still holds. `SPEC.md` section numbers are given as `§n`.

Conventions for the whole project:

- Python 3.12, `uv`, `click`, `pydantic` v2, PyYAML, `pytest`, `ruff`,
  `mypy --strict`.
- `make check` = ruff + mypy + fast tests. `make test-corpus` = the slow
  tests over the real exports. Run both before calling a milestone done.
- The corpus is **not** in this repo. Tests read it from `UAC_COMPOSE_CORPUS`
  (a local folder) and skip if it is absent.
  It is real production data: never copy it here, never paste object
  contents into issues or commits.
- A live controller is used only by tests marked `live`, opt-in via
  `UAC_COMPOSE_LIVE_PROFILE`.

---

## Milestone 0 — project skeleton and `uacdocs.yaml`

**Goal:** a package that installs, and the vendor field knowledge as data.

Tasks
1. `pyproject.toml` (package `uac-compose`, console script `uac-compose`), `Makefile`
   (`check`, `test-corpus`, `check-all`), `src/uac-compose/__init__.py`,
   `src/uac-compose/paths.py` (data root vs workspace root, `UAC_COMPOSE_WORKSPACE` read per
   call), `src/uac-compose/cli.py` with the four commands as stubs.
2. Package `data/` into the wheel (`uac-compose/_data`), so `paths.DATA` works from a
   checkout and from an install.
3. Write a throwaway script (not a CLI command; neither it nor the AI 2.0
   docs are in this repo now) that parses the tables in the skills and
   agents of the AI 2.0 docs:
   - rows `| API field | Display name | Type / Values | Req. | Notes |`
   - section headings `## Category: …` → `category`
   - value maps `` 1=`Command` `` → `values`, `(UI "…")` → `ui_labels`
   - `Default \`X\`` → `default`
   - formats `DD:HH:MM:SS`, `HH:MM`, `yyyy-MM-dd`, `1,5,22-30` → `format`
   - the `skills:` frontmatter of each agent → `include`
   Output `data/uacdocs.yaml`. **Then review it by hand** and add
   `applies_when` / `required_when` from the Notes column (these are prose;
   do not try to parse them automatically). Add the cross-field `rules:` block
   (§1a).
4. `src/uac-compose/uacdocs.py`: load it, resolve `include`, answer
   `field(type, name)`, `values(type, name)`, `default(type, name)`,
   `category(type, name)`.

Acceptance / tests
- `pip install -e .` and `uac-compose --help` lists `learn export import freeze`.
- Every field named in `uacdocs.yaml` exists in `data/registry/types.yaml`
  for that type (or in `common` for every type that declares it).
- Every documented default is one of its field's value-map names.
- Every documented default agrees with `data/rules/documented.yaml` (that
  file was extracted from the same docs by `uac_model`; a disagreement is a
  parsing bug in one of them).

---

## Milestone 1 — the lossless core

**Goal:** `inflate(simplify(x, t), t) == normalize(x)` for every corpus object.

Tasks
1. `registry.py`: load `types.yaml` → `TypeEntry(type, kind, fields,
   field_types, sub_fields, export_table)`; `var_pairs(type)`;
   `reference_fields(type)` (§5).
2. `envelope.py`: `ObjectEnvelope(type, name, obj, release)`; `routing.py`:
   `UAC_TYPE_KEY = "_uacType"`, route by `exportTable`.
3. `ingest/archive.py`: read the JSON zips; dedupe by content hash (tasks and
   workflows archives overlap — 5,909 files, 5,286 distinct objects).
4. `core/normalize.py` (§5.1) with `data/rules/exclusions.yaml`.
5. `core/templates.py`: `TemplateSet(release)` loads `<type>.json` and
   `_sub/<path>.json`; `sub_templates()` adds `FALLBACK_SUB_TEMPLATES`.
6. `core/simplify.py`, `core/inflate.py` sharing `should_descend` (§5.2).
7. `core/extensions.py`: collapse/expand Universal slots with
   `_extensions.json` (§5.3).

Acceptance / tests
- `test_roundtrip_corpus`: 5,286 / 5,286 with the shipped templates.
- `test_should_descend_is_shared`: inflate imports the function, not a copy.
- `test_exclusions_are_exact_names`: `httpVersion` survives, `version` goes.
- `test_unset_slot_dropped`, `test_zero_and_false_kept`.
- `test_collapse_never_shadows_real_field`, `test_table_slot_not_collapsed`,
  `test_collapse_keys_on_name_not_label`.

Reference code in `uac_model`: `src/uac_ai/simplify/differ.py`,
`simplify/inflater.py`, `simplify/extensions.py`,
`bootstrap/templates.py`, `ingest/json_archive.py`.

---

## Milestone 2 — `uac-compose learn`

**Goal:** one command makes a controller usable offline.

Tasks
1. `transport.py`: `Transport` protocol and `UacCli` implementation (§3):
   `NAME_ARGS` table, two-level connection groups, 404 vs malformed vs
   error, first-stderr-line reason, business-service create quirk.
   `FakeTransport` (in-memory dict) for tests.
2. `learn.py` fetch (§4.1): `SOURCES` table (kind, group, verb, stamp,
   keeps_type_at); stamp routing key on disk; move a credential's /
   virtual resource's / email connection's own `type` to `_uacType`;
   release = the one most objects carry; write `source.json`.
3. References (§4.2) → `references.json` (identity fields only).
4. Extension map from `universal-template list`, observed usage merged over it
   (§4.3).
5. `core/derive.py` (§5.4) with every gate; apply `documented.yaml` /
   `uacdocs.yaml` defaults only where the presence gate passes; print what was
   withheld.
6. Self-check: round trip every cached object, print `N/N`.

Acceptance / tests
- Against the corpus as if it were a profile: derived templates reproduce
  5,286 / 5,286.
- `test_reference_never_defaulted` (a corpus where every Unix task shares an
  agent still gets no `agent` default).
- `test_partial_field_gets_no_default` (`exitCodes` on Unix).
- `test_varying_shape_gets_no_default` (workflow edge `condition`).
- `test_name_arg_table`, `test_404_is_absent`,
  `test_no_usage_found_is_not_absent`, `test_error_reason_is_first_line`.
- `test_credential_type_moved_not_overwritten`.
- Live (opt-in): `uac-compose learn --profile $UAC_COMPOSE_LIVE_PROFILE` finishes and
  prints `N/N ok`.

Reference: `uac_model/src/uac_ai/ingest/live.py`, `bootstrap/derive.py`,
`references.py`, `documented.py`.

---

## Milestone 3 — `export --style uac` and `import`

**Goal:** the smallest useful loop: export a task, import it back, `reuse`.

Tasks
1. `yamlio.py` (§6.3): loader with bool resolver restricted to `true/false`;
   strip `comment:`/`create_only:` at every depth; deterministic dumper with
   type-safe quoting.
2. `style/uac.py`: object → `simplify` → `{name: body}` under its section.
   Apply inactive-field rules (§6.5a) unless `--show-inactive`.
3. `compile.py` for the uac style: section + key + body → UAC object;
   `_as_declared` coercion (§5.5); enum normalization (id / UI label → name)
   from `uacdocs.yaml`.
4. `importer.py`:
   - `decide`: create / reuse / update / conflict / keep (§8.4)
   - `compare`: stored object collapsed-not-simplified, one-directional,
     unordered lists of dicts (§8.5)
   - create: inflate → `oncreate.yaml` fills → strip routing key → POST
   - update: merge over stored in simplified form, var-twin clearing,
     business-service fence → PUT
   - creation order across sections (§6.2); iterate lists, never sets
   - `--dry-run`, `--update`; exit 1 on any conflict and write nothing.
5. `export NAME --type K --style uac --out F` with cache-then-live lookup.

Acceptance / tests
- `test_emit_compile_identity_uac`: `compile(emit(x, "uac")) == simplify(x)`
  for every corpus object.
- `test_on_is_not_true` (YAML 1.1 booleans), `test_equals_sign_quoted`.
- With `FakeTransport` seeded from the corpus: every exported object decides
  `reuse`.
- `test_restated_default_is_reuse`, `test_list_order_ignored`,
  `test_create_only_beats_update`, `test_update_clears_other_twin`,
  `test_oncreate_exit_codes_only_in_exit_code_mode`,
  `test_oncreate_not_on_update_not_in_compare`.
- Live (opt-in): export a real task, import → `reuse`; change one field in the
  file → `conflict`; `--update` → `updated`; import again → `reuse`.

---

## Milestone 4 — compact style

**Goal:** the readable form, compiling to exactly the same objects.

Tasks
1. `style/compact.py` + `typealias.py`: short types resolved per section,
   overrides for `triggerFm/Um/Em/Tm/Vm/Zm`, raw names still accepted.
2. Groups from UI categories (§6.5); `like:` and `templates:`; a block rejects
   fields that are not its own.
3. Idioms (§6.4): name/value lists, custom fields, template refs, var pairs,
   durations, enum names, `email_on` (lossless only).
4. Inactive-field rules shared with the uac style.

Acceptance / tests
- `test_emit_compile_identity_compact` over the whole corpus.
- `test_grouped_equals_flat`.
- `test_group_block_rejects_foreign_field` (`retryIntervl` inside `retry:`).
- `test_variables_mapping_is_group_list_is_field`.
- `test_var_pair_idiom_roundtrip`, `test_duration_idiom_four_parts`,
  `test_duration_idiom_not_on_offset_fields`,
  `test_enum_id_and_ui_label_normalized`.
- `test_email_on_only_when_lossless` (a notification with recipients stays
  raw).

---

## Milestone 5 — workflows and run criteria

Tasks
1. `workflow.py` (§6.6): handles, `task:`/`alias:`, edges and criteria read by
   vertexId, `after:`, `condition:` parse/render with quoting, execution
   order, `position`, `straight`, `vertexId` (starting at 2), raw fallback for
   variable-condition edges, undefined member = existing task.
2. `runcriteria.py` (§6.4): grammar, `render` returns `None` when it cannot
   say something.
3. Edge condition vocabulary and source-type restrictions from
   the workflow-build prompt of the AI 2.0 docs.

Acceptance / tests
- Corpus: every recompilable workflow emits and compiles to the same graph;
  all 1,548 run criteria render and parse back.
- `test_repeated_task_keeps_both_vertices` (`Load → Load`).
- `test_variable_condition_edge_falls_back_to_raw`.
- `test_join_name_with_space_is_quoted`.
- `test_vertex_id_one_rejected`.
- `test_members_emitted_in_execution_order`.

Reference: `uac_model/src/uac_ai/joins.py`, `runcriteria.py`, workflow parts
of `bundle.py`.

---

## Milestone 6 — the other sections and `--with-members`

Tasks
1. Sections (§6.2, §6.7, §6.8): business services (list vs mapping),
   credentials (refuse secrets; redacted provider params never emitted),
   variables, virtual resources, the four connections, custom days and
   calendars (two weekday vocabularies, `observed`, residue by `ctype`),
   scripts (closed `type` list), event templates.
2. Routing `type` vs UAC's own `type` (`_uacType`) on the three singleton
   kinds.
3. `export --with-members` (§7): members, triggers, monitors, calendars →
   custom days, scripts, event templates, credentials, connections, virtual
   resources. Triggers before calendars.
4. Name lookup across kinds; ambiguous name → `--type` required.

Acceptance / tests
- Every cached credential, variable, calendar, custom day, event template
  exports and decides `reuse`.
- `test_secret_field_refused`, `test_blank_provider_parameter_not_emitted`.
- `test_business_service_mapping_does_not_join`,
  `test_services_hoist_intersection_not_union`,
  `test_no_services_on_types_without_opswise_groups`.
- `test_calendar_rejects_short_weekday`, `test_custom_day_rejects_long_weekday`.

---

## Milestone 7 — validation and warnings

Tasks
1. Rungs from §8.2 as independent functions returning findings.
2. Doc-driven rules from `uacdocs.yaml`: enum values, formats,
   required-when, execution target, exclusive pairs, `*VarCheck` refusal,
   edge-condition validity, "field stated but inactive" warning.
3. Reference warnings against `references.json` **plus** the file's own
   definitions, with close matches.
4. Output: every finding in full, one per line, `break_on_hyphens=False`.

Acceptance / tests
- Every file exported from the corpus validates clean (a rung that rejects
  valid objects gets turned off — measure false positives over the corpus
  before shipping each rung).
- One test per rung, named for the failure it catches.
- `test_warning_knows_objects_defined_in_same_file`.

---

## Milestone 8 — `uac-compose freeze` and `import --extensions`

Tasks
1. `freeze.py` (§9): export zips, content checksum (strip bookkeeping),
   `archive_checksum` informational, manifest, skip unchanged via `get`,
   `--refresh`, `--parallel`, plain progress lines.
2. `import --extensions DIR`: runs before objects; missing + zip → import;
   missing + none → warn; differs → warn or `--update-extensions`.
3. Only `taskUniversal`/`taskUniversalMonitor.template` names an extension.

Acceptance / tests
- `test_checksum_stable_across_exports` (strip list).
- `test_freeze_one_never_raises`.
- `test_email_template_is_not_an_extension`.
- Live (opt-in): freeze one extension, import into a controller without it.

---

## Done means

- `make check-all` green.
- Corpus round trip 5,286 / 5,286; emit→compile identity in both styles.
- Live loop: `learn` → `export --with-members` a real workflow → `import` into
  the same controller = all `reuse`.
