# imgui_lab4_interface

A desktop window for the PML group to move data onto BioHPC storage and attach Lab4 metadata to it. Built on
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

1. **Sign in.** The gate is the pml_utilities BioHPC widget's demo login (`loson` / `treadmill`); nothing is
   authenticated. Storage access uses this machine's own mount or ssh keys.
2. **Storage.** Pick how this machine reaches BioHPC: a mounted share (a local folder BioHPC paths resolve under,
   e.g. a mapped lamella drive) or ssh (an alias or `user@host` with keys already set up). Edit the destination
   folders offered on Transfer. Saved to `~/.imgui_lab4_interface/settings.json`.
3. **Metadata.** Fill in the recording: the Lab4 DataJoint key (experimenter, group, subject, session date,
   recording #), acquisition (device, indicator, region), notes and any extra key/value fields.
4. **Transfer.** Add files or folders, choose a destination and optional subfolder, and send. Each source is copied
   under its own name with `<name>.lab4.json` beside it.
5. **Browse storage.** List BioHPC folders, see which items already carry metadata, and attach a sidecar to data
   that is already there. **Scan** catalogs the folder and everything under it: each dataset's kind, what every
   folder holds, and the sidecars. Double-click a dataset to open it in mbo or masknmf.
6. **Find data.** Search the catalog by words in the path or sidecar (`M231 2026-08 kind:suite2p-plane`), or run
   read-only SQL over it, and open what it finds.

Opening needs the share mounted (ssh storage only lists) and the viewers set on the Storage page, either names on
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

## Tests

```bash
.venv/Scripts/python -m pytest
```
