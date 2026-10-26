# imgui_lab4_interface

A desktop app for the PML lab: copy data to BioHPC, attach Lab4 metadata, and register recordings in the Lab4
database. Built on [imgui-bundle](https://github.com/pthom/imgui_bundle) and
[imgui_data_loader](https://github.com/FlynnOConnell/imgui_data_loader).

## Install

```bash
git clone https://github.com/FlynnOConnell/imgui_lab4_interface.git
cd imgui_lab4_interface
uv venv && uv pip install -e ".[dev]"
lab4
```

## Links

- [Lab docs](https://doc.memorylongevity.org/)
- [Lab4 docs](https://lab4.memorylongevity.org/)
- [Lab4 workspace](http://server.memorylongevity.org): sign in with GitLab and download your credentials

## Use

1. **Sign in.** Download your credentials from the Lab4 workspace and save them as `~/.lab4/secrets.json` (or set
   `LAB4_SECRETS_PATH`). **Work offline** skips Lab4; transfers and sidecars still work.
2. **Settings.** **Save** writes them to `~/.imgui_lab4_interface/settings.json`.
   - *Connection*: a mounted share (e.g. `X:\` showing `/biohpc`) or ssh (an alias or `user@host`, keys set up).
     With no mount root set, a single `/biohpc` mount found on this machine is used; with none, ssh to
     `lab-research`, the lab server alias pml_utilities uses. **Detect** lists mounts.
   - *Destinations*: the folders Transfer offers.
   - *Machines*: the computers Results catalogs (this one, lab machines, home computers). Each is reached as
     *this computer*, *mounted here* (a local folder showing one of its folders, e.g. `~/pml-w1` showing
     `/home/fo`), or *ssh* (listed only), with the folders to catalog in its own paths.
   - *Viewers*: the `mbo` and `masknmf` commands, on `PATH` or full paths.
   - *Processing*: the ssh host jobs are submitted through (a BioHPC login node, `biohpc` by default), GPU
     partition, GPUs, email, and the pipeline folder
     (`~/repos/pml_utilities/hpc/asap7`; see its README).

   Paths are always BioHPC paths (`/biohpc/data2/...`), as Lab4 stores them, and a machine's own paths for a machine.
3. **Transfer.**
   1. *Sources*: add files or folders.
   2. *Destination*: type a BioHPC folder or **Browse...**. A missing folder is made when its parent exists.
   3. *Metadata*: the Lab4 key (experimenter, experiment group, subject, session start, device), label, weight,
      notes and extra fields. Signed in, the choices come from Lab4. A new subject asks for its Subject fields.
   4. *Processing*: tick **Process .mesc units overnight** to run the ASAP7 spine pipeline (`mbo asap7`) on BioHPC.
      Pick the units (movies with enough frames) and set the channel, first frame, MAD threshold and detrend window.

   **Send** copies each source with `<name>.lab4.json` beside it. With processing on, it then checks each `.mesc`
   landed whole and submits one SLURM job per unit. Results go to `masknmf_asap7/<unit>_detrend_<time>/` beside the
   `.mesc`. **Check** gets job states from `sacct`.
4. **Browse storage.** List BioHPC folders and see which items have metadata. Select one to attach a sidecar.
   **Scan** catalogs a folder and everything under it. Double-click a folder to enter it, or a dataset to open it.
   Select an `.h5` (from `lab4.convert`) to **Register in Lab4**; new experiment groups, subjects and sessions are
   added in the same transaction. A database icon marks files that are a Lab4 recording's data; **Show in Lab4**
   opens one. **Load into the form** fills the metadata form from the selected item's sidecar.
5. **Lab4 recordings.** Every recording in Lab4, filterable. Pick one to edit its label, weight and data path, its
   session's notes, and its subject's columns, or add an injection (where the server has the Injection table). **Save to Lab4** lists the changes and asks first.
   The key (experimenter, group, subject, session start, device) cannot change here: other rows point at it, and lab
   members cannot delete. With a sidecar beside the data, Save can rewrite it with the saved values, keeping its
   extra fields. **Refresh** keeps unsaved edits and takes what others changed elsewhere. Double-click a recording,
   or **Show in Browse**, to list its folder; **Use in the metadata form** copies it to Transfer's and Browse's form.
6. **Results.** One row per recording and unit, with its frame rate, frames, indicator, the masknmf and vnoiser
   runs on it and their figures and tables, from every machine. Pick a row to open its data in `mbo`, a run in
   `masknmf view` or its log, or double-click a result file. **Scan machines** catalogs every machine's folders;
   `lab4-scan [machine ...]` does the same headless, e.g. from a timer. Units come from `.mesc` headers (read only
   where the file opens in place) and `<recording>__MSession_N_MUnit_N.tif` exports; runs and results are matched
   to them by the recording and unit names in their paths and inputs. A machine that doesn't answer keeps its last
   scan.
7. **Human slices.** Track human slices, with demo rows seeded from Shobhana's files in `/biohpc/data4/Shobhana`.
   A slice's `file_match` finds its files: every word must be in the path, a `YYYY-MM-DD` word matches any spelling
   of that date (`7-27-2026`, `260727`, `May21st_2026`), and `-word` excludes. Filter the tables with words,
   `column:value`, or `column=value` for an exact match. Double-click a file to open it; over
   ssh it is first copied to `~/.imgui_lab4_interface/cache`, after asking. Rows belong to whoever added them;
   `shobhana` is the admin. *acting as* picks the user.

Opening a dataset needs the share mounted (ssh can only list) and the viewers set in Settings.

| kind | opens with |
|------|------------|
| `tiff`, `tiff-series`, `zarr`, `mesc`, `hdf5`, `suite2p-plane`, `suite2p-run` | `mbo <path>` |
| `voltage-pf` | `mbo curate <path>` |
| `masknmf-results`, `masknmf-run`, `roicat-tracking` | `masknmf view <path>` |

## Files on disk

Under `~/.imgui_lab4_interface/` (or `IMGUI_LAB4_HOME`):

| file | holds |
|------|-------|
| `settings.json` | settings |
| `recent.json` | recent sources, shown in the rail |
| `window.ini` | window layout |
| `catalog.sqlite` | the catalog: `datasets`, `folders`, `metadata`, `units`, `runs`, `results`, `scans` |
| `slices.sqlite` | the slice tables: `patients`, `slices`, `assays`, and the `files` index |
| `cache/` | files copied from BioHPC to open |
| `logs/` | viewer output |
| `jobs.json` | submitted processing jobs |

## Development database

Never develop against the Lab4 server. `scripts/dev_db.py` runs a local Lab4 database on `127.0.0.1:55433`
(PostgreSQL 16 through [pixi](https://pixi.sh)), built from a Lab4 checkout (`~/repos/lab4`, or `--lab4`).

```bash
.venv/Scripts/python scripts/dev_db.py up      # create on first run, then start; down stops, reset rebuilds
LAB4_SECRETS_PATH=.dev/secrets.json lab4
```

## Tests

```bash
.venv/Scripts/python -m pytest
```

Tests point `LAB4_SECRETS_PATH` at a missing file. `tests/test_database.py` needs the development database and
skips without it.

## What changes files

Nothing is deleted, moved or renamed on BioHPC. Every action that writes data or deletes rows asks first:

| action | writes | asks with |
|--------|--------|-----------|
| Transfer: Send | the sources and sidecars, then any processing jobs; with overwrite on, replaces what is there | Send to BioHPC? |
| Browse: Attach metadata | `<name>.lab4.json` | Attach metadata? |
| Lab4 recordings: Save to Lab4 | the recording's, session's and subject's columns outside the key, then, if ticked, the sidecar beside its data | Save to Lab4? |
| Lab4 recordings: Add injection | an Injection row | Add to Lab4? |
| Human slices: open a file over ssh | a copy in `cache/` | Copy to this machine? |
| Human slices: Export | a csv | the system save dialog |
| Human slices: Delete, Reset rows | rows in `slices.sqlite` | Delete rows?, Reset rows? |

Editing a cell saves it on Enter. The app's own files (`settings.json`, `recent.json`, `window.ini`,
`catalog.sqlite`, `logs/`, `jobs.json`) are written without asking. A `slices.sqlite` from an older version is kept
as `slices.v<N>.sqlite`.
