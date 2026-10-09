# imgui_lab4_interface

A desktop window for the PML group to move data onto BioHPC storage, attach Lab4 metadata to it, and register
recordings in the Lab4 database. Built on
[imgui-bundle](https://github.com/pthom/imgui_bundle) and
[imgui_data_loader](https://github.com/FlynnOConnell/imgui_data_loader), laid out like the `masknmf` launcher.

## Install

```bash
git clone https://github.com/FlynnOConnell/imgui_lab4_interface.git
cd imgui_lab4_interface
uv venv && uv pip install -e ".[dev]"
lab4
```

## Use

1. **Sign in.** The window signs in to the Lab4 database with the credentials file the Lab4 website hands out: sign
   in there with GitLab, download your credentials, and save them as `~/.lab4/secrets.json` (or point
   `LAB4_SECRETS_PATH` at them). **Work offline** skips Lab4; transfers and sidecars still work.
2. **Settings.** On **Connection**, pick how this machine reaches BioHPC: a mounted share (a local folder and the BioHPC folder it
   shows, e.g. `X:\` showing `/biohpc`) or ssh (an alias or `user@host` with keys already set up). Paths are always
   BioHPC's own (`/biohpc/data2/...`), the ones Lab4 stores. With no mount root set, the window looks for a drive
   or mount whose share is `/biohpc` (e.g. an SSHFS-Win `\\sshfs.kr\you@host!port\biohpc`) and uses it when there
   is exactly one; **Detect** lists them. **Destinations** holds the folders Transfer offers and **Viewers** what
   opens a dataset. **Save** writes them to `~/.imgui_lab4_interface/settings.json`.
3. **Transfer.** Three steps on one page:
   1. *Sources*: add files or folders.
   2. *Destination*: type a BioHPC folder or **Browse...** for one. A folder that does not exist yet is made when its
      parent exists (one new folder, never its parents); the line under the field says whether it exists, will be
      made, or cannot be used.
   3. *Metadata*: the recording's Lab4 key (experimenter, experiment group, subject, session start, device), its
      label, weight and notes, and any extra key/value fields. Signed in, the choices come from Lab4's lookup tables,
      and a subject new to Lab4 asks for the fields its Subject row needs.

   Send copies each source under its own name with `<name>.lab4.json` beside it.
4. **Browse storage.** List BioHPC folders, see which items already carry metadata, and attach a sidecar to data
   that is already there, using the same metadata form, folded below the list. **Scan** catalogs the folder and
   everything under it: each dataset's kind, what every folder holds, and the sidecars. Double-click a folder to enter it and a dataset file to open it in mbo or
   masknmf; a dataset folder opens from **Open in** below the list. Selecting an `.h5` (what `lab4.convert` writes)
   offers **Register in Lab4**, which adds it as the recording's data, with its experiment group, subject and
   session when they are new, in one transaction.

Opening needs the share mounted (ssh storage only lists) and the viewers set in Settings, either names on
`PATH` or full paths, e.g. `~/envs/pengui/.venv/Scripts/mbo.exe`.

| kind | opens with |
|------|------------|
| `tiff`, `tiff-series`, `zarr`, `mesc`, `hdf5`, `suite2p-plane`, `suite2p-run` | `mbo <path>` |
| `voltage-pf` | `mbo curate <path>` |
| `masknmf-results`, `masknmf-run`, `roicat-tracking` | `masknmf view <path>` |

## Files on disk

Everything lives under `~/.imgui_lab4_interface/` (override with `IMGUI_LAB4_HOME`):

| file | holds |
|------|-------|
| `settings.json` | backend, mount root, ssh host, destinations |
| `recent.json` | recently added sources, shown in the rail |
| `window.ini` | window layout |
| `catalog.sqlite` | the catalog: `datasets`, `folders`, `metadata`, `scans` |
| `logs/` | output of each viewer opened |

## Development database

Never develop against the Lab4 server. `scripts/dev_db.py` runs a throwaway Lab4 database on `127.0.0.1:55433`:
PostgreSQL 16 from conda-forge through [pixi](https://pixi.sh), built from a Lab4 checkout's development fixtures and
migrations (`~/repos/lab4`, or `--lab4`), with one lab user provisioned the way the website provisions one.

```bash
.venv/Scripts/python scripts/dev_db.py up      # create on first run, then start; down stops, reset rebuilds
LAB4_SECRETS_PATH=.dev/secrets.json lab4
```

## Tests

```bash
.venv/Scripts/python -m pytest
```

Every test runs with `LAB4_SECRETS_PATH` pointed at a missing file. `tests/test_database.py` uses only the
development database and skips when it is not running.
