# Development

## Run From Source

```bash
./twingate-gui
```

## Compile Check

```bash
python3 -m py_compile twingate-gui.py twingate-chrome-profile-picker.py
```

## Build Release Tarball

```bash
VERSION=0.1.3 ./scripts/build-release.sh
```

The release archive is written to `dist/`.

## Clean Local Build Artifacts

```bash
rm -rf __pycache__ dist
```

## Security And Privacy

Do not commit personal Twingate account data, CLI output logs, browser profile
state, or machine-specific paths. The app should discover all accounts and
resources from the local Twingate CLI at runtime.

## Versioning

Update `APP_VERSION` in `twingate-gui.py` before building a release. The app's
self-update check compares that value with the latest GitHub release tag.
