# Realtime System Monitor (macOS)

Menu bar system monitor with realtime stats + settings UI. Each metric shows as its own status item (label on top, value below). Click a metric to open a detailed panel. Disk panel includes SMART fields via `smartctl` (required). GPU usage uses `powermetrics` (requires sudo) on Apple Silicon.

## Features
- Menu bar text shows CPU/RAM/Disk/Network in realtime
- Settings UI (PySide6) to toggle which metrics appear
- Realtime CPU chart in settings (pyqtgraph)
- Update interval configurable (default 500ms)

## Requirements
- macOS
- Python 3.10+

## Install
```bash
pip install -r requirements.txt
```

## Run
```bash
python realtime_overlay.py
```

## Notes
- The menu bar text updates without clicking.
- GPU usage uses `powermetrics` (requires sudo) on Apple Silicon.
- SMART data requires `smartctl` (install via `brew install smartmontools`).
 - You can grant permissions inside the app (Settings → Enable GPU/SMART) without using Terminal.
 - The app can attempt to auto-install `smartmontools` when you click Enable GPU/SMART.

## Build macOS App (.app)
Install PyInstaller (one-time):
```bash
python -m pip install pyinstaller
```

Build:
```bash
./build_macos.sh
```

With icon:
```bash
./build_macos.sh /path/to/icon.icns
```

Output:
```
dist/RealtimeSystemMonitor.app
```

## Build DMG (single file)
After building the app, create a DMG:
```bash
hdiutil create -volname RealtimeSystemMonitor -srcfolder dist/RealtimeSystemMonitor.app -ov -format UDZO dist/RealtimeSystemMonitor.dmg
```

## Optional: Allow powermetrics without password
If you want GPU to update without sudo prompt, add to sudoers:
```
<username> ALL=(root) NOPASSWD: /usr/bin/powermetrics
```
- Settings are opened via the menu bar item.
