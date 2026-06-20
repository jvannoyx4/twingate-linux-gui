#!/usr/bin/env python3
import csv
import json
import os
import pty
import re
import select
import shutil
import shlex
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import Request, urlopen

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Notify", "0.7")
from gi.repository import AppIndicator3, Gdk, GLib, Gtk, Notify, Pango


APP_ID = "io.github.twingate_linux_gui.TwingateGui"
APP_CLASS = "twingate-gui"
APP_NAME = "Twingate"
APP_VERSION = "0.1.3"
RELEASE_API_URL = "https://api.github.com/repos/jvannoyx4/twingate-linux-gui/releases/latest"
RELEASE_PAGE_URL = "https://github.com/jvannoyx4/twingate-linux-gui/releases/latest"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(APP_DIR, "assets")
ICON_ONLINE = "twingate-tray-online"
ICON_OFFLINE = "twingate-tray-offline"
ICON_FILE = os.path.join(ASSET_DIR, "twingate-tray-online.svg")
CHROME_PROFILE_PICKER = os.path.join(APP_DIR, "twingate-chrome-profile-picker.py")

GLib.set_application_name(APP_NAME)
GLib.set_prgname(APP_CLASS)
if hasattr(Gdk, "set_program_class"):
    Gdk.set_program_class(APP_CLASS)


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class Account:
    email: str
    network: str
    network_url: str
    active: bool

    @property
    def slug(self) -> str:
        return self.network_url.split(".", 1)[0] if self.network_url else self.network

    @property
    def account_id(self) -> str:
        return f"{self.email}:{self.slug}" if self.slug else self.email


@dataclass
class ConnectionDetails:
    interface: str
    tunnel_ip: str
    route_count: int


@dataclass
class UpdateInfo:
    version: str
    tag: str
    asset_url: str
    page_url: str


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
URL_RE = re.compile(r"https://\S+")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def parse_version(value: str) -> tuple[int, ...]:
    text = value.strip().lstrip("vV")
    parts = []
    for part in text.split("."):
        match = re.match(r"(\d+)", part)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts or [0])


def newer_version(candidate: str, current: str) -> bool:
    left = list(parse_version(candidate))
    right = list(parse_version(current))
    width = max(len(left), len(right))
    left.extend([0] * (width - len(left)))
    right.extend([0] * (width - len(right)))
    return tuple(left) > tuple(right)


def fetch_latest_update() -> UpdateInfo | None:
    request = Request(
        RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"twingate-linux-gui/{APP_VERSION}",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to check for updates: {exc}") from exc

    tag = release.get("tag_name") or ""
    version = tag.lstrip("vV")
    if not tag or not newer_version(version, APP_VERSION):
        return None

    for asset in release.get("assets", []):
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        if name.endswith(".tar.gz") and "twingate-linux-gui" in name and url:
            return UpdateInfo(
                version=version,
                tag=tag,
                asset_url=url,
                page_url=release.get("html_url") or RELEASE_PAGE_URL,
            )
    raise RuntimeError(f"{tag} is available, but no release tarball was found.")


def safe_extract_tar(archive_path: str, destination: str) -> None:
    destination_path = os.path.realpath(destination)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            target_path = os.path.realpath(os.path.join(destination, member.name))
            if target_path != destination_path and not target_path.startswith(destination_path + os.sep):
                raise RuntimeError(f"Unsafe path in release archive: {member.name}")
        archive.extractall(destination)


def install_release_update(update: UpdateInfo) -> str:
    prefix = os.environ.get("PREFIX") or str(Path.home() / ".local")
    install_command = None
    with TemporaryDirectory(prefix="twingate-gui-update-") as workdir:
        archive_path = os.path.join(workdir, os.path.basename(update.asset_url.split("?", 1)[0]))
        request = Request(
            update.asset_url,
            headers={"User-Agent": f"twingate-linux-gui/{APP_VERSION}"},
        )
        with urlopen(request, timeout=60) as response, open(archive_path, "wb") as output:
            shutil.copyfileobj(response, output)

        safe_extract_tar(archive_path, workdir)

        for root, _dirs, files in os.walk(workdir):
            if "install.sh" in files:
                install_command = [os.path.join(root, "install.sh")]
                break
        if not install_command:
            raise RuntimeError("Release archive did not contain install.sh.")

        env = os.environ.copy()
        env["PREFIX"] = prefix
        result = subprocess.run(
            install_command,
            text=True,
            capture_output=True,
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Installer failed.")
        return os.path.join(prefix, "bin", "twingate-gui")


def email_from_account_id(account_id: str | None) -> str | None:
    if not account_id:
        return None
    return account_id.split(":", 1)[0]


def tenant_url_from_account_id(account_id: str) -> str:
    slug = account_id.split(":", 1)[1] if ":" in account_id else ""
    return f"https://{slug}.twingate.com" if slug else "chrome://newtab"


def twingate_env(account_id: str | None = None, include_browser: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    if include_browser and os.path.exists(CHROME_PROFILE_PICKER):
        env["BROWSER"] = CHROME_PROFILE_PICKER
    email = email_from_account_id(account_id)
    if email:
        env["TWINGATE_AUTH_EMAIL"] = email
        env["TWINGATE_AUTH_ACCOUNT_ID"] = account_id
    return env


def run_twingate(
    *args: str,
    input_text: str | None = None,
    browser_account_id: str | None = None,
    include_browser: bool = True,
) -> CommandResult:
    command = ["twingate", "--disable-colors", *args]
    try:
        proc = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=60,
            env=twingate_env(browser_account_id, include_browser=include_browser),
        )
        return CommandResult(
            command,
            proc.returncode,
            strip_ansi(proc.stdout.strip()),
            strip_ansi(proc.stderr.strip()),
        )
    except FileNotFoundError as exc:
        return CommandResult(command, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return CommandResult(command, 124, "", "Command timed out.")


def run_twingate_auth(resource_name: str, account_id: str) -> CommandResult:
    command = ["twingate", "--disable-colors", "auth", resource_name]
    output = []
    launched_url = False

    try:
        proc = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=twingate_env(account_id, include_browser=False),
        )
    except FileNotFoundError as exc:
        return CommandResult(command, 127, "", str(exc))

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            clean = strip_ansi(line.rstrip())
            output.append(clean)
            if not launched_url:
                match = URL_RE.search(clean)
                if match:
                    launch_chrome_for_account(account_id, match.group(0))
                    launched_url = True
        returncode = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return CommandResult(command, 124, "\n".join(output), "Authentication timed out.")

    return CommandResult(command, returncode, "\n".join(output), "")


def run_twingate_account_add(network: str, allow_diagnostics: bool = False) -> CommandResult:
    command = ["twingate", "--disable-colors", "account", "add", "--disable-network-verification"]
    network = network.strip().removeprefix("https://").removeprefix("http://")
    network = network.strip().strip("/")
    network = network.split("/", 1)[0]
    network = network.removesuffix(".twingate.com").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", network):
        return CommandResult(command, 2, "", "Enter only the Twingate network name, for example 'acme'.")

    diagnostics_answer = "y\n" if allow_diagnostics else "n\n"
    prompt_answers = [
        ("do you want to change it?", "y\n"),
        ("enter the name of your twingate network", f"{network}\n"),
        ("do you want to switch to this account now?", "y\n"),
        ("restart now?", "y\n"),
        ("press [enter] to continue", "\n"),
        ("do you want to try to connect anyway?", "n\n"),
        ("diagnostic", diagnostics_answer),
        ("analytics", diagnostics_answer),
        ("telemetry", diagnostics_answer),
    ]
    answered: dict[str, float] = {}
    output: list[str] = []
    output_text = ""
    returncode = 124
    pid = None
    master_fd = None

    try:
        pid, master_fd = pty.fork()
        if pid == 0:
            os.execvpe(command[0], command, twingate_env(include_browser=True))

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            ready, _write, _error = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                text = strip_ansi(chunk.decode("utf-8", errors="replace"))
                output.append(text)
                output_text += text
                lowered = output_text.lower()
                now = time.monotonic()
                for prompt, answer in prompt_answers:
                    if prompt in lowered and now - answered.get(prompt, 0) > 1.5:
                        os.write(master_fd, answer.encode("utf-8"))
                        answered[prompt] = now
            try:
                done_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done_pid = pid
                status = 0
            if done_pid == pid:
                returncode = os.waitstatus_to_exitcode(status)
                break
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    except FileNotFoundError as exc:
        return CommandResult(command, 127, "", str(exc))
    except OSError as exc:
        return CommandResult(command, 1, "".join(output), str(exc))
    finally:
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if pid:
            try:
                done_pid, status = os.waitpid(pid, os.WNOHANG)
                if done_pid == pid and returncode == 124:
                    returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                pass

    text = "".join(output).strip()
    if returncode == 124:
        return CommandResult(command, returncode, text, "Account setup timed out.")
    return CommandResult(command, returncode, text, "")


def parse_resources(output: str) -> list[dict[str, str]]:
    rows = []
    reader = csv.reader(StringIO(output), delimiter="\t")
    headers = None
    for raw in reader:
        cols = [col.strip() for col in raw]
        if not cols or not any(cols):
            continue
        if headers is None:
            headers = cols
            continue
        while len(cols) < 4:
            cols.append("")
        rows.append(
            {
                "name": cols[0],
                "address": cols[1],
                "alias": cols[2],
                "auth": cols[3],
            }
        )
    return rows


def parse_accounts(output: str) -> list[Account]:
    accounts = []
    reader = csv.reader(StringIO(output), delimiter="\t")
    headers = None
    for raw in reader:
        cols = [col.strip() for col in raw]
        if not cols or not any(cols):
            continue
        if headers is None:
            headers = cols
            continue
        while len(cols) < 4:
            cols.append("")
        accounts.append(
            Account(
                email=cols[0],
                network=cols[1],
                network_url=cols[2],
                active=any(col == "*" for col in cols[3:]),
            )
        )
    return accounts


def active_account_id() -> str | None:
    result = run_twingate("account", "list")
    if result.returncode != 0:
        return None
    active = next((account for account in parse_accounts(result.stdout) if account.active), None)
    return active.account_id if active else None


def launch_chrome_for_account(account_id: str, url: str | None = None) -> None:
    if not os.path.exists(CHROME_PROFILE_PICKER):
        return
    subprocess.Popen([CHROME_PROFILE_PICKER, url or tenant_url_from_account_id(account_id)], env=twingate_env(account_id))


def connection_details() -> ConnectionDetails:
    interface = "sdwan0"
    tunnel_ip = "Unavailable"
    route_count = 0

    try:
        addr = subprocess.run(
            ["ip", "-4", "addr", "show", "dev", interface],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if addr.returncode == 0:
            match = re.search(r"\binet\s+([0-9.]+)/", addr.stdout)
            if match:
                tunnel_ip = match.group(1)
        else:
            interface = "Unavailable"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        interface = "Unavailable"

    if interface != "Unavailable":
        try:
            routes = subprocess.run(
                ["ip", "route", "show", "dev", interface],
                text=True,
                capture_output=True,
                timeout=5,
            )
            if routes.returncode == 0:
                route_count = len([line for line in routes.stdout.splitlines() if line.strip()])
        except (FileNotFoundError, subprocess.TimeoutExpired):
            route_count = 0

    return ConnectionDetails(interface=interface, tunnel_ip=tunnel_ip, route_count=route_count)


def switch_account_command(account_id: str, open_browser: bool = True) -> CommandResult:
    switch = run_twingate("account", "switch", account_id, input_text="y\ny\n", browser_account_id=account_id)
    active = active_account_id()
    status = run_twingate("status")
    start = None

    if active == account_id and status.stdout != "online":
        start = run_twingate("start", input_text="y\n", browser_account_id=account_id)
        status = run_twingate("status")

    stdout_parts = [switch.stdout]
    stderr_parts = [switch.stderr]
    if start:
        stdout_parts.append(start.stdout)
        stderr_parts.append(start.stderr)
    stdout_parts.append(f"Active account: {active or 'unknown'}")
    stdout_parts.append(f"Status: {status.stdout or status.stderr or 'unknown'}")

    if active == account_id and status.stdout == "online":
        if open_browser:
            launch_chrome_for_account(account_id)
        return CommandResult(
            switch.command,
            0,
            "\n\n".join(part for part in stdout_parts if part),
            "\n\n".join(part for part in stderr_parts if part),
        )

    return CommandResult(
        switch.command,
        switch.returncode or (start.returncode if start else status.returncode),
        "\n\n".join(part for part in stdout_parts if part),
        "\n\n".join(part for part in stderr_parts if part),
    )


class TwingateGui(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None
        self.status_label = None
        self.account_label = None
        self.active_network_label = None
        self.active_email_label = None
        self.resource_count_label = None
        self.pending_count_label = None
        self.last_refresh_label = None
        self.update_box = None
        self.update_label = None
        self.update_button = None
        self.connection_status_detail_label = None
        self.connection_account_detail_label = None
        self.connection_ip_detail_label = None
        self.connection_interface_detail_label = None
        self.connection_routes_detail_label = None
        self.connection_resources_detail_label = None
        self.connection_action_stack = None
        self.connect_button = None
        self.disconnect_button = None
        self.start_button = None
        self.stop_button = None
        self.summary_network_label = None
        self.summary_user_label = None
        self.summary_tunnel_label = None
        self.summary_resources_label = None
        self.summary_refresh_label = None
        self.resource_search_entry = None
        self.resource_filter_mode = "all"
        self.resource_filter_buttons = {}
        self.resources_filter = None
        self.account_cards_box = None
        self.accounts_tree = None
        self.resources_tree = None
        self.content_box = None
        self.resources_store = None
        self.accounts_store = None
        self.output_buffer = None
        self.indicator = None
        self.indicator_menu = None
        self.status_item = None
        self.connect_item = None
        self.disconnect_item = None
        self.start_item = None
        self.stop_item = None
        self.last_status = "unknown"
        self.refresh_running = False
        self.update_check_running = False
        self.update_install_running = False
        self.update_info = None
        self.update_status = f"Version {APP_VERSION}"
        self.menu_accounts = None
        self.menu_active = None
        self.menu_online = False
        self.menu_resource_count = 0
        self.menu_pending_count = 0
        self.menu_details = None
        self.auto_auth_inflight = set()
        self.auto_auth_attempted = {}

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        Notify.init(APP_NAME)
        self._load_css()
        self._build_indicator()
        GLib.timeout_add_seconds(30, self._periodic_refresh)
        GLib.timeout_add_seconds(6 * 60 * 60, self._periodic_update_check)
        GLib.timeout_add_seconds(15, self._initial_update_check)

    def _load_css(self):
        css = b"""
        .app-root {
            background: #1a1a1d;
            color: #e8e8ec;
        }

        .top-bar {
            background: #1a1a1d;
            border-bottom: 1px solid #2f2f36;
        }

        .title {
            color: #e8e8ec;
            font-size: 20px;
            font-weight: 700;
        }

        .subtitle {
            color: #8a8a93;
            font-size: 13px;
        }

        .section-title {
            color: #e8e8ec;
            font-size: 19px;
            font-weight: 700;
        }

        .panel {
            background: #222227;
            border: 1px solid #2f2f36;
            border-radius: 8px;
        }

        .sidebar {
            background: #1a1a1d;
        }

        .metric {
            background: #222227;
            border: 1px solid #2f2f36;
            border-radius: 8px;
        }

        .metric-value {
            color: #e8e8ec;
            font-size: 16px;
            font-weight: 700;
        }

        .metric-label {
            color: #8a8a93;
            font-size: 11px;
        }

        .status-strip {
            background: #222227;
            border: 1px solid #2f2f36;
            border-radius: 8px;
        }

        .summary-item {
            background: #1a1a1d;
            border: 1px solid #2f2f36;
            border-radius: 8px;
        }

        .network-card {
            background: #1a1a1d;
            color: #e8e8ec;
            border: 1px solid #3a3a42;
            padding: 14px;
        }

        .network-card-active {
            border: 1px solid #3b82f6;
        }

        .network-menu-button {
            background: #222227;
            color: #e8e8ec;
            border: 1px solid #2f2f36;
            padding: 4px 8px;
        }

        .network-avatar {
            background: #ffffff;
            color: #0b0b0d;
            font-size: 20px;
            font-weight: 700;
            padding: 16px;
        }

        .status-online {
            color: #e8e8ec;
            background: #222227;
            border: 1px solid #2f2f36;
            font-weight: 700;
            padding: 5px 10px;
        }

        .status-offline {
            color: #b91c1c;
            background: #fee2e2;
            font-weight: 700;
            padding: 5px 10px;
        }

        button.primary {
            background: #0b0b0d;
            color: #ffffff;
            border: 1px solid #2f2f36;
            font-weight: 700;
            padding: 7px 12px;
        }

        button.action-primary {
            background: #0b0b0d;
            color: #ffffff;
            border: 1px solid #2f2f36;
            font-weight: 700;
            padding: 10px 12px;
        }

        button.action-danger {
            background: #2a1f22;
            color: #f4f4f5;
            border: 1px solid #493238;
            font-weight: 700;
            padding: 10px 12px;
        }

        button.secondary {
            background: #222227;
            color: #e8e8ec;
            border: 1px solid #2f2f36;
            padding: 7px 12px;
        }

        .resource-table,
        .account-table {
            background: #222227;
            color: #e8e8ec;
        }

        .content-wrap {
            background: #1a1a1d;
        }

        .muted {
            color: #8a8a93;
        }

        .accent {
            color: #3b82f6;
        }

        .connection-details {
            background: #1a1a1d;
            border: 1px solid #2f2f36;
            border-radius: 8px;
        }

        .filter-bar {
            background: #1a1a1d;
            border: 1px solid #2f2f36;
            border-radius: 8px;
        }

        entry.search {
            background: #222227;
            color: #e8e8ec;
            border: 1px solid #2f2f36;
            padding: 8px 10px;
        }

        button.filter-active {
            background: #0b0b0d;
            color: #ffffff;
            border: 1px solid #3b82f6;
            font-weight: 700;
        }

        .update-panel {
            background: #1a1a1d;
            border: 1px solid #3b82f6;
            border-radius: 8px;
        }

        .detail-value {
            color: #e8e8ec;
            font-size: 13px;
            font-weight: 600;
        }

        textview {
            background: #111113;
            color: #e5e7eb;
            font-family: monospace;
            font-size: 12px;
        }
        """
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except GLib.GError as exc:
            print(f"Unable to load application CSS: {exc}")

    def do_activate(self):
        self.show_window()
        self.refresh()

    def _build_indicator(self):
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID, ICON_OFFLINE, AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        if os.path.isdir(ASSET_DIR):
            self.indicator.set_icon_theme_path(ASSET_DIR)
        self.indicator.set_icon_full(ICON_OFFLINE, APP_NAME)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator_menu = Gtk.Menu()
        self._rebuild_indicator_menu()
        self.indicator.set_menu(self.indicator_menu)

    def _menu_item(self, label: str, callback=None, sensitive: bool = True) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        if callback:
            item.connect("activate", callback)
        return item

    def _rebuild_indicator_menu(
        self,
        accounts: list[Account] | None = None,
        active: Account | None = None,
        online: bool = False,
        resource_count: int = 0,
        pending_count: int = 0,
        details: ConnectionDetails | None = None,
    ):
        if self.indicator_menu is None:
            return
        for child in self.indicator_menu.get_children():
            self.indicator_menu.remove(child)

        status_text = "online" if online else self.last_status
        self.status_item = self._menu_item(f"Status: {status_text}", sensitive=False)
        self.indicator_menu.append(self.status_item)
        active_text = f"Active: {active.network} / {active.email}" if active else "Active: none"
        self.indicator_menu.append(self._menu_item(active_text, sensitive=False))
        resource_text = f"Resources: {resource_count}"
        if pending_count:
            resource_text = f"{resource_text} ({pending_count} need auth)"
        self.indicator_menu.append(self._menu_item(resource_text, sensitive=False))
        if details:
            tunnel_text = f"Tunnel: {details.tunnel_ip} on {details.interface}"
            self.indicator_menu.append(self._menu_item(tunnel_text, sensitive=False))
        self.indicator_menu.append(self._menu_item(f"App: {self.update_status}", sensitive=False))

        self.indicator_menu.append(Gtk.SeparatorMenuItem())
        self.indicator_menu.append(self._menu_item("Open Twingate", lambda _item: self.show_window()))
        self.indicator_menu.append(
            self._menu_item("Open Active Network", lambda _item: self.open_active_account_browser(), sensitive=active is not None)
        )
        self.indicator_menu.append(self._menu_item("Refresh", lambda _item: self.refresh()))
        self.indicator_menu.append(self._menu_item("Check for Updates", lambda _item: self.check_for_updates(manual=True), sensitive=not self.update_check_running and not self.update_install_running))
        if self.update_info is not None:
            self.indicator_menu.append(
                self._menu_item(
                    f"Install Update {self.update_info.tag}",
                    lambda _item: self.install_update(),
                    sensitive=not self.update_install_running,
                )
            )

        self.indicator_menu.append(Gtk.SeparatorMenuItem())
        self.indicator_menu.append(self._menu_item("Switch Account", sensitive=False))
        if accounts:
            for account in accounts:
                prefix = "✓" if account.active else "Switch to"
                label = f"{prefix} {account.network} ({account.email})"
                self.indicator_menu.append(
                    self._menu_item(
                        label,
                        lambda _item, account_id=account.account_id: self.switch_account(account_id),
                        sensitive=not account.active,
                    )
                )
        else:
            self.indicator_menu.append(self._menu_item("No accounts found", sensitive=False))
        self.indicator_menu.append(self._menu_item("Add Account...", lambda _item: self.show_add_account_dialog()))

        self.indicator_menu.append(Gtk.SeparatorMenuItem())
        self.connect_item = self._menu_item("Connect", lambda _item: self.run_action("connect"), sensitive=not online)
        self.disconnect_item = self._menu_item("Disconnect", lambda _item: self.run_action("disconnect"), sensitive=online)
        self.indicator_menu.append(self.connect_item)
        self.indicator_menu.append(self.disconnect_item)
        self.start_item = self._menu_item("Start Service", lambda _item: self.run_action("start"), sensitive=not online)
        self.stop_item = self._menu_item("Stop Service", lambda _item: self.run_action("stop"), sensitive=online)
        self.indicator_menu.append(self.start_item)
        self.indicator_menu.append(self.stop_item)

        self.indicator_menu.append(Gtk.SeparatorMenuItem())
        self.indicator_menu.append(self._menu_item("Setup...", lambda _item: self.open_terminal("setup")))
        self.indicator_menu.append(self._menu_item("Quit", lambda _item: self.quit()))
        self.indicator_menu.show_all()

    def _rebuild_current_indicator_menu(self):
        self._rebuild_indicator_menu(
            self.menu_accounts,
            self.menu_active,
            self.menu_online,
            self.menu_resource_count,
            self.menu_pending_count,
            self.menu_details,
        )

    def show_window(self):
        if self.window is None:
            self.window = self._create_window()
        self.window.present()

    def _label(self, text: str = "", css_class: str | None = None, xalign: float = 0) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_xalign(xalign)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        if css_class:
            label.get_style_context().add_class(css_class)
        return label

    def _button(self, label: str, callback, css_class: str = "secondary", icon: str | None = None) -> Gtk.Button:
        button = Gtk.Button()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        if icon:
            image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            box.pack_start(image, False, False, 0)
        box.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(box)
        button.get_style_context().add_class(css_class)
        button.connect("clicked", callback)
        return button

    def _icon_button(self, icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        button.set_tooltip_text(tooltip)
        button.get_style_context().add_class("secondary")
        button.connect("clicked", callback)
        return button

    def _metric(self, title: str, value: str) -> tuple[Gtk.Box, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(12)
        box.get_style_context().add_class("metric")
        box.set_size_request(120, 64)
        value_label = self._label(value, "metric-value")
        title_label = self._label(title, "metric-label")
        box.pack_start(value_label, False, False, 0)
        box.pack_start(title_label, False, False, 0)
        return box, value_label

    def _summary_item(self, title: str, value: str) -> tuple[Gtk.Box, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_border_width(12)
        box.set_hexpand(True)
        box.get_style_context().add_class("summary-item")
        title_label = self._label(title, "metric-label")
        value_label = self._label(value, "detail-value")
        box.pack_start(title_label, False, False, 0)
        box.pack_start(value_label, False, False, 0)
        return box, value_label

    def _detail_row(self, title: str, value: str = "-") -> tuple[Gtk.Box, Gtk.Label]:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_label = self._label(title, "muted")
        title_label.set_width_chars(15)
        value_label = self._label(value, "detail-value", 1)
        row.pack_start(title_label, False, False, 0)
        row.pack_start(value_label, True, True, 0)
        return row, value_label

    def _network_card(self, account: Account) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        card.set_border_width(20)
        card.get_style_context().add_class("network-card")
        if account.active:
            card.get_style_context().add_class("network-card-active")

        avatar = self._label((account.network or account.email or "?")[0].upper(), "network-avatar", 0.5)
        avatar.set_size_request(58, 58)
        card.pack_start(avatar, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.pack_start(text, True, True, 0)
        text.pack_start(self._label(account.network, "title"), False, False, 0)
        text.pack_start(self._label(account.network_url, "subtitle"), False, False, 0)
        text.pack_start(self._label(account.email, "subtitle"), False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.pack_end(actions, False, False, 0)
        if account.active:
            active = self._label("● Active", "accent", 1)
            active.set_width_chars(10)
            actions.pack_start(active, False, False, 0)

        menu_button = Gtk.MenuButton()
        menu_button.set_tooltip_text(f"Actions for {account.network}")
        menu_button.get_style_context().add_class("network-menu-button")
        menu_button.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu = Gtk.Menu()
        switch_item = self._menu_item(
            "Switch",
            lambda _item, account_id=account.account_id: self.switch_account(account_id),
            sensitive=not account.active,
        )
        browser_item = self._menu_item(
            "Open Browser",
            lambda _item, account_id=account.account_id: launch_chrome_for_account(account_id),
        )
        refresh_item = self._menu_item("Refresh Resources", lambda _item: self.refresh())
        menu.append(switch_item)
        menu.append(browser_item)
        menu.append(refresh_item)
        menu.show_all()
        menu_button.set_popup(menu)
        actions.pack_end(menu_button, False, False, 0)

        return card

    def _column(
        self,
        title: str,
        index: int,
        width: int | None = None,
        visible: bool = True,
        auth_index: int | None = None,
    ) -> Gtk.TreeViewColumn:
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        renderer.set_property("foreground", "#e8e8ec")
        column = Gtk.TreeViewColumn(title, renderer, text=index)
        if auth_index is not None:
            column.set_cell_data_func(renderer, self._resource_cell_style, auth_index)
        column.set_resizable(True)
        column.set_sort_column_id(index)
        column.set_visible(visible)
        if width:
            column.set_min_width(width)
        return column

    def _resource_cell_style(self, _column, renderer, model, tree_iter, auth_index: int):
        auth = str(model[tree_iter][auth_index] or "").lower()
        if "pending" in auth:
            renderer.set_property("foreground", "#f59e0b")
            renderer.set_property("weight", 700)
        else:
            renderer.set_property("foreground", "#e8e8ec")
            renderer.set_property("weight", 400)

    def _create_window(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("Twingate")
        window.set_default_size(1200, 780)
        window.set_size_request(980, 640)
        if hasattr(window, "set_wmclass"):
            window.set_wmclass(APP_CLASS, APP_NAME)
        if os.path.exists(ICON_FILE):
            window.set_icon_from_file(ICON_FILE)
        else:
            window.set_icon_name(ICON_ONLINE)
        window.connect("delete-event", self._hide_on_close)
        window.connect("size-allocate", self._resize_content)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class("app-root")
        window.add(root)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Twingate")
        header.set_subtitle("Secure access client")
        window.set_titlebar(header)

        header.pack_start(self._icon_button("view-refresh-symbolic", "Refresh", lambda _button: self.refresh()))
        header.pack_start(self._icon_button("software-update-available-symbolic", "Check for updates", lambda _button: self.check_for_updates(manual=True)))
        header.pack_start(self._icon_button("web-browser-symbolic", "Open account browser", lambda _button: self.open_active_account_browser()))
        header.pack_end(self._button("Add Account", lambda _button: self.show_add_account_dialog(), icon="list-add-symbolic"))

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        top.set_border_width(22)
        top.get_style_context().add_class("top-bar")
        root.pack_start(top, False, False, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        top.pack_start(left, True, True, 0)
        left.pack_start(self._label("Twingate", "title"), False, False, 0)

        self.status_label = self._label("Checking", "status-offline", 0.5)
        self.status_label.set_width_chars(12)
        self.status_label.set_size_request(130, 36)
        top.pack_end(self.status_label, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class("content-wrap")
        root.pack_start(scroll, True, True, 0)

        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.content_box.set_border_width(28)
        self.content_box.set_halign(Gtk.Align.CENTER)
        self.content_box.set_hexpand(True)
        self.content_box.set_size_request(760, -1)
        scroll.add(self.content_box)

        self.update_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.update_box.set_border_width(14)
        self.update_box.get_style_context().add_class("update-panel")
        self.update_box.set_hexpand(True)
        self.update_label = self._label("Update available", "detail-value")
        self.update_box.pack_start(self.update_label, True, True, 0)
        self.update_button = self._button("Install Update", lambda _button: self.install_update(), "primary", "software-update-available-symbolic")
        self.update_box.pack_end(self.update_button, False, False, 0)
        self.content_box.pack_start(self.update_box, False, False, 0)
        self.update_box.set_no_show_all(True)
        self.update_box.hide()

        status_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status_strip.set_border_width(12)
        status_strip.set_hexpand(True)
        status_strip.get_style_context().add_class("status-strip")
        self.content_box.pack_start(status_strip, False, False, 0)
        for box, label_attr in [
            self._summary_item("Network", "None"),
            self._summary_item("User", "None"),
            self._summary_item("Tunnel", "Unavailable"),
            self._summary_item("Resources", "0"),
            self._summary_item("Last refresh", "Never"),
        ]:
            status_strip.pack_start(box, True, True, 0)
            if self.summary_network_label is None:
                self.summary_network_label = label_attr
            elif self.summary_user_label is None:
                self.summary_user_label = label_attr
            elif self.summary_tunnel_label is None:
                self.summary_tunnel_label = label_attr
            elif self.summary_resources_label is None:
                self.summary_resources_label = label_attr
            elif self.summary_refresh_label is None:
                self.summary_refresh_label = label_attr

        top_cards = Gtk.FlowBox()
        top_cards.set_selection_mode(Gtk.SelectionMode.NONE)
        top_cards.set_column_spacing(18)
        top_cards.set_row_spacing(18)
        top_cards.set_min_children_per_line(1)
        top_cards.set_max_children_per_line(2)
        top_cards.set_homogeneous(True)
        top_cards.set_hexpand(True)
        self.content_box.pack_start(top_cards, False, False, 0)

        account_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        account_card.set_border_width(20)
        account_card.get_style_context().add_class("panel")
        account_card.set_size_request(460, -1)
        account_card.set_hexpand(True)
        top_cards.add(account_card)

        account_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        account_card.pack_start(account_head, False, False, 0)
        account_head.pack_start(Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        account_head.pack_start(self._label("Network", "section-title"), True, True, 0)

        account_identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        account_card.pack_start(account_identity, False, False, 0)
        self.active_network_label = self._label("No active network", "title")
        self.active_email_label = self._label("", "subtitle")
        account_identity.pack_start(self.active_network_label, False, False, 0)
        account_identity.pack_start(self.active_email_label, False, False, 0)

        self.account_cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        account_card.pack_start(self.account_cards_box, False, False, 0)

        account_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        account_card.pack_start(account_actions, False, False, 0)
        for button in [
            self._button("Add Account", lambda _button: self.show_add_account_dialog(), "primary", "list-add-symbolic"),
            self._button("Open Browser", lambda _button: self.open_active_account_browser(), icon="web-browser-symbolic"),
            self._button("Setup", lambda _button: self.open_terminal("setup"), icon="preferences-system-symbolic"),
        ]:
            button.set_hexpand(True)
            account_actions.pack_start(button, True, True, 0)

        connection_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        connection_card.set_border_width(20)
        connection_card.get_style_context().add_class("panel")
        connection_card.set_size_request(300, -1)
        connection_card.set_hexpand(True)
        top_cards.add(connection_card)
        connection_card.pack_start(self._label("Connection", "section-title"), False, False, 0)

        self.connection_action_stack = Gtk.Stack()
        self.connection_action_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.connection_action_stack.set_transition_duration(160)
        self.connect_button = self._button(
            "Connect",
            lambda _button: self.run_action("connect"),
            "action-primary",
            "network-transmit-receive-symbolic",
        )
        self.disconnect_button = self._button(
            "Disconnect",
            lambda _button: self.run_action("disconnect"),
            "action-danger",
            "network-offline-symbolic",
        )
        for name, button in [("connect", self.connect_button), ("disconnect", self.disconnect_button)]:
            button.set_hexpand(True)
            self.connection_action_stack.add_named(button, name)
        self.connection_action_stack.set_visible_child_name("connect")
        connection_card.pack_start(self.connection_action_stack, False, False, 0)

        connection_grid = Gtk.Grid()
        connection_grid.set_column_spacing(10)
        connection_grid.set_row_spacing(10)
        self.start_button = self._button("Start", lambda _button: self.run_action("start"), icon="media-playback-start-symbolic")
        self.stop_button = self._button("Stop", lambda _button: self.run_action("stop"), icon="media-playback-stop-symbolic")
        for button, left in [
            (self.start_button, 0),
            (self.stop_button, 1),
        ]:
            button.set_hexpand(True)
            connection_grid.attach(button, left, 0, 1, 1)
        connection_card.pack_start(connection_grid, False, False, 0)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        details.set_border_width(14)
        details.get_style_context().add_class("connection-details")
        connection_card.pack_start(details, False, False, 0)
        for row, label_attr in [
            self._detail_row("State", "Checking"),
            self._detail_row("Active account"),
            self._detail_row("Tunnel IP"),
            self._detail_row("Interface"),
            self._detail_row("Routes"),
            self._detail_row("Resources"),
        ]:
            details.pack_start(row, False, False, 0)
            if self.connection_status_detail_label is None:
                self.connection_status_detail_label = label_attr
            elif self.connection_account_detail_label is None:
                self.connection_account_detail_label = label_attr
            elif self.connection_ip_detail_label is None:
                self.connection_ip_detail_label = label_attr
            elif self.connection_interface_detail_label is None:
                self.connection_interface_detail_label = label_attr
            elif self.connection_routes_detail_label is None:
                self.connection_routes_detail_label = label_attr
            elif self.connection_resources_detail_label is None:
                self.connection_resources_detail_label = label_attr

        resources_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        resources_card.set_border_width(20)
        resources_card.get_style_context().add_class("panel")
        resources_card.set_hexpand(True)
        self.content_box.pack_start(resources_card, True, True, 0)

        resource_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        resources_card.pack_start(resource_head, False, False, 0)
        resource_head.pack_start(Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        resource_head.pack_start(self._label("Resources", "section-title"), True, True, 0)

        metrics = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        resources_card.pack_start(metrics, False, False, 0)
        resource_metric, self.resource_count_label = self._metric("Resources", "0")
        pending_metric, self.pending_count_label = self._metric("Need auth", "0")
        refresh_metric, self.last_refresh_label = self._metric("Last refresh", "Never")
        metrics.pack_start(resource_metric, False, False, 0)
        metrics.pack_start(pending_metric, False, False, 0)
        metrics.pack_start(refresh_metric, False, False, 0)

        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        filter_bar.set_border_width(10)
        filter_bar.get_style_context().add_class("filter-bar")
        resources_card.pack_start(filter_bar, False, False, 0)

        self.resource_search_entry = Gtk.SearchEntry()
        self.resource_search_entry.set_placeholder_text("Search resources, addresses, aliases")
        self.resource_search_entry.set_hexpand(True)
        self.resource_search_entry.get_style_context().add_class("search")
        self.resource_search_entry.connect("search-changed", lambda _entry: self._refilter_resources())
        filter_bar.pack_start(self.resource_search_entry, True, True, 0)

        for mode, label in [("all", "All"), ("pending", "Needs Auth"), ("ready", "Authenticated")]:
            toggle = Gtk.ToggleButton(label=label)
            toggle.get_style_context().add_class("secondary")
            toggle.connect("toggled", self._resource_filter_toggled, mode)
            self.resource_filter_buttons[mode] = toggle
            filter_bar.pack_start(toggle, False, False, 0)
        self.resource_filter_buttons["all"].set_active(True)

        self.resources_store = Gtk.ListStore(str, str, str, str, str)
        self.resources_filter = self.resources_store.filter_new()
        self.resources_filter.set_visible_func(self._resource_visible)
        self.resources_tree = Gtk.TreeView(model=self.resources_filter)
        self.resources_tree.set_headers_visible(True)
        self.resources_tree.get_style_context().add_class("resource-table")
        self.resources_tree.set_enable_search(True)
        self.resources_tree.connect("row-activated", self._resource_activated)
        self.resources_tree.append_column(self._column("Account", 0, visible=False, auth_index=4))
        self.resources_tree.append_column(self._column("Resource", 1, 220, auth_index=4))
        self.resources_tree.append_column(self._column("Address", 2, 190, auth_index=4))
        self.resources_tree.append_column(self._column("Alias", 3, 180, auth_index=4))
        self.resources_tree.append_column(self._column("Authentication", 4, 170, auth_index=4))

        resource_scroll = Gtk.ScrolledWindow()
        resource_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        resource_scroll.set_min_content_height(280)
        resource_scroll.get_style_context().add_class("panel")
        resource_scroll.add(self.resources_tree)
        resources_card.pack_start(resource_scroll, True, True, 0)

        self.output_buffer = Gtk.TextBuffer()
        output = Gtk.TextView(buffer=self.output_buffer)
        output.set_editable(False)
        output.set_monospace(True)
        output.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        output_scroll = Gtk.ScrolledWindow()
        output_scroll.set_min_content_height(110)
        output_scroll.get_style_context().add_class("panel")
        output_scroll.add(output)
        expander = Gtk.Expander(label="Activity")
        expander.add(output_scroll)
        resources_card.pack_start(expander, False, False, 0)

        window.show_all()
        return window

    def _resize_content(self, _window, allocation):
        if self.content_box is None:
            return
        width = max(760, min(1220, allocation.width - 56))
        self.content_box.set_size_request(width, -1)

    def _hide_on_close(self, window, _event):
        window.hide()
        return True

    def _resource_filter_toggled(self, button: Gtk.ToggleButton, mode: str):
        if not button.get_active():
            return
        self.resource_filter_mode = mode
        for other_mode, other_button in self.resource_filter_buttons.items():
            if other_mode != mode:
                other_button.set_active(False)
            context = other_button.get_style_context()
            if other_mode == mode:
                context.add_class("filter-active")
            else:
                context.remove_class("filter-active")
        self._refilter_resources()

    def _refilter_resources(self):
        if self.resources_filter is not None:
            self.resources_filter.refilter()

    def _resource_visible(self, model, tree_iter, _data=None) -> bool:
        search = ""
        if self.resource_search_entry is not None:
            search = self.resource_search_entry.get_text().strip().lower()
        fields = [str(model[tree_iter][index] or "") for index in range(1, 5)]
        auth = fields[3].lower()
        if self.resource_filter_mode == "pending" and "pending" not in auth:
            return False
        if self.resource_filter_mode == "ready" and "pending" in auth:
            return False
        if search and search not in " ".join(fields).lower():
            return False
        return True

    def _resource_activated(self, tree, path, _column):
        model = tree.get_model()
        row = model[path]
        account_id = row[0]
        resource_name = row[1]
        auth_status = row[4]
        if account_id and resource_name:
            if "pending" not in auth_status.lower():
                self.notify("Resource does not need authentication", f"{resource_name}: {auth_status}")
                return
            self.switch_and_auth(account_id, resource_name)

    def _account_activated(self, tree, path, _column):
        model = tree.get_model()
        row = model[path]
        account_id = row[3]
        if account_id:
            self.switch_account(account_id)

    def switch_selected_account(self):
        if not self.accounts_tree:
            return
        selection = self.accounts_tree.get_selection()
        model, tree_iter = selection.get_selected()
        if tree_iter:
            self.switch_account(model[tree_iter][3])

    def refresh(self):
        if self.refresh_running:
            return
        self.refresh_running = True
        self._set_output("Refreshing status, accounts, and resources...")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _periodic_refresh(self):
        self.refresh()
        return True

    def _initial_update_check(self):
        self.check_for_updates(manual=False)
        return False

    def _periodic_update_check(self):
        self.check_for_updates(manual=False)
        return True

    def _refresh_worker(self):
        status = run_twingate("status")
        accounts = run_twingate("account", "list")
        parsed_accounts = parse_accounts(accounts.stdout) if accounts.returncode == 0 else []
        original = next((account for account in parsed_accounts if account.active), None)
        all_resources = []

        if original:
            resources = run_twingate("resources")
            parsed_resources = parse_resources(resources.stdout) if resources.returncode == 0 else []
            all_resources.append((original, resources, parsed_resources))

        details = connection_details()
        GLib.idle_add(self._apply_refresh, status, accounts, parsed_accounts, all_resources, details)

    def _apply_refresh(
        self,
        status: CommandResult,
        accounts_result: CommandResult,
        accounts: list[Account],
        all_resources,
        details: ConnectionDetails,
    ):
        self.refresh_running = False
        self.last_status = status.stdout or "unknown"
        online = self.last_status.lower() == "online"
        icon = ICON_ONLINE if online else ICON_OFFLINE
        self.indicator.set_icon_full(icon, APP_NAME)
        self.status_item.set_label(f"Status: {self.last_status}")
        if self.status_label is not None:
            context = self.status_label.get_style_context()
            context.remove_class("status-online")
            context.remove_class("status-offline")
            context.add_class("status-online" if online else "status-offline")
            self.status_label.set_text("● Connected" if online else "● Offline")
        if self.connect_item is not None:
            self.connect_item.set_sensitive(not online)
        if self.disconnect_item is not None:
            self.disconnect_item.set_sensitive(online)
        if self.start_item is not None:
            self.start_item.set_sensitive(not online)
        if self.stop_item is not None:
            self.stop_item.set_sensitive(online)
        if self.connection_action_stack is not None:
            self.connection_action_stack.set_visible_child_name("disconnect" if online else "connect")
        if self.start_button is not None:
            self.start_button.set_sensitive(not online)
        if self.stop_button is not None:
            self.stop_button.set_sensitive(online)

        active = next((account for account in accounts if account.active), None)
        if self.account_label is not None:
            self.account_label.set_text(
                f"Active account: {active.email} / {active.network_url}" if active else "No active account"
            )
        if self.active_network_label is not None:
            self.active_network_label.set_text(active.network_url if active else "No active network")
        if self.active_email_label is not None:
            self.active_email_label.set_text(active.email if active else "")

        if self.accounts_store is not None:
            self.accounts_store.clear()
            for account in accounts:
                self.accounts_store.append(
                    [
                        f"{account.email}{' *' if account.active else ''}",
                        account.network,
                        account.network_url,
                        account.account_id,
                        account.active,
                    ]
                )
        if self.account_cards_box is not None:
            for child in self.account_cards_box.get_children():
                self.account_cards_box.remove(child)
            for account in accounts:
                self.account_cards_box.pack_start(self._network_card(account), False, False, 0)
            self.account_cards_box.show_all()

        resource_count = 0
        pending_count = 0
        if self.resources_store is not None:
            self.resources_store.clear()
            for account, result, resources in all_resources:
                if result.returncode != 0:
                    self.resources_store.append(
                        [account.account_id, f"Error: {result.stderr or result.stdout}", "", "", ""]
                    )
                    continue
                for item in resources:
                    resource_count += 1
                    pending = item["auth"].lower() == "pending"
                    if pending:
                        pending_count += 1
                    self.resources_store.append(
                        [
                            account.account_id,
                            item["name"],
                            item["address"],
                            "" if item["alias"] == "-" else item["alias"],
                            "● Pending" if pending else item["auth"] or "Ready",
                        ]
                    )
            if self.resource_count_label is not None:
                self.resource_count_label.set_text(str(resource_count))
            if self.pending_count_label is not None:
                self.pending_count_label.set_text(str(pending_count))
            if self.last_refresh_label is not None:
                refresh_time = datetime.now().strftime("%I:%M %p").lstrip("0")
                self.last_refresh_label.set_text(refresh_time)
            else:
                refresh_time = datetime.now().strftime("%I:%M %p").lstrip("0")
            if self.resources_filter is not None:
                self.resources_filter.refilter()

        active_account = f"{active.email} / {active.network}" if active else "None"
        resources_summary = f"{resource_count} available"
        if pending_count:
            resources_summary = f"{resources_summary}, {pending_count} need auth"
        if self.summary_network_label is not None:
            self.summary_network_label.set_text(active.network if active else "None")
        if self.summary_user_label is not None:
            self.summary_user_label.set_text(active.email if active else "None")
        if self.summary_tunnel_label is not None:
            tunnel = details.tunnel_ip if details.tunnel_ip != "Unavailable" else details.interface
            self.summary_tunnel_label.set_text(tunnel)
        if self.summary_resources_label is not None:
            self.summary_resources_label.set_text(resources_summary)
        if self.summary_refresh_label is not None:
            self.summary_refresh_label.set_text(refresh_time)
        if self.connection_status_detail_label is not None:
            self.connection_status_detail_label.set_text("Online" if online else self.last_status.title())
        if self.connection_account_detail_label is not None:
            self.connection_account_detail_label.set_text(active_account)
        if self.connection_ip_detail_label is not None:
            self.connection_ip_detail_label.set_text(details.tunnel_ip)
        if self.connection_interface_detail_label is not None:
            self.connection_interface_detail_label.set_text(details.interface)
        if self.connection_routes_detail_label is not None:
            self.connection_routes_detail_label.set_text(str(details.route_count))
        if self.connection_resources_detail_label is not None:
            self.connection_resources_detail_label.set_text(resources_summary)

        self.menu_accounts = accounts
        self.menu_active = active
        self.menu_online = online
        self.menu_resource_count = resource_count
        self.menu_pending_count = pending_count
        self.menu_details = details
        self._rebuild_indicator_menu(accounts, active, online, resource_count, pending_count, details)

        if online and active:
            self._maybe_auto_auth_pending(active, all_resources)

        resource_output = "\n\n".join(
            f"[{account.account_id}]\n{result.stdout or result.stderr}"
            for account, result, _resources in all_resources
        )
        output = "\n\n".join(
            part
            for part in [
                f"$ {' '.join(status.command)}\n{status.stdout or status.stderr}",
                f"$ {' '.join(accounts_result.command)}\n{accounts_result.stdout or accounts_result.stderr}",
                resource_output,
            ]
            if part.strip()
        )
        self._set_output(output)
        return False

    def _maybe_auto_auth_pending(self, active: Account, all_resources):
        now = time.monotonic()
        for account, result, resources in all_resources:
            if account.account_id != active.account_id or result.returncode != 0:
                continue
            for item in resources:
                if item["auth"].lower() != "pending":
                    continue
                resource_name = item["name"]
                key = (account.account_id, resource_name)
                last_attempt = self.auto_auth_attempted.get(key, 0)
                if key in self.auto_auth_inflight or now - last_attempt < 120:
                    return
                self.auto_auth_inflight.add(key)
                self.auto_auth_attempted[key] = now
                self._set_output(f"Authentication required: {resource_name}\nOpening browser auth session...")
                threading.Thread(
                    target=self._auto_auth_worker,
                    args=(account.account_id, resource_name, key),
                    daemon=True,
                ).start()
                return

    def _auto_auth_worker(self, account_id: str, resource_name: str, key: tuple[str, str]):
        result = run_twingate_auth(resource_name, account_id)
        GLib.idle_add(self._apply_auto_auth_result, result, key)

    def _apply_auto_auth_result(self, result: CommandResult, key: tuple[str, str]):
        self.auto_auth_inflight.discard(key)
        text = result.stdout or result.stderr or f"Command exited with {result.returncode}"
        self._set_output(f"$ {' '.join(result.command)}\n{text}")
        if result.returncode != 0:
            self.notify("Twingate authentication failed", text)
        self.refresh()
        return False

    def run_action(self, *args: str):
        self._set_output(f"Running: twingate {' '.join(args)}")
        threading.Thread(target=self._action_worker, args=args, daemon=True).start()

    def _action_worker(self, args: tuple[str, ...]):
        input_text = "y\n" if args and args[0] == "start" else None
        result = run_twingate(*args, input_text=input_text)
        GLib.idle_add(self._apply_action_result, result)

    def _apply_action_result(self, result: CommandResult):
        text = result.stdout or result.stderr or f"Command exited with {result.returncode}"
        self._set_output(f"$ {' '.join(result.command)}\n{text}")
        if result.returncode != 0:
            self.notify("Twingate command failed", text)
        self.refresh()
        return False

    def check_for_updates(self, manual: bool = False):
        if self.update_check_running or self.update_install_running:
            return
        self.update_check_running = True
        if manual:
            self.update_status = "Checking for updates..."
            self._set_output("Checking GitHub for Twingate Linux GUI updates...")
            self._sync_update_ui()
        threading.Thread(target=self._update_check_worker, args=(manual,), daemon=True).start()

    def _update_check_worker(self, manual: bool):
        try:
            update = fetch_latest_update()
            error = None
        except Exception as exc:
            update = None
            error = str(exc)
        GLib.idle_add(self._apply_update_check, update, error, manual)

    def _apply_update_check(self, update: UpdateInfo | None, error: str | None, manual: bool):
        self.update_check_running = False
        if error:
            self.update_status = f"Update check failed: {error}"
            if manual:
                self.notify("Update check failed", error)
                self._set_output(self.update_status)
        elif update:
            self.update_info = update
            self.update_status = f"Update {update.tag} available"
            self.notify("Twingate update available", f"{update.tag} is ready to install.")
            self._set_output(f"{self.update_status}\n{update.page_url}")
        else:
            self.update_info = None
            self.update_status = f"Version {APP_VERSION} is current"
            if manual:
                self.notify("Twingate is up to date", self.update_status)
                self._set_output(self.update_status)
        self._sync_update_ui()
        return False

    def install_update(self):
        if self.update_info is None or self.update_install_running:
            return
        self.update_install_running = True
        self.update_status = f"Installing {self.update_info.tag}..."
        self._set_output(self.update_status)
        self._sync_update_ui()
        threading.Thread(target=self._install_update_worker, args=(self.update_info,), daemon=True).start()

    def _install_update_worker(self, update: UpdateInfo):
        try:
            launcher = install_release_update(update)
            error = None
        except Exception as exc:
            launcher = ""
            error = str(exc)
        GLib.idle_add(self._apply_update_install, update, launcher, error)

    def _apply_update_install(self, update: UpdateInfo, launcher: str, error: str | None):
        self.update_install_running = False
        if error:
            self.update_status = f"Update install failed: {error}"
            self.notify("Update install failed", error)
            self._set_output(self.update_status)
            self._sync_update_ui()
            return False

        self.update_status = f"Updated to {update.tag}; restarting..."
        self.notify("Update installed", f"Restarting Twingate {update.tag}.")
        self._set_output(self.update_status)
        self._sync_update_ui()
        if launcher and os.path.exists(launcher):
            subprocess.Popen([launcher])
        self.quit()
        return False

    def _sync_update_ui(self):
        if self.update_box is not None and self.update_label is not None:
            self.update_label.set_text(self.update_status)
            if self.update_button is not None:
                self.update_button.set_sensitive(self.update_info is not None and not self.update_install_running)
            if self.update_info is not None or self.update_install_running:
                self.update_box.show_all()
            else:
                self.update_box.hide()
        self._rebuild_current_indicator_menu()

    def show_add_account_dialog(self):
        parent = self.window if self.window is not None else None
        dialog = Gtk.Dialog(
            title="Add Twingate Account",
            transient_for=parent,
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        add_button = dialog.add_button("Add Account", Gtk.ResponseType.OK)
        add_button.get_style_context().add_class("primary")

        content = dialog.get_content_area()
        content.set_spacing(14)
        content.set_border_width(18)

        intro = self._label("Enter the first part of your Twingate URL. Authentication will open in your browser.", "muted")
        intro.set_line_wrap(True)
        content.pack_start(intro, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(10)
        content.pack_start(grid, False, False, 0)

        network_label = self._label("Network", "detail-value")
        network_label.set_width_chars(12)
        network_entry = Gtk.Entry()
        network_entry.set_placeholder_text("acme")
        network_entry.set_activates_default(True)
        network_entry.set_width_chars(22)
        network_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        network_box.set_hexpand(True)
        network_box.pack_start(network_entry, True, True, 0)
        suffix = self._label(".twingate.com", "detail-value", 0)
        suffix.set_selectable(False)
        suffix.set_margin_start(10)
        network_box.pack_start(suffix, False, False, 0)
        grid.attach(network_label, 0, 0, 1, 1)
        grid.attach(network_box, 1, 0, 1, 1)

        help_text = self._label("Example: enter 'acme' for acme.twingate.com.", "muted")
        help_text.set_line_wrap(True)
        content.pack_start(help_text, False, False, 0)

        diagnostics_check = Gtk.CheckButton()
        diagnostics_check.set_active(False)
        diagnostics_check.set_valign(Gtk.Align.START)
        diagnostics_check.set_margin_top(2)
        diagnostics_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        diagnostics_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        diagnostics_title = self._label("Send diagnostics to Twingate", "detail-value")
        diagnostics_note = self._label("Off by default. Leave this off unless you intentionally want to share diagnostics.", "muted")
        diagnostics_note.set_line_wrap(True)
        diagnostics_title.set_mnemonic_widget(diagnostics_check)
        diagnostics_title.connect("button-press-event", lambda _label, _event: diagnostics_check.set_active(not diagnostics_check.get_active()))
        diagnostics_text.pack_start(diagnostics_title, False, False, 0)
        diagnostics_text.pack_start(diagnostics_note, False, False, 0)
        diagnostics_row.pack_start(diagnostics_check, False, False, 0)
        diagnostics_row.pack_start(diagnostics_text, True, True, 0)
        content.pack_start(diagnostics_row, False, False, 0)

        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        network = network_entry.get_text().strip()
        allow_diagnostics = diagnostics_check.get_active()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return
        if not network:
            self.notify("Network required", "Enter your Twingate network name.")
            return
        self.add_account(network, allow_diagnostics)

    def add_account(self, network: str, allow_diagnostics: bool = False):
        cleaned = network.strip()
        self._set_output(f"Adding Twingate account for network: {cleaned}")
        threading.Thread(target=self._add_account_worker, args=(cleaned, allow_diagnostics), daemon=True).start()

    def _add_account_worker(self, network: str, allow_diagnostics: bool):
        result = run_twingate_account_add(network, allow_diagnostics)
        GLib.idle_add(self._apply_add_account_result, result)

    def _apply_add_account_result(self, result: CommandResult):
        text = result.stdout or result.stderr or f"Command exited with {result.returncode}"
        self._set_output(f"$ {' '.join(result.command)}\n{text}")
        if result.returncode != 0:
            self.notify("Account setup failed", text)
        else:
            self.notify("Account added", "Twingate account setup completed.")
        self.refresh()
        return False

    def switch_account(self, account_id: str):
        self._set_output(f"Switching account: {account_id}")
        threading.Thread(target=self._switch_worker, args=(account_id,), daemon=True).start()

    def _switch_worker(self, account_id: str):
        result = switch_account_command(account_id)
        GLib.idle_add(self._apply_action_result, result)

    def switch_and_auth(self, account_id: str, resource_name: str):
        self._set_output(f"Switching to {account_id}, then authenticating {resource_name}")
        threading.Thread(target=self._switch_and_auth_worker, args=(account_id, resource_name), daemon=True).start()

    def _switch_and_auth_worker(self, account_id: str, resource_name: str):
        switch = switch_account_command(account_id, open_browser=False)
        if switch.returncode != 0:
            GLib.idle_add(self._apply_action_result, switch)
            return
        auth = run_twingate_auth(resource_name, account_id)
        combined = CommandResult(
            command=["twingate", "--disable-colors", "account", "switch", account_id, "&&", "twingate", "auth", resource_name],
            returncode=auth.returncode,
            stdout="\n\n".join(part for part in [switch.stdout, auth.stdout] if part),
            stderr="\n\n".join(part for part in [switch.stderr, auth.stderr] if part),
        )
        GLib.idle_add(self._apply_action_result, combined)

    def open_terminal(self, *args: str):
        terminal = shutil.which("gnome-terminal") or shutil.which("x-terminal-emulator")
        if not terminal:
            self.notify("Terminal not found", "Install gnome-terminal or x-terminal-emulator.")
            return
        browser_prefix = f"BROWSER={shlex.quote(CHROME_PROFILE_PICKER)} " if os.path.exists(CHROME_PROFILE_PICKER) else ""
        command = browser_prefix + " ".join(shlex.quote(part) for part in ["twingate", "--disable-colors", *args])
        if os.path.basename(terminal) == "gnome-terminal":
            subprocess.Popen([terminal, "--", "bash", "-lc", f"{command}; echo; read -p 'Press Enter to close...'"], env=twingate_env())
        else:
            subprocess.Popen([terminal, "-e", f"bash -lc \"{command}; echo; read -p 'Press Enter to close...'\""], env=twingate_env())

    def open_active_account_browser(self):
        account_id = active_account_id()
        if account_id:
            launch_chrome_for_account(account_id)
            return
        self.open_chrome_profile_picker()

    def open_chrome_profile_picker(self):
        if not os.path.exists(CHROME_PROFILE_PICKER):
            self.notify("Chrome profile picker missing", CHROME_PROFILE_PICKER)
            return
        subprocess.Popen([CHROME_PROFILE_PICKER])

    def notify(self, title: str, message: str):
        notification = Notify.Notification.new(title, message[:300], ICON_FILE if os.path.exists(ICON_FILE) else ICON_ONLINE)
        notification.show()

    def _set_output(self, text: str):
        if self.output_buffer:
            stamp = datetime.now().strftime("%I:%M:%S %p").lstrip("0")
            entry = f"[{stamp}] {text.strip()}\n\n"
            end = self.output_buffer.get_end_iter()
            self.output_buffer.insert(end, entry)


def main():
    app = TwingateGui()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
