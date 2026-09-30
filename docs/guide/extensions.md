# Universal Extensions

A `Universal` task names its extension with `template:`, and that extension has
to be installed on the target first — nothing about the task creates it.

```bash
# on the source controller: save each extension it needs as a zip
uac-compose freeze nightly.uac.yaml --profile source --out ./extensions
#   ./extensions/AWS-S3.zip, ./extensions/manifest.json (name, checksum, release)

# on the target: install what is missing, then apply the objects
uac-compose import nightly.uac.yaml --profile target --extensions ./extensions
```

`--extensions` takes any folder holding those zips and their `manifest.json`.
An extension the target already has, and that matches, is left alone; one that
differs is reported and kept, unless `--update-extensions` says to replace it.
Nothing is installed until every object in the file has been decided, so a
conflict still means nothing was written.

`uac-compose freeze` with no file saves every extension on the profile; `--out`
defaults to `<workspace>/<profile>/extensions`.

`export --export-extensions` (`-d`, `-dex`, or `--download-extensions`) does
the freeze in the same run: after the file is written, it saves the extensions
the exported tasks name into `<workspace>/<profile>/extensions`.

**An extension is downloaded again only when it has actually changed.** A
Universal Extension has no version field, so `manifest.json` keeps two
checksums per extension, taken from what the controller answers:

| checksum | over |
|---|---|
| `scripts_checksum` | the code: `script`, `scriptUnix`, `scriptWindows` |
| `definition_checksum` | everything else: fields, choices, labels, commands |

Each run asks the controller for the extension (a cheap call, not the export
download) and compares both. If they match, the zip on disk is kept and the
line says `unchanged`. If not, it is exported again and the line says which
half moved — `(code changed)` or `(definition changed)`. `sysId`, the export
release and the other bookkeeping the controller stamps on are in neither
checksum, so re-exporting on the source does not look like a change.
`--refresh` exports everything regardless. The manifest also keeps a
`metadata` block (`extension`, `label`, `minReleaseLevel`, …) so you can read
what was frozen without opening the zip.

If some extensions fail — a shared controller can stop answering under a long
run — the ones that worked are still recorded. Run the same command again to
retry only the failures.

Two more things a long run needs. The manifest is rewritten **as each export
lands**, so a run that is stopped or killed keeps what it already downloaded
and the next run carries on from there. And one folder takes one freeze at a
time: a second run into the same folder is refused, because each run holds the
manifest in memory and the later save would drop the earlier one's rows.
