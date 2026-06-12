#!/usr/bin/env python3
import csv
import os
import re
import shutil
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Notify", "0.7")
from gi.repository import AppIndicator3, Gdk, GLib, Gtk, Notify, Pango


APP_ID = "io.github.twingate_linux_gui.TwingateGui"
APP_NAME = "Twingate"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(APP_DIR, "assets")
ICON_ONLINE = "twingate-tray-online"
ICON_OFFLINE = "twingate-tray-offline"
ICON_FILE = os.path.join(ASSET_DIR, "twingate-tray-online.svg")
CHROME_PROFILE_PICKER = os.path.join(APP_DIR, "twingate-chrome-profile-picker.py")


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


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
URL_RE = re.compile(r"https://\S+")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


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
        self.connection_status_detail_label = None
        self.connection_account_detail_label = None
        self.connection_ip_detail_label = None
        self.connection_interface_detail_label = None
        self.connection_routes_detail_label = None
        self.connection_resources_detail_label = None
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
        self.last_status = "unknown"
        self.refresh_running = False
        self.auto_auth_inflight = set()
        self.auto_auth_attempted = {}

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        Notify.init(APP_NAME)
        self._load_css()
        self._build_indicator()
        GLib.timeout_add_seconds(30, self._periodic_refresh)

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

        .network-card {
            background: #1a1a1d;
            color: #e8e8ec;
            border: 1px solid #3a3a42;
            padding: 14px;
        }

        .network-card-active {
            border: 1px solid #3b82f6;
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

        self.indicator_menu.append(Gtk.SeparatorMenuItem())
        self.indicator_menu.append(self._menu_item("Open Twingate", lambda _item: self.show_window()))
        self.indicator_menu.append(
            self._menu_item("Open Active Network", lambda _item: self.open_active_account_browser(), sensitive=active is not None)
        )
        self.indicator_menu.append(self._menu_item("Refresh", lambda _item: self.refresh()))

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
        self.indicator_menu.append(
            self._menu_item("Add Account...", lambda _item: self.open_terminal("account", "add"))
        )

        self.indicator_menu.append(Gtk.SeparatorMenuItem())
        self.connect_item = self._menu_item("Connect", lambda _item: self.run_action("connect"), sensitive=not online)
        self.disconnect_item = self._menu_item("Disconnect", lambda _item: self.run_action("disconnect"), sensitive=online)
        self.indicator_menu.append(self.connect_item)
        self.indicator_menu.append(self.disconnect_item)
        self.indicator_menu.append(self._menu_item("Start Service", lambda _item: self.run_action("start")))
        self.indicator_menu.append(self._menu_item("Stop Service", lambda _item: self.run_action("stop")))

        self.indicator_menu.append(Gtk.SeparatorMenuItem())
        self.indicator_menu.append(self._menu_item("Setup...", lambda _item: self.open_terminal("setup")))
        self.indicator_menu.append(self._menu_item("Quit", lambda _item: self.quit()))
        self.indicator_menu.show_all()

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

    def _detail_row(self, title: str, value: str = "-") -> tuple[Gtk.Box, Gtk.Label]:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_label = self._label(title, "muted")
        title_label.set_width_chars(15)
        value_label = self._label(value, "detail-value", 1)
        row.pack_start(title_label, False, False, 0)
        row.pack_start(value_label, True, True, 0)
        return row, value_label

    def _network_card(self, account: Account) -> Gtk.Button:
        button = Gtk.Button()
        button.get_style_context().add_class("network-card")
        if account.active:
            button.get_style_context().add_class("network-card-active")
        button.connect("clicked", lambda _button, account_id=account.account_id: self.switch_account(account_id))

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        row.set_border_width(10)
        button.add(row)

        avatar = self._label((account.network or account.email or "?")[0].upper(), "network-avatar", 0.5)
        avatar.set_size_request(58, 58)
        row.pack_start(avatar, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        row.pack_start(text, True, True, 0)
        text.pack_start(self._label(account.network, "title"), False, False, 0)
        text.pack_start(self._label(account.network_url, "subtitle"), False, False, 0)
        text.pack_start(self._label(account.email, "subtitle"), False, False, 0)

        if account.active:
            active = self._label("● Active", "accent", 1)
            active.set_width_chars(10)
            row.pack_end(active, False, False, 0)

        return button

    def _column(self, title: str, index: int, width: int | None = None, visible: bool = True) -> Gtk.TreeViewColumn:
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        renderer.set_property("foreground", "#e8e8ec")
        column = Gtk.TreeViewColumn(title, renderer, text=index)
        column.set_resizable(True)
        column.set_sort_column_id(index)
        column.set_visible(visible)
        if width:
            column.set_min_width(width)
        return column

    def _create_window(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("Twingate")
        window.set_default_size(1200, 780)
        window.set_size_request(980, 640)
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
        header.pack_start(self._icon_button("web-browser-symbolic", "Open account browser", lambda _button: self.open_active_account_browser()))
        header.pack_end(self._button("Add Account", lambda _button: self.open_terminal("account", "add"), icon="list-add-symbolic"))

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
            self._button("Add Account", lambda _button: self.open_terminal("account", "add"), "primary", "list-add-symbolic"),
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
        connection_grid = Gtk.Grid()
        connection_grid.set_column_spacing(10)
        connection_grid.set_row_spacing(10)
        for button, left, top_row in [
            (self._button("Connect", lambda _button: self.run_action("connect"), icon="network-transmit-receive-symbolic"), 0, 0),
            (self._button("Disconnect", lambda _button: self.run_action("disconnect"), icon="network-offline-symbolic"), 1, 0),
            (self._button("Start", lambda _button: self.run_action("start"), icon="media-playback-start-symbolic"), 0, 1),
            (self._button("Stop", lambda _button: self.run_action("stop"), icon="media-playback-stop-symbolic"), 1, 1),
        ]:
            button.set_hexpand(True)
            connection_grid.attach(button, left, top_row, 1, 1)
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

        self.resources_store = Gtk.ListStore(str, str, str, str, str)
        self.resources_tree = Gtk.TreeView(model=self.resources_store)
        self.resources_tree.set_headers_visible(True)
        self.resources_tree.get_style_context().add_class("resource-table")
        self.resources_tree.set_enable_search(True)
        self.resources_tree.connect("row-activated", self._resource_activated)
        self.resources_tree.append_column(self._column("Account", 0, visible=False))
        self.resources_tree.append_column(self._column("Resource", 1, 220))
        self.resources_tree.append_column(self._column("Address", 2, 190))
        self.resources_tree.append_column(self._column("Alias", 3, 180))
        self.resources_tree.append_column(self._column("Authentication", 4, 170))

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

    def _resource_activated(self, tree, path, _column):
        model = tree.get_model()
        row = model[path]
        account_id = row[0]
        resource_name = row[1]
        auth_status = row[4]
        if account_id and resource_name:
            if auth_status.lower() != "pending":
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
        self._set_output("Refreshing...")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _periodic_refresh(self):
        self.refresh()
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
                    if item["auth"].lower() == "pending":
                        pending_count += 1
                    self.resources_store.append(
                        [
                            account.account_id,
                            item["name"],
                            item["address"],
                            "" if item["alias"] == "-" else item["alias"],
                            item["auth"] or "Ready",
                        ]
                    )
            if self.resource_count_label is not None:
                self.resource_count_label.set_text(str(resource_count))
            if self.pending_count_label is not None:
                self.pending_count_label.set_text(str(pending_count))
            if self.last_refresh_label is not None:
                self.last_refresh_label.set_text(datetime.now().strftime("%I:%M %p").lstrip("0"))

        active_account = f"{active.email} / {active.network}" if active else "None"
        resources_summary = f"{resource_count} available"
        if pending_count:
            resources_summary = f"{resources_summary}, {pending_count} need auth"
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
        result = run_twingate(*args)
        GLib.idle_add(self._apply_action_result, result)

    def _apply_action_result(self, result: CommandResult):
        text = result.stdout or result.stderr or f"Command exited with {result.returncode}"
        self._set_output(f"$ {' '.join(result.command)}\n{text}")
        if result.returncode != 0:
            self.notify("Twingate command failed", text)
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
            self.output_buffer.set_text(text)


def main():
    app = TwingateGui()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
