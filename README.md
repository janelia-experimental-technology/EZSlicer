# EZSlicer

EZSlicer is a Windows-focused MSLA resin slicer with an Electron 3D viewer, deterministic Python slicing backend, automatic island support generation, and native `.goo` / `.ctb` export.

The project favors stability and explicit settings over silent guessing. Always confirm the machine profile, resin profile, and output format against the printer you intend to use before printing.

## Shared Portable Build

For someone who just wants to run the app:

1. Extract `EZSlicer-Windows-Portable-<version>.zip` (check it against its `.sha256`).
2. Run `Run EZSlicer.vbs`, or open `EZSlicer\EZSlicer.exe`.
3. Keep the `EZSlicer` folder together. Do not move only the `.exe` out of the folder.

The packaged build includes Electron and a minimal Python runtime, so users should not need Node.js, npm, or a separate Python install.

To create the shareable package from source:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\package_release.ps1
```

Outputs:

- `dist\EZSlicer-Windows-Portable\`
- `dist\EZSlicer-Windows-Portable-<version>.zip` and its `.sha256`
- `version.txt` next to `EZSlicer.exe` (first line = the version)

## Versioning

EZSlicer uses semantic versions tagged as `vX.Y.Z`. The version lives in `package.json`, `package-lock.json`, `pyproject.toml` and `resin_slicer/__init__.py`; the release script keeps them in step:

```powershell
npm run release -- patch          # bug fixes: 0.2.0 -> 0.2.1
npm run release -- minor          # new features: 0.2.0 -> 0.3.0
npm run release -- 1.0.0          # explicit version
npm run release -- minor --dry-run   # preview the bump and change list
npm run release -- minor --push      # also push the commit and tag
```

It refuses to run with uncommitted changes, runs the tests and build, bumps every version field, commits `Release vX.Y.Z`, and creates an annotated tag listing the commits since the previous tag. The app shows its version next to the title in the sidebar, and `python -m resin_slicer.cli --version` prints it.

Pushing a tag builds the release in GitHub Actions (`.github/workflows/release.yml`) from the tag itself: the portable app zip and the engine zip, each with a `.sha256`, attached to a GitHub release.

## Headless Engine

EZSlicer's slicing and printer connections also run without the window, as an HTTP/JSON API on `127.0.0.1` for jET Hangar. The window uses the same engine for its own work, so each printer connection has one owner.

```powershell
$env:JET_ENGINE_TOKEN = "<secret>"
python -m resin_slicer.engine --engine   # or: EZSlicer.exe --engine
```

It prints `{"engine": "ezslicer", "version": ..., "api": "1.0", "port": ...}` and serves `/api/health`, `/api/version`, `/api/capabilities`, `/api/openapi.json` and the `/api/v1/...` routes. Contract tests: `python -m pytest tests/contract`. See [docs/ENGINE.md](docs/ENGINE.md) for the routes, streaming, errors and release packages.

## GUI Workflow

1. Open one or more STL/OBJ/STP/STEP files, or drag mesh files into the 3D view.
2. Choose a machine profile and printer-specific resin from the top ribbon.
3. Choose `GOO` or `CTB` in the `Job` tab.
4. For STEP/STP jobs, choose `Tessellated` or `B-rep sections` from `CAD slice mode` in the Job tab. Tessellated mode slices the converted triangle mesh. B-rep sections mode uses OpenCascade CAD sectioning for STEP/STP model exposure layers while retaining tessellation for display and support planning.
5. Adjust placement/orientation in the 3D viewer or the `Placement` tab. Multiple loaded meshes can be placed across multiple build plates.
6. Generate supports from the `Supports` tab when needed.
7. Slice the active build plate, or use `Slice All` and `Save All` to process each build plate as its own sliced file.

Layer rendering uses multiple worker threads by default, capped at four workers or the number of layers in the job.

The app is organized around three main areas:

- Top ribbon: machine profile, resin selection, `Modify Printer`, `Modify Resin`, and `Settings`.
- Left sidebar: build plates and parts, grouped by build plate. Build plate groups can be expanded/collapsed, and parts can be moved between plates.
- Right sidebar: tabbed `Job`, `Placement`, and `Supports` menus. The sidebar can be hidden and reopened from the side tabs.

The 3D view supports multiple build plates, drag-and-drop import, direct model selection, transform widgets, a view cube, and a vertical layer/clipping scrubber. The layer scrubber starts at layer 1 and, after slicing, can display actual sliced layer images on the clipping plane. The slice preview reuses the same layer controls.

## Profiles

The GUI can import and export:

- Machine profiles
- Resin profiles
- Support settings

Machine profiles can also be imported directly from the UVTools GitHub printer list. Use `Modify Printer` -> `Import Printer`, search for the printer, then import it into the current machine settings. Imported printers can be saved to the local machine profile list.

`Modify Printer` lets you edit, save as new, save current, export, and delete machine profiles. The machine name field offers existing printers while still allowing typed names for new profiles.

`Modify Resin` works the same way for resins attached to the selected printer. The top ribbon resin dropdown only shows resins configured for the active machine profile. Resin and machine preferences are persisted locally so the app reopens with the same selections.

Exports are JSON files using this slicer's schema. Resin import also includes best-effort Chitubox-style field mapping for common names such as `normalExposureTime`, `bottomExposureTime`, `bottomLayerCount`, `liftingDistance`, `liftingSpeed`, `retractSpeed`, and `lightOffDelay`.

Chitubox profile formats are not fully standardized, so imported values should be reviewed after import.

## Supports

Support generation is island driven:

- Unsupported regions are detected layer-by-layer from overhang/island analysis.
- `Overhang deg` controls how aggressively regions are treated as unsupported.
- `Spacing mm` controls normal support density.
- `Primary extras` adds denser support candidates around each detected island's primary attachment area.
- `Enforcers` allow part-to-part supports for regions that cannot get a safe bed support.
- `Path clearance`, `Base reach`, and `Max shaft angle` control route safety.
- `Lift mm` raises the part only when support geometry is generated. Unsupported/no-support jobs drop the part to the build plate.
- `Light`, `Normal`, and `Heavy` support presets can be selected, and the current settings can be saved back to the active preset.
- Raft/skate interfaces use the projected model footprint with a configurable offset, thickness, and bottom chamfer.

The support preview shows posts, tips, bases, contact spheres, rafts/skates, enforcers, painted support/blocker zones, and diagonal braces before slicing. Previewed support plans are cached and reused for slicing when the model and settings match.

## File Format Notes

- `.goo`: native Elegoo GOO V3.0 writer with per-layer print settings and RLE compression.
- `.ctb`: native Chitu CTB v5 writer with CTB RLE, layer XOR coding, encrypted settings/signature sections, and preview/resin metadata records.

These are proprietary printer formats. The writer is intended to produce printer-compatible files, but machine firmware can be strict about resolution, build volume, metadata, and CTB/GOO dialects. Test with non-critical files first.

## Command Line

Basic slicing:

```powershell
python -m resin_slicer.cli model.stl --format goo --output model.goo --profile generic-2k
python -m resin_slicer.cli model.stl --format ctb --output model.ctb --profile generic-2k
python -m resin_slicer.cli model.stl --format goo --output model.goo --profile elegoo-jupiter-2-16k
python -m resin_slicer.cli model.obj --format goo --output model.goo --profile generic-2k
```

Orientation and printer overrides:

```powershell
python -m resin_slicer.cli model.stl --rotate-x 30 --rotate-z 45 --scale 0.95 --format goo --output model.goo
python -m resin_slicer.cli model.stl --resolution 3840x2400 --volume 130x82x160 --layer-height 0.05 --format goo --output model.goo
```

Support and resin settings:

```powershell
python -m resin_slicer.cli model.stl --model-lift 5 --overhang-angle 45 --support-spacing 3 --primary-supports --format goo --output model.goo
python -m resin_slicer.cli model.stl --exposure 2.5 --bottom-exposure 35 --lift-distance 5 --lift-speed 65 --retract-speed 150 --format ctb --output model.ctb
```

Disable supports:

```powershell
python -m resin_slicer.cli model.stl --no-supports --format ctb --output model.ctb
```

Control slicing worker threads:

```powershell
python -m resin_slicer.cli model.stl --format goo --output model.goo --workers 8
$env:RESIN_SLICER_LAYER_WORKERS = "8"
```

## Development

### Prerequisites

- **Node.js** (LTS). Install from https://nodejs.org or, on Windows:

  ```powershell
  winget install OpenJS.NodeJS.LTS
  ```

  After installing, sign out and back in (or reboot) so `node`/`npm` are on `PATH`.

- **Python 3.10 or newer** for the slicing backend. The Electron app auto-detects a
  suitable interpreter (via the `py` launcher, then `python`/`python3`), so you do
  not normally need to configure anything. If detection picks the wrong one, set the
  `PYTHON` environment variable to the interpreter you want and it takes priority.

Run tests:

```powershell
python -m unittest discover -s tests
```

Run the JavaScript syntax/build check:

```powershell
npm run check
```

Run the Electron app from source with Vite hot renderer updates:

```powershell
npm install
npm run dev
```

`npm install` runs a `postinstall` step (`tools/ensure-electron.js`) that guarantees the Electron binary is unpacked, working around environments where Electron's own installer downloads the binary but fails to extract it.

The double-click `Launch EZSlicer` shortcut in the app folder (and the `EZSlicer` desktop shortcut it installs) runs `launchers\EZSlicer.vbs` through `wscript.exe`, which starts this development flow without leaving a terminal window visible. It shows the lightweight splash screen first, launches the Vite dev server on `http://127.0.0.1:5178`, and then starts Electron with `VITE_DEV_SERVER_URL` pointed at that server.

Renderer/UI changes hot-update through Vite without closing the Electron window. Electron main-process and preload changes still require restarting the app.

Build and run the production-style renderer files:

```powershell
npm run build
npm start
```

In production mode Electron loads `dist/index.html`; keep `npm run build` in the packaging flow before launching `npm start`.

Build the portable app folder without the release zip:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_electron_portable.ps1
.\dist\EZSlicer\EZSlicer.exe
```

Build the optional root launcher:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_launcher.ps1
.\EZSlicer.exe
```

## Safety Checklist

Before printing:

- Match resolution and build volume to the exact printer.
- Confirm resin exposure, bottom exposure, lift speeds, and retract speeds.
- Preview supports after every orientation/settings change.
- Inspect islands and primary attachment regions.
- Start with a small, non-critical model when testing a new printer/profile combination.
