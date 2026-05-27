# FORGE — Windows Build via GitHub Actions

This builds a single-file Windows `.exe` of FORGE in the cloud, with
BtbN's static ffmpeg 8.1 bundled inside. No Windows machine required
on your end.

## Setup (one time)

1. **Put your project on GitHub.** If it's not there yet, create a new
   repo and push the codebase.

2. **Add these files to the repo:**

   | File                                  | Location in repo                          |
   | ------------------------------------- | ----------------------------------------- |
   | `app.py`                              | repo root (replaces your existing one)    |
   | `index.html`                          | repo root (unchanged)                     |
   | `forge-windows.spec`                  | repo root                                 |
   | `requirements.txt`                    | repo root (unchanged)                     |
   | `build-windows.yml`                   | `.github/workflows/build-windows.yml`     |

   The workflow lives at `.github/workflows/build-windows.yml` — the
   directory matters; GitHub only auto-discovers workflows in that
   exact path.

3. **Commit and push.**

That's it. GitHub Actions auto-runs the workflow on every push to
`main`/`master`, on pull requests, on tag pushes starting with `v`, and
whenever you click "Run workflow" from the Actions tab.

## Download the .exe

After a successful run:

- Go to **Actions** in your repo
- Click the most recent green-checkmark run
- Scroll to the **Artifacts** section at the bottom
- Download `forge-windows-x64.zip` — it contains `forge.exe`

Artifacts stay available for 30 days. If you want a permanent download
link, push a tag (`git tag v1.0.0 && git push --tags`) and the workflow
will attach `forge.exe` to a GitHub Release instead.

## What the workflow does

1. Spins up a fresh `windows-latest` runner
2. Installs Python 3.12 + Flask + PyInstaller
3. Downloads BtbN's `ffmpeg-n8.1-latest-win64-gpl-8.1.zip` (~80 MB)
4. Extracts `ffmpeg.exe` and `ffprobe.exe` into `bin/`
5. Logs which hardware encoders the bundled ffmpeg actually has
   (NVENC / QSV / AMF / Vulkan) — useful for sanity checks
6. Runs `pyinstaller forge-windows.spec`
7. **Smoke-tests** the resulting `.exe`: launches it, hits
   `/api/config`, kills it. If the exe is broken, the build fails.
8. Uploads `forge.exe` as a downloadable artifact

Total build time is typically 3–5 minutes.

## Running the .exe

Double-click `forge.exe`. The default browser opens at
`http://127.0.0.1:5500`. No console window appears.

On first launch, `forge.exe` creates a `forge-data\` folder next to
itself for config and temp files. Move the exe and that folder
together if you want to relocate the app.

To quit: close the browser tab and click "Quit" if you've added that
to the UI, or end the `forge.exe` process from Task Manager. (The
app exposes `POST /api/shutdown` which terminates it cleanly.)

## Hardware encoder support on Windows

The bundled ffmpeg is configured to use whichever hardware acceleration
your machine actually has. The "GPU" dropdown is populated by:

1. Listing your video adapters via `Get-CimInstance Win32_VideoController`
2. Asking ffmpeg which `_nvenc` / `_qsv` / `_amf` / `_vulkan` encoders
   it has compiled in
3. Showing the intersection

| GPU vendor              | Encoder family | Requires       |
| ----------------------- | -------------- | -------------- |
| NVIDIA (GTX 600+ / RTX) | NVENC          | Recent driver  |
| Intel (Arc, recent iGPU)| QSV (oneVPL)   | Recent driver  |
| AMD (Radeon)            | AMF            | Adrenalin      |
| Any with Vulkan support | Vulkan         | Vulkan ICD     |

There's no VAAPI on Windows — that's Linux-only. The Linux build
covers VAAPI; the Windows build covers NVENC and AMF instead. QSV
and Vulkan are available on both.

## Local Windows build (alternative)

If you'd rather build on a Windows box directly without GitHub:

```powershell
# Prerequisites: Python 3.10+ from python.org

python -m venv build-venv
.\build-venv\Scripts\Activate.ps1
pip install flask flask-socketio eventlet pyinstaller

# Grab ffmpeg
Invoke-WebRequest 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip' -OutFile ffmpeg.zip
Expand-Archive ffmpeg.zip -DestinationPath ffmpeg-extract
mkdir bin
$src = (Get-ChildItem ffmpeg-extract -Recurse -Filter ffmpeg.exe | Select-Object -First 1).Directory.FullName
Copy-Item "$src\ffmpeg.exe","$src\ffprobe.exe" -Destination bin\

# Build
pyinstaller forge-windows.spec --clean --noconfirm

# Test
.\dist\forge.exe
```

## Troubleshooting

**Workflow fails at "Download BtbN ffmpeg 8.1"** — BtbN's `latest`
release floats; if they retitle the file, update the URL in
`build-windows.yml`. Check the current name at
<https://github.com/BtbN/FFmpeg-Builds/releases/tag/latest>.

**Smoke test fails** — the `.exe` builds but doesn't respond on port
5500. Most common cause: a hidden import got missed. Check the run
log for Python errors. Adding `console=True` temporarily in
`forge-windows.spec` and re-running will surface them on next launch.

**SmartScreen warns "unrecognized app"** on the user's machine — that's
because the exe isn't code-signed. Signing requires a Windows code
signing certificate (~$200/yr from DigiCert/Sectigo). Users can click
"More info → Run anyway" to bypass. For broader distribution, signing
is worth it; for personal use, it isn't.

**Antivirus flags the .exe** — PyInstaller bundles are infamous for
false positives. The two solutions: code-sign it (above), or rebuild
PyInstaller's bootloader from source (a one-time fix that drops most
AV detections, but it's involved). For a single-user app, neither is
necessary.
