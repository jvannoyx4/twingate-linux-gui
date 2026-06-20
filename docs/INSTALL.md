# Install

Twingate Linux GUI installs as a per-user desktop application. It does not
require root access unless your system is missing required OS packages.

## Requirements

- Twingate Linux CLI installed and available as `twingate`.
- Python 3.
- GTK 3, AppIndicator, and libnotify Python bindings.
- Chrome or Chromium for browser-based Twingate authentication.
- Optional: `zenity` for profile selection dialogs.

Ubuntu/Debian:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1 gir1.2-notify-0.7 zenity
```

## Install From A Release Tarball

Download and extract a release:

```bash
tar -xzf twingate-linux-gui-0.1.3.tar.gz
cd twingate-linux-gui-0.1.3
./install.sh
```

The installer writes files to:

- `~/.local/opt/twingate-linux-gui`
- `~/.local/bin/twingate-gui`
- `~/.local/share/applications/twingate-gui.desktop`
- `~/.local/share/icons/hicolor/scalable/apps/twingate-gui.svg`

Launch it from your application launcher or run:

```bash
~/.local/bin/twingate-gui
```

## Install From Git

```bash
git clone https://github.com/jvannoyx4/twingate-linux-gui.git
cd twingate-linux-gui
./install.sh
```

## Custom Install Prefix

By default the app installs under `~/.local`. You can override that:

```bash
PREFIX="$HOME/Applications/twingate-linux-gui" ./install.sh
```

## Uninstall

From the extracted release or cloned repo:

```bash
./uninstall.sh
```

If you used a custom prefix:

```bash
PREFIX="$HOME/Applications/twingate-linux-gui" ./uninstall.sh
```
