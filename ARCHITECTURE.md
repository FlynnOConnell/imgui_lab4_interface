# Architecture

## Modules

- `app.py` — `Lab4App`, the window. An `imgui_data_loader.FileDialog` with no picker buttons hosts it through the
  `header_draw` / `body_draw` / `footer_draw` slots, as the masknmf launcher does. `body_draw` shows the sign-in
  card until Lab4 accepts the credentials file or the window works offline, then the rail and the current page. The footer shows what blocks the page's action
  (`problems()`), or the last outcome, then Sign out, Quit and the page's action (`label_action()` / `act()`).
- `storage.py` — the `Storage` protocol, `MountedStorage`, `SshStorage` and `Transfer`.
- `catalog.py` — `Catalog`, the SQLite index, and the rules (`classify_folder`, `classify_file`, `KINDS`) that name
  a dataset from its listing.
- `viewers.py` — the command a dataset's kind opens with, and the detached launch.
- `lab4.py` — `Recording`: the Lab4 Recording key, its details, a new subject's fields, and the sidecar dict.
- `database.py` — `Credentials` (the website's credentials file), `Lab4Database` (sign-in, `schema`, `register`,
  `recordings`) and `Schema`, the tables as the database has them now.
- `config.py` — `Settings` and `data_dir()`.

## Threads

Every storage call runs on the app's `ThreadPoolExecutor` via `submit(name, fn, *args)`. `poll()` runs at the top of
each frame and lands finished futures by name (`sign_in`, `check`, `list`, `sidecar`, `attach`, `transfer`, `dest_check`, `picker`,
`register`, `refresh`, `subject`, ...). A destination is checked once per value typed (`destination_state`: exists, new,
or why not); `Transfer.run` makes it with `Storage.mkdir`, one level only. Database calls run there too; each opens
its own connection. A `Transfer`
exposes `done` / `total` / `current` counters the window reads while it runs, and `cancelled` to stop it between
chunks.

## Paths and remote access

A `Storage` takes BioHPC's own posix paths (`/biohpc/data2/...`, the paths Lab4 stores) and maps them to whatever it
talks to: `MountedStorage` maps its base (`/biohpc`) onto a local mount root, `SshStorage` runs commands over `ssh -o BatchMode=yes` and
streams writes through `cat`. The window only ever calls the protocol (`check`, `exists`, `listdir`, `read_text`,
`open_write`), so a remote service backend (http, Globus) is one more class and one more branch in
`open_storage`.

## Lab4 database

The website provisions a Postgres role per lab member (`lab4_u_<gitlab id>`, granted `lab4_user`: select, insert and
update on the `lab4` schema, no delete) and hands out its credentials file. The window signs in with that file and
talks plain SQL, as the website does, rather than importing the `lab4` package.

`Lab4Database.schema` reads columns from `information_schema` and primary and foreign keys from `pg_constraint`
(`information_schema` hides keys on tables the user does not own). A foreign key into a `#lookup` table makes that
column a choice; a subject column that is NOT NULL without a default is required for a new subject. Nothing about
the tables' shape is hardcoded beyond the Recording key, so the dev fixtures' `subject.species` and lab4.tables'
`genotype` / `brain_location` / `dob` / `sex` both work.

`register` inserts ExptGroup, Subject and Session when their key is missing (checked first: NOT NULL fires before
`ON CONFLICT`), then Recording, in one transaction. `data_path` must be under `/biohpc`.

## Catalog

`Catalog.scan(storage, root)` takes one `Storage.walk` (an `os.scandir` walk, or one `find -printf` over ssh), reads
only the sidecars' contents, and classifies bottom-up so a folder knows its subfolders' kinds (a `suite2p-run` is a
folder holding `suite2p-plane`s). Hidden folders and `.zarr` folders are listed but not entered. A scan replaces
every row under its root; the totals of folders above the root go stale until they are scanned themselves.

| table | row per | holds |
|-------|---------|-------|
| `datasets` | file or folder a viewer opens | kind, detail, files, bytes, newest mtime, whether a sidecar is beside it |
| `folders` | folder | files and bytes beneath it, newest mtime, `kinds` json of the datasets beneath it |
| `metadata` | sidecar | the path it describes, recording, subject, session date, experimenter, the full json |
| `scans` | scan root | when, items listed, seconds |

Every table is keyed by `(storage, path)` with `storage` the backend's label, so one file holds several backends.
The schema is plain SQL to carry over to the lab's database server. Browse reads it through `Catalog.describe`.

## Sidecars

`<name>.lab4.json` beside each transferred or attached item: `recording`, `key`, `ui_label`, `weight_g`, `subject`,
`notes`, `extra`, `written_by`, `written_at`, `source`.

## Gotchas

- A selectable reports a click on mouse release but `is_mouse_double_clicked` is true on the second press; rows that
  act on a double-click need `SelectableFlags_.allow_double_click`. `tests/test_browse_clicks.py` drives real clicks
  through the test engine, in a subprocess because the null backend's teardown corrupts imgui for later windows.
- hello_imgui loads Font Awesome glyphs only up to U+F2E0. Icons past it (`ICON_FA_ROTATE`,
  `ICON_FA_RIGHT_FROM_BRACKET`, `ICON_FA_FOLDER_TREE`) render as `?`; pick one below.
- Headless tests run on hello_imgui's null backend, which needs `renderer_has_textures` set in `post_init`.
