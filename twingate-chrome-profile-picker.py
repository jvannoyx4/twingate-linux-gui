#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


CHROME_CONFIG = Path.home() / ".config" / "google-chrome"
LOCAL_STATE = CHROME_CONFIG / "Local State"


def chrome_binary() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def profiles() -> list[dict[str, str]]:
    try:
        data = json.loads(LOCAL_STATE.read_text())
    except Exception:
        return [{"directory": "Default", "name": "Default", "email": ""}]

    cache = data.get("profile", {}).get("info_cache", {})
    items = []
    for directory, info in cache.items():
        if directory in {"Guest Profile", "System Profile"}:
            continue
        items.append(
            {
                "directory": directory,
                "name": info.get("name") or directory,
                "email": info.get("user_name") or "",
            }
        )

    return sorted(items, key=lambda item: (item["name"].casefold(), item["email"].casefold()))


def profile_for_email(items: list[dict[str, str]], email: str | None) -> str | None:
    if not email:
        return None
    target = email.casefold()
    for item in items:
        if item["email"].casefold() == target:
            return item["directory"]
    return None


def choose_profile(items: list[dict[str, str]]) -> str | None:
    zenity = shutil.which("zenity")
    if not zenity:
        return items[0]["directory"] if items else "Default"

    args = [
        zenity,
        "--list",
        "--title=Twingate Authentication",
        "--text=Select the Chrome profile to use for Twingate authentication.",
        "--width=720",
        "--height=360",
        "--column=Directory",
        "--column=Profile",
        "--column=Signed-in account",
        "--hide-column=1",
        "--print-column=1",
    ]
    for item in items:
        args.extend([item["directory"], item["name"], item["email"]])

    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        return None
    selected = proc.stdout.strip()
    return selected or None


def main() -> int:
    chrome = chrome_binary()
    if not chrome:
        subprocess.run(["zenity", "--error", "--text=Chrome was not found."])
        return 127

    urls = sys.argv[1:] or ["chrome://newtab"]
    items = profiles()
    selected = profile_for_email(items, os.environ.get("TWINGATE_AUTH_EMAIL"))
    if not selected:
        selected = choose_profile(items)
    if not selected:
        return 1

    subprocess.Popen([chrome, f"--profile-directory={selected}", *urls])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
