# Troubleshooting

## App Does Not Start

Run it from a terminal:

```bash
~/.local/bin/twingate-gui
```

If Python GTK bindings are missing, install:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1 gir1.2-notify-0.7
```

## Tray Icon Does Not Appear

The app uses AppIndicator. Make sure your desktop environment supports
AppIndicator tray icons. Ubuntu GNOME usually supports this out of the box.

## Launcher Icon Is Stale

Refresh local desktop/icon caches:

```bash
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

You may need to close and reopen the app launcher, or log out and back in.

## Resource Opens Browser But Authentication Fails

Check that the browser profile selected by the app is signed in to the identity
provider account expected by that Twingate network.

If automatic profile matching picks the wrong profile, sign into Chrome/Chromium
with the correct profile email or install `zenity` so the app can prompt for a
profile.

## No Resources Are Listed

Check the active account and run:

```bash
twingate status
twingate account list
twingate resources
```

If the CLI does not show resources, the GUI will not show them either.

## Twingate CLI Is Missing

Install the official Twingate Linux client first. This project does not bundle
or replace the supported Twingate client.
