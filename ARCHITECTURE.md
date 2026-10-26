# Architecture

## Modules

- `app.py`: `Lab4App`, the window. An `imgui_data_loader.FileDialog` hosts it through `header_draw`, `body_draw`
  and `footer_draw`. The body shows the sign-in card until Lab4 accepts the credentials (or the user works
  offline), then the rail and the current page. The footer shows why the page's action is blocked (`problems()`)
  or the last message, then Sign out, Quit and the action (`label_action()` / `act()`).
- `storage.py`: the `Storage` protocol, `MountedStorage`, `SshStorage`, `Transfer`, and `open_machine`, the storage
  for one of `Settings.machines`.
- `catalog.py`: `Catalog`, the SQLite index, and `classify_folder`, `classify_file` and `KINDS`, which name a
  dataset from its listing. `scan_machines` scans every machine's roots for the Results page; `main` is `lab4-scan`,
  the same headless.
- `slices.py`: `SliceDatabase`, the human slice tables in SQLite. `patients`, `slices` and `assays` join on
  `patient_id` and `slice_id`; renames cascade and parents with children cannot be deleted. `files` is an index of
  everything under `ROOT`. `COLUMNS` types every editable column. Rows have an `owner`, and `can_edit` (owner or
  `ADMIN`) is checked on every update and delete. `match_files` finds a slice's files from its `file_match`. A file
  from another `VERSION_SCHEMA` is renamed aside, not dropped.
- `viewers.py`: the command each dataset kind opens with, and the detached launch.
- `lab4.py`: `Recording`, the Lab4 key, its details, a new subject's fields, and the sidecar.
- `database.py`: `Credentials`, `Lab4Database` (sign-in, `schema`, `register`, `recordings`) and `Schema`, the
  tables as they are now.
- `processing.py`: `list_units` reads a `.mesc`'s units with h5py, `sbatch_command` builds one job per unit running
  `pml_utilities/hpc/asap7/run_asap7.sh`, and `submit` checks the files landed whole first. Jobs are kept in
  `jobs.json`.
- `config.py`: `Settings`, including `machines`, the computers Results catalogs, and `data_dir()`.

## Threads

Every storage and database call runs on a worker through `submit(name, fn, *args)`. Names in `NAMES_LONG` (transfer,
job submission, scans, the file index, downloads) run on `executor_long`, a thread each, and the rest on `executor`,
so a listing or a destination check never waits behind a long job. `poll()` runs at the top of each frame and handles
finished futures by name (`sign_in`, `check`, `list`, `transfer`, ...). Each database call opens its own connection.
Send measures the sources on a worker (`sizes`) and opens its dialog when that lands. One `files_index` runs at a
time; if the storage changed while it ran, it runs again. A `Transfer` has `done`, `total` and `current` counters the
window reads while it runs, and `cancelled` to stop it between chunks.

The destination is checked once per value typed (`destination_state`: exists, new, or why not). `Transfer.run`
makes at most one new folder.

Processing is chained in `poll()`: `act` fixes the jobs when Send is pressed, and a `transfer` that finishes without
an error submits them as `process`. A failed or cancelled transfer submits nothing. Jobs always go through
`SshStorage.run` on `Settings.processing_host`, whatever the storage backend.

## Paths and remote access

A `Storage` takes BioHPC paths (`/biohpc/data2/...`, the paths Lab4 stores), or a machine's own. `MountedStorage`
maps its base (`/biohpc`) onto a local mount root. `SshStorage` runs commands over `ssh -o BatchMode=yes`. Outside
Windows, whose ssh cannot, commands share one connection per host (`ControlMaster`, socket `~/.ssh/lab4-%C`), so
only the first logs in. One write streams through `cat`; a Transfer streams every file through one `tar -xmf -`
(`open_tree`), so a folder of a thousand files is one login, not a thousand. The tar holds only files, since GNU
tar resets the permissions of any existing folder it carries. The window only calls the protocol (`check`, `exists`,
`listdir`, `read_text`, `open_write`, ...), so another backend (http, Globus) is one more class and one more branch
in `open_storage`.

## Lab4 database

The website makes a Postgres role per lab member (`lab4_u_<gitlab id>`, granted `lab4_user`: select, insert and
update on the `lab4` schema, no delete) and hands out its credentials file. The window signs in with that file and
uses plain SQL, as the website does, instead of importing the `lab4` package.

`Lab4Database.schema` reads columns from `information_schema`, and primary and foreign keys from `pg_constraint`
(`information_schema` hides keys on tables the user does not own). A foreign key into a `#lookup` table makes that
column a choice. A subject column that is NOT NULL without a default is required for a new subject. Only the
Recording key is hardcoded, so both the dev fixtures' and `lab4.tables`' subject columns work.

`register` inserts ExptGroup, Subject and Session when their key is missing (checked first, since NOT NULL fires
before `ON CONFLICT`), then Recording, in one transaction. `data_path` must be under `/biohpc`.

## Catalog

`Catalog.scan(storage, root)` takes one `Storage.walk` (`os.scandir`, or one `find -printf` over ssh), reads only
the sidecars, run manifests and failed runs' logs (`read_texts`, one ssh call per 200 files), and the headers of
`.mesc` files it can open in place. It classifies bottom-up so a folder knows its subfolders' kinds (a `suite2p-run`
holds `suite2p-plane`s). Hidden and `.zarr` folders are listed but not entered, except `.vnoiser.zarr` runs, which
hold figures and tables. A scan replaces every row under its root; folders above the root keep stale totals until
they are scanned.

| table | one row per | holds |
|-------|-------------|-------|
| `datasets` | file or folder a viewer opens | kind, detail, files, bytes, newest mtime, whether it has a sidecar |
| `folders` | folder | files and bytes below it, newest mtime, `kinds` json of the datasets below it |
| `metadata` | sidecar | the path it describes, recording, subject, session date, experimenter, the full json |
| `units` | `.mesc` unit, `.mesc` whose headers were not read, or exported unit tiff | recording, role, method, frames, frame rate |
| `runs` | masknmf run (a `config.json` manifest) or vnoiser run | pipeline, status, start, input, frame rate, last error, log, recording, unit |
| `results` | figure, table or movie | size, mtime, its run, recording, unit |
| `scans` | scan root | when, items listed, seconds |

Every table is keyed by `(storage, path)`, with `storage` the backend's label or the machine's name, so one file
holds several backends.

`link` names each run's and result's recording and unit across every storage (`match_unit`), and `overview` builds
the Results rows, one per recording and unit. A machine that cannot be reached keeps its last scan's rows.

## Writes and the confirm dialog

`Storage` never deletes, moves or renames; `open_write`, `open_tree` and `mkdir` are its only writes, used by
`Transfer.run` and `write_text`. The window never calls them directly: `act()` (Send through `confirm_send`) and the slice page's Delete and Reset set
`self.confirm` (title, lines, button, and the call to run), and `draw_body` shows it as a modal that runs the call
only on its button. Downloads (`storage.download`) go through the same modal and write only under `cache/`, through
a `.part` file. Export uses the system save dialog. `tests/test_confirm.py` checks that each of these leaves files
alone until confirmed.

## Sidecars

`<name>.lab4.json` beside each transferred or attached item: `recording`, `key`, `ui_label`, `weight_g`, `subject`,
`notes`, `extra`, `written_by`, `written_at`, `source`.

## Gotchas

- A selectable reports a click on mouse release, but `is_mouse_double_clicked` is true on the second press. Rows
  that act on a double-click need `SelectableFlags_.allow_double_click`. `tests/test_browse_clicks.py` drives real
  clicks through the test engine in a subprocess, because the null backend's teardown breaks imgui for later windows.
- `set_keyboard_focus_here` activates a text box a frame late and selects all of it, so characters typed in that
  frame are lost. The slice table's edit boxes hold them (`hold_chars`) and `place_caret` adds them after placing
  the caret on activation. imgui moves focus on Tab unless the key has an owner, so the table claims Tab
  (`set_key_owner`) while a cell is outlined.
- hello_imgui loads Font Awesome glyphs only up to U+F2E0. Icons past it (`ICON_FA_ROTATE`,
  `ICON_FA_RIGHT_FROM_BRACKET`, `ICON_FA_FOLDER_TREE`) render as `?`.
- Headless tests run on hello_imgui's null backend, which needs `renderer_has_textures` set in `post_init`.
