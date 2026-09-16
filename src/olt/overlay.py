"""Floating bilingual overlay (GTK4 layer-shell).

The overlay only renders; the controller drives it through a small JSON line
protocol over stdin. Each line is a command:

    {"cmd": "card", "id": "out-1", "direction": "out",
     "source": "…", "target": "…", "state": "draft"}
    {"cmd": "state", "id": "out-1", "state": "ready"}
    {"cmd": "clear", "id": "out-1"}
    {"cmd": "clear_all"}
    {"cmd": "hint", "text": "reconnecting…"}

All cards live inside a single window (one themed panel). The panel is capped
at the screen height; when more translations arrive than fit, the content
scrolls.

Theming follows the active Omarchy theme: the overlay reads the current
theme's colors.toml (bg/fg/accent/green) and builds a CSS provider from those
colors. If no Omarchy theme is found it falls back to GTK named colors.
"""

from __future__ import annotations

import json
import os
import sys
import time

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell  # noqa: E402

from .theme import border_gradient_css, load_theme

STATE_CLASS = {
    "draft": "olt-draft",
    "ready": "olt-ready",
    "spoken": "olt-spoken",
    "incoming": "olt-incoming",
}


def build_css(theme: dict) -> str:
    # All colors come from the active Omarchy theme (read dynamically at
    # startup); nothing here is hardcoded. When no theme token is present we
    # fall back to GTK's own named colors so it still follows the GTK theme.
    bg = theme.get("bg", "@theme_bg_color")
    fg = theme.get("fg", "@theme_fg_color")
    light_fg = theme.get("light_fg", theme.get("fg", "@theme_fg_color"))
    accent = theme.get("accent", "@accent_color")
    green = theme.get("green", "@success_color")

    entries = f"""
box.olt-entry {{
    border-bottom: 1px solid alpha({fg}, 0.12);
    padding-bottom: 6px;
}}
box.olt-entry:last-child {{
    border-bottom: none;
    padding-bottom: 0;
}}
label.olt-src {{ color: alpha({light_fg}, 0.65); font-size: 0.9em; }}
label.olt-target {{ color: {fg}; }}
label.olt-draft {{ color: alpha({fg}, 0.55); }}
label.olt-ready {{ color: {fg}; }}
label.olt-spoken {{ color: {green}; }}
label.olt-incoming {{ color: {accent}; }}
"""

    grad = border_gradient_css(theme)
    if grad:
        # Gradient border: the outer box paints the theme's active-border
        # gradient, the inner card paints the solid theme background, and the
        # outer box's padding reveals the gradient as a thin ring.
        return f"""
window.olt-root {{
    background: transparent;
}}
box.card-border {{
    background-image: {grad};
    border-radius: 10px;
    padding: 2px;
}}
box.card {{
    background: alpha({bg}, 0.92);
    border-radius: 8px;
    padding: 8px;
}}
{entries}
"""
    return f"""
window.olt-root {{
    background: transparent;
}}
box.card-border {{ background: none; padding: 0; }}
box.card {{
    background: alpha({bg}, 0.92);
    border: 1px solid alpha({fg}, 0.15);
    border-radius: 8px;
    padding: 8px;
}}
{entries}
"""


class OverlayApp:
    def __init__(self, position: str):
        self.window = Gtk.Window()
        self.window.set_default_size(420, -1)
        self.window.set_title("olt-overlay")

        Gtk4LayerShell.init_for_window(self.window)
        Gtk4LayerShell.set_layer(self.window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_namespace(self.window, "olt-overlay")
        Gtk4LayerShell.set_keyboard_mode(
            self.window, Gtk4LayerShell.KeyboardMode.NONE
        )

        top = "top" in position
        bottom = "bottom" in position
        left = "left" in position
        right = "right" in position
        if top:
            Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.TOP, True)
        if bottom:
            Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.BOTTOM, True)
        if left:
            Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.LEFT, True)
        if right:
            Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.RIGHT, True)
        Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.TOP, 12)
        Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.RIGHT, 12)
        Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.BOTTOM, 12)
        Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.LEFT, 12)

        theme = load_theme()
        self.css = Gtk.CssProvider()
        self.css.load_from_string(build_css(theme))
        Gtk.StyleContext.add_provider_for_display(
            Gtk.Widget.get_display(self.window), self.css, 800
        )

        # The layer-shell window itself must be transparent; otherwise GTK
        # paints a default (black) background behind the themed cards.
        self.window.add_css_class("olt-root")

        # One themed panel holds every translation entry.
        self.border = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.border.add_css_class("card-border")

        self.card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.card_box.add_css_class("card")

        self.hint_label = Gtk.Label(label="")
        self.hint_label.set_wrap(True)
        self.hint_label.set_halign(Gtk.Align.START)
        self.card_box.append(self.hint_label)

        self.entries = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.set_child(self.entries)
        self.card_box.append(self.scroll)

        self.border.append(self.card_box)
        self.window.set_child(self.border)

        self.cards: dict[str, tuple[Gtk.Box, Gtk.Label, Gtk.Label]] = {}
        self._shown: list[str] = []
        self.history = True
        self._stdin_buffer = bytearray()
        # Floor for the panel height so a card never shrinks when its text is
        # replaced by something shorter (e.g. two-tier refine replaces a long
        # streaming draft with a shorter offline transcript). Resets only when
        # the last card is cleared.
        self._min_scroll_height = 0

        self.window.connect("map", lambda *_: self._relayout())

        # Hidden until the first card appears (e.g. F10 press).
        self.window.set_visible(False)

    def _monitor_size(self):
        """Return (width, height) of the monitor the overlay is on, or None.

        Gtk4LayerShell.get_monitor() returns None for this window (the layer
        surface is created by the compositor, not Gdk), so resolve the monitor
        from the window's Wayland surface via Gdk instead, falling back to the
        layer-shell API.
        """
        try:
            surface = self.window.get_surface()
            if surface is not None:
                display = Gdk.Display.get_default()
                if display is not None:
                    monitor = display.get_monitor_at_surface(surface)
                    if monitor is not None:
                        geo = monitor.get_geometry()
                        return (geo.width, geo.height)
        except Exception:
            pass
        try:
            monitor = Gtk4LayerShell.get_monitor(self.window)
            if monitor is not None:
                geo = monitor.get_geometry()
                return (geo.width, geo.height)
        except Exception:
            pass
        return None

    def _relayout(self) -> None:
        """Size the panel to its content, capped at the monitor height.

        The ScrolledWindow does not reliably propagate its child's natural
        height to the layer-shell window (and in-place label updates don't
        queue a resize), so measure the entries box synchronously at the
        content width and pin the scrolled window to that height (capped).
        Content scrolls only once it hits the cap.
        """
        size = self._monitor_size()
        if size is None:
            return
        max_h = max(120, size[1] - 48)
        self.scroll.set_max_content_height(max_h)
        width = self.window.get_width()
        if width <= 0:
            width = 420
        for_size = max(0, width - 24)  # card padding + gradient border
        _, nat, _, _ = self.entries.measure(Gtk.Orientation.VERTICAL, for_size)
        self._min_scroll_height = max(self._min_scroll_height, min(nat, max_h))
        self.scroll.set_size_request(-1, max(self._min_scroll_height, min(nat, max_h)))
        # Newest is prepended at the top; keep it visible.
        adj = self.scroll.get_vadjustment()
        GLib.idle_add(adj.set_value, 0)

    def _apply_history(self) -> None:
        """Show only the newest card, or all cards (scrollable history).

        Entries are physically added/removed from the entries box rather than
        hidden in place: hiding a child inside a GtkScrolledWindow's viewport
        triggers `gtk_widget_is_ancestor` assertions, so we detach instead.
        """
        # Dict insertion order records creation order, not delta arrival order.
        shown = list(reversed(self.cards))
        if not self.history:
            shown = shown[:1]
        wanted = set(shown)
        for card_id in self._shown:
            if card_id not in wanted:
                self.entries.remove(self.cards[card_id][0])
        previous = None
        for card_id in shown:
            entry = self.cards[card_id][0]
            if entry.get_parent() is None:
                self.entries.insert_child_after(entry, previous)
            previous = entry
        self._shown = shown
        self.scroll.set_visible(True)
        self._relayout()

    def _update_visibility(self) -> None:
        visible = bool(self.cards) or bool(self.hint_label.get_text())
        self.window.set_visible(visible)

    # -- rendering ---------------------------------------------------------

    def _label(self, text: str, css_class: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_xalign(0.0)
        label.set_selectable(True)
        label.add_css_class(css_class)
        return label

    def card(self, card_id: str, direction: str, source: str, target: str, state: str):
        state_class = STATE_CLASS.get(state, "olt-draft")

        if card_id in self.cards:
            # Update the existing entry in place. Removing and re-adding a
            # child inside the ScrolledWindow viewport triggers
            # `gtk_widget_is_ancestor` assertions during reflow, so we only
            # ever mutate label text, never reparent.
            entry, top_label, bottom_label = self.cards[card_id]
            if direction == "out":
                top_label.set_text(source)
                bottom_label.set_text(target)
                top_label.set_css_classes(["olt-src"])
                bottom_label.set_css_classes([state_class])
            else:
                top_label.set_text(target)
                bottom_label.set_text(source)
                top_label.set_css_classes([state_class])
                bottom_label.set_css_classes(["olt-src"])
            self._update_visibility()
            # Mutating label text in place does not queue a resize, so the
            # window keeps its old height and clips the new content. Queue the
            # resize on the scrolled content (NOT the window — a window-level
            # queue_resize does not reflow the ScrolledWindow's child) so the
            # window height tracks the updated text.
            self.entries.queue_resize()
            self._relayout()
            return

        entry = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        entry.add_css_class("olt-entry")

        if direction == "out":
            top_label = self._label(source, "olt-src")
            bottom_label = self._label(target, state_class)
        else:
            top_label = self._label(target, state_class)
            bottom_label = self._label(source, "olt-src")
        entry.append(top_label)
        entry.append(bottom_label)

        self.cards[card_id] = (entry, top_label, bottom_label)
        self._apply_history()
        self._update_visibility()

    def state(self, card_id: str, state: str):
        pass

    def clear(self, card_id: str):
        item = self.cards.pop(card_id, None)
        if item is not None:
            entry = item[0]
            if entry.get_parent() is not None:
                self.entries.remove(entry)
        if card_id in self._shown:
            self._shown.remove(card_id)
        if not self.cards:
            self._min_scroll_height = 0
            self.scroll.set_size_request(-1, 0)
        self._apply_history()
        self._update_visibility()

    def clear_all(self):
        for item in self.cards.values():
            entry = item[0]
            if entry.get_parent() is not None:
                self.entries.remove(entry)
        self.cards.clear()
        self._shown = []
        self._min_scroll_height = 0
        self.scroll.set_size_request(-1, 0)
        self._update_visibility()

    def set_history(self, enabled: bool):
        self.history = bool(enabled)
        self._apply_history()

    def hint(self, text: str):
        self.hint_label.set_text(text)
        self.hint_label.set_visible(bool(text))
        self._update_visibility()

    # -- stdin protocol ----------------------------------------------------

    def on_line(self, line: str):
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        cmd = msg.get("cmd")
        if cmd == "card":
            if msg.get("id", "?") not in self.cards:
                print(
                    f"olt-overlay: first card received id={msg.get('id', '?')} "
                    f"monotonic={time.monotonic():.6f}",
                    file=sys.stderr,
                    flush=True,
                )
            self.card(
                msg.get("id", "?"),
                msg.get("direction", "out"),
                msg.get("source", ""),
                msg.get("target", ""),
                msg.get("state", "draft"),
            )
        elif cmd == "clear":
            self.clear(msg.get("id", ""))
        elif cmd == "clear_all":
            self.clear_all()
        elif cmd == "history":
            self.set_history(msg.get("enabled", True))
        elif cmd == "hint":
            self.hint(msg.get("text", ""))

    def run(self):
        fd = sys.stdin.fileno()
        os.set_blocking(fd, False)
        GLib.io_add_watch(
            fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._stdin_cb
        )
        self.window.connect("destroy", lambda *_: self.loop.quit())
        self.loop = GLib.MainLoop()
        self.loop.run()

    def _stdin_cb(self, source, condition):
        # Never mix fd readiness with TextIOWrapper read-ahead. Frame bytes
        # before decoding so even a split UTF-8 codepoint waits for its line.
        eof = False
        while True:
            try:
                chunk = os.read(source, 65536)
            except InterruptedError:
                continue
            except BlockingIOError:
                break
            except OSError:
                eof = True
                break
            if not chunk:
                eof = True
                break
            self._stdin_buffer.extend(chunk)
        lines = self._stdin_buffer.split(b"\n")
        self._stdin_buffer = lines.pop()
        if eof or condition & (GLib.IO_HUP | GLib.IO_ERR | GLib.IO_NVAL):
            # Accept a final command without a newline, then retire the watch.
            lines.append(self._stdin_buffer)
            self._stdin_buffer = bytearray()
            eof = True
        for line in lines:
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            self.on_line(text)
        return GLib.SOURCE_REMOVE if eof else GLib.SOURCE_CONTINUE


def main():
    position = "top-right"
    if len(sys.argv) > 1:
        position = sys.argv[1]
    app = OverlayApp(position)
    app.run()


if __name__ == "__main__":
    main()
