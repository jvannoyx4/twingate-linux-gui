# Contributing

Contributions are welcome.

## Before Opening A Pull Request

Run:

```bash
python3 -m py_compile twingate-gui.py twingate-chrome-profile-picker.py
VERSION=0.1.0 ./scripts/build-release.sh
```

Check that no personal account data is included:

```bash
rg -n "your-email|your-network|/home/your-user" .
```

## Scope

This project should remain a lightweight desktop wrapper around the supported
Twingate Linux CLI. Avoid changes that replace or reimplement Twingate's own
client behavior.
