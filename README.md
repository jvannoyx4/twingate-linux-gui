# Twingate Linux GUI

A GTK/AppIndicator desktop wrapper for the supported Twingate Linux CLI.

This is not a port of Twingate's macOS application source. The app calls the
installed `twingate` command for status, connect/disconnect, start/stop, setup,
account switching, resource listing, and resource authentication.

## Features

- Polished GTK desktop window for Twingate status, accounts, resources, and connection details.
- AppIndicator tray menu with status, active account, resource stats, tunnel details, and account switching.
- Automatic Chrome/Chromium profile selection for Twingate authentication when the browser profile email matches the Twingate account email.
- Automatic browser auth launch when a resource enters `Pending` authentication.
- Local user install with desktop launcher and icon.

## Requirements

- Official Twingate Linux CLI installed and available as `twingate`.
- Python 3.
- GTK 3, libnotify, and AppIndicator GObject bindings.
- Optional: Google Chrome or Chromium for browser authentication.
- Optional: `zenity` for manually choosing a Chrome profile when no signed-in profile email matches the active Twingate account.

Ubuntu/Debian dependencies:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1 gir1.2-notify-0.7 zenity
```

## Install

From an extracted release tarball or cloned repo:

```bash
./install.sh
```

Install from Git:

```bash
git clone https://github.com/jvannoyx4/twingate-linux-gui.git
cd twingate-linux-gui
./install.sh
```

This installs to:

- App files: `~/.local/opt/twingate-linux-gui`
- Command: `~/.local/bin/twingate-gui`
- Launcher: `~/.local/share/applications/twingate-gui.desktop`
- Icon: `~/.local/share/icons/hicolor/scalable/apps/twingate-gui.svg`

Run it from your app launcher or:

```bash
twingate-gui
```

If `~/.local/bin` is not in your `PATH`, run:

```bash
~/.local/bin/twingate-gui
```

## Uninstall

```bash
./uninstall.sh
```

## Documentation

- [Install](docs/INSTALL.md)
- [Usage](docs/USAGE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Development](docs/DEVELOPMENT.md)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Build A Release Tarball

```bash
./scripts/build-release.sh
```

The tarball is written to `dist/`.

Set a specific version:

```bash
VERSION=0.1.0 ./scripts/build-release.sh
```

## Notes

- The app does not bundle account data. Accounts are read from the local Twingate CLI on each machine.
- Browser profile matching is based on Chrome/Chromium profile metadata on the local machine.
- Setup and account-add flows open in a terminal because those Twingate CLI flows can be interactive.
- The tray indicator requires AppIndicator support. Ubuntu GNOME supports this by default on many installs.

## License

MIT
