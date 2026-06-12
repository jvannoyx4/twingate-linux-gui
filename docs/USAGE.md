# Usage

Twingate Linux GUI is a desktop wrapper around the official `twingate` CLI.
Accounts, resources, authentication state, and connection status all come from
the local Twingate client on the machine where the app is installed.

## Main Window

The main window has three primary areas:

- Network: active Twingate network and available local accounts.
- Connection: connect/disconnect controls plus tunnel status details.
- Resources: resources visible to the active account and authentication state.

## Account Switching

Select an account card in the Network section or choose an account from the tray
menu under `Switch Account`.

When switching succeeds, the app opens the active Twingate tenant in the browser
profile associated with that Twingate account email when Chrome/Chromium exposes
matching profile metadata.

## Resource Authentication

When a resource is marked `Pending`, the app runs:

```bash
twingate auth "<resource name>"
```

It captures the browser authentication URL from the command output and opens it
in the matched Chrome/Chromium profile. The app rate-limits automatic auth
attempts per resource to avoid opening repeated browser tabs.

You can also double-click a pending resource in the Resources table to start
authentication manually.

## Tray Menu

The system tray menu shows:

- connection status
- active account
- resource and pending-auth count
- tunnel IP/interface
- quick account switching
- connect/disconnect and service start/stop actions
- setup and quit actions

## Chrome/Chromium Profile Selection

The browser helper looks at Chrome/Chromium profile metadata. If a profile email
matches the active Twingate account email, that profile is used automatically.

If no matching profile is found and `zenity` is installed, the app prompts you to
choose a profile.

## Interactive Twingate Commands

`Setup` and `Add Account` open in a terminal because the Twingate CLI can prompt
for input during those flows.
