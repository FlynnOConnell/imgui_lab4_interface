# Architecture

## Modules

- `app.py` — `Lab4App`, the window. An `imgui_data_loader.FileDialog` with no picker buttons hosts it through the
  `header_draw` / `body_draw` / `footer_draw` slots, as the masknmf launcher does. `body_draw` shows the sign-in
  card until someone signs in, then the rail and the current page. The footer shows what blocks the page's action
  (`problems()`), or the last outcome, then Sign out, Quit and the page's action (`label_action()` / `act()`).
- `storage.py` — the `Storage` protocol, `MountedStorage`, `SshStorage` and `Transfer`.
- `catalog.py` — `Catalog`, the SQLite index, and the rules (`classify_folder`, `classify_file`, `KINDS`) that name
  a dataset from its listing.
- `viewers.py` — the command a dataset's kind opens with, and the detached launch.
- `lab4.py` — `Recording`: the DataJoint key, acquisition fields, and the sidecar dict.
- `auth.py` — `sign_in`, the demo gate.
- `config.py` — `Settings` and `data_dir()`.

## Threads

Every storage call runs on the app's `ThreadPoolExecutor` via `submit(name, fn, *args)`. `poll()` runs at the top of
each frame and lands finished futures by name (`check`, `list`, `sidecar`, `attach`, `transfer`). A `Transfer`
exposes `done` / `total` / `current` counters the window reads while it runs, and `cancelled` to stop it between
chunks.

## Paths and remote access

A `Storage` takes BioHPC's own posix paths (`/archive/...`, `/work/...`) and maps them to whatever it talks to:
`MountedStorage` joins them under a local mount root, `SshStorage` runs commands over `ssh -o BatchMode=yes` and
streams writes through `cat`. The window only ever calls the protocol (`check`, `exists`, `listdir`, `read_text`,
`open_write`), so a remote service backend (http, Globus) is one more class and one more branch in
`open_storage`. Real sign-in replaces `auth.sign_in` and keeps its signature.

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
The schema is plain SQL to carry over to the lab's database server; `Catalog.sql` runs read-only queries for now.

## Sidecars

`<name>.lab4.json` beside each transferred or attached item: `recording`, `key`, `acquisition`, `notes`, `extra`,
`written_by`, `written_at`, `source`.

## Gotchas

- hello_imgui loads Font Awesome glyphs only up to U+F2E0. Icons past it (`ICON_FA_ROTATE`,
  `ICON_FA_RIGHT_FROM_BRACKET`, `ICON_FA_FOLDER_TREE`) render as `?`; pick one below.
- Headless tests run on hello_imgui's null backend, which needs `renderer_has_textures` set in `post_init`.
