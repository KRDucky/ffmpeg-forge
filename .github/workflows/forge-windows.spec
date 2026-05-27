# PyInstaller spec for FORGE (Windows)
# Built by .github/workflows/build-windows.yml
#
#   pyinstaller forge-windows.spec --clean --noconfirm
#
# Output: dist\forge.exe

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = (
    collect_submodules('engineio.async_drivers')
    + collect_submodules('socketio')
    + [
        'engineio.async_drivers.threading',
        'engineio.async_drivers.eventlet',
    ]
)

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[
        ('bin/ffmpeg.exe',  'bin'),
        ('bin/ffprobe.exe', 'bin'),
    ],
    datas=[
        ('index.html', 'templates'),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc_data', 'test',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='forge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # No console window — users get the browser tab and nothing else.
    # If you need to debug a misbehaving build, flip this to True temporarily.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='forge.ico',   # uncomment if you add an icon
)
