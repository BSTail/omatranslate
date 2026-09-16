import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class Widget:
    """Small GTK stand-in that rejects invalid parenting operations."""

    def __init__(self, label="", **kwargs):
        self.children = []
        self.parent = None
        self.text = label
        self.visible = True
        self.classes = []
        self.natural_height = 100
        self.size_request = (-1, -1)
        self.parent_changes = 0

    def insert_child_after(self, child, previous):
        assert child.parent is None
        index = 0 if previous is None else self.children.index(previous) + 1
        self.children.insert(index, child)
        child.parent = self
        child.parent_changes += 1

    def append(self, child):
        self.insert_child_after(child, self.children[-1] if self.children else None)

    def remove(self, child):
        assert child.parent is self
        self.children.remove(child)
        child.parent = None
        child.parent_changes += 1

    def get_parent(self):
        return self.parent

    def get_first_child(self):
        return next(iter(self.children), None)

    def get_next_sibling(self):
        siblings = self.parent.children
        index = siblings.index(self) + 1
        return siblings[index] if index < len(siblings) else None

    def set_text(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_visible(self, visible):
        self.visible = visible

    def get_visible(self):
        return self.visible

    def add_css_class(self, name):
        self.classes.append(name)

    def set_css_classes(self, names):
        self.classes = names

    def set_size_request(self, width, height):
        self.size_request = (width, height)

    def get_size_request(self):
        return self.size_request

    def measure(self, *args):
        return (0, self.natural_height if self.children else 0, 0, 0)

    def get_width(self):
        return 420

    def get_vadjustment(self):
        return Mock()

    def __getattr__(self, name):
        if name.startswith("set_") or name == "queue_resize":
            return lambda *args: None
        raise AttributeError(name)


REAL_GTK = os.environ.get("OLT_TEST_REAL_GTK") == "1"
spec = importlib.util.spec_from_file_location(
    "olt._overlay_test_subject",
    Path(__file__).resolve().parents[1] / "src/olt/overlay.py",
)
overlay = importlib.util.module_from_spec(spec)
if REAL_GTK:
    spec.loader.exec_module(overlay)
else:
    gtk = types.SimpleNamespace(
        Box=Widget, Label=Widget, Orientation=types.SimpleNamespace(VERTICAL=1)
    )
    glib = types.SimpleNamespace(
        IO_IN=1, IO_HUP=16, IO_ERR=8, IO_NVAL=32,
        SOURCE_REMOVE=False, SOURCE_CONTINUE=True, idle_add=lambda *args: None,
    )
    gi = types.ModuleType("gi")
    gi.require_version = lambda *args: None
    repository = types.ModuleType("gi.repository")
    repository.Gtk = gtk
    repository.GLib = glib
    repository.Gdk = repository.Gtk4LayerShell = types.SimpleNamespace()
    with patch.dict(sys.modules, {"gi": gi, "gi.repository": repository}):
        spec.loader.exec_module(overlay)


class StdinTests(unittest.TestCase):
    def setUp(self):
        self.app = overlay.OverlayApp.__new__(overlay.OverlayApp)
        self.app._stdin_buffer = bytearray()
        self.app.cards = {}
        self.app.card = Mock(side_effect=lambda card_id, *args: self.app.cards.setdefault(card_id, True))
        self.app.hint = Mock()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        self.addCleanup(os.close, self.read_fd)
        self.addCleanup(self.close_writer)
        self.stderr = io.StringIO()
        # Keep diagnostic output out of the test runner.
        self.redirect = contextlib.redirect_stderr(self.stderr)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def close_writer(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def callback(self, condition=None):
        return self.app._stdin_cb(
            self.read_fd, overlay.GLib.IO_IN if condition is None else condition
        )

    def test_multiple_commands_without_another_write(self):
        os.write(self.write_fd, b'{"cmd":"hint","text":"one"}\n{"cmd":"hint","text":"two"}\n')
        self.assertTrue(self.callback())
        self.assertEqual(self.app.hint.call_args_list, [(('one',),), (('two',),)])
        self.assertTrue(self.callback())  # EAGAIN must not block or remove watch.
        self.assertEqual(self.app.hint.call_count, 2)

    def test_partial_utf8_and_line(self):
        data = json.dumps({"cmd": "hint", "text": "\u65e5\U0001f600"}, ensure_ascii=False).encode()
        split = data.index(b'\xf0') + 2
        os.write(self.write_fd, data[:split])
        self.assertTrue(self.callback())
        self.app.hint.assert_not_called()
        os.write(self.write_fd, data[split:])
        self.assertTrue(self.callback())
        self.app.hint.assert_not_called()
        os.write(self.write_fd, b'\n')
        self.assertTrue(self.callback())
        self.app.hint.assert_called_once_with("\u65e5\U0001f600")

    def test_eof_drains_and_accepts_unterminated_final_line(self):
        os.write(self.write_fd, b'{"cmd":"hint","text":"one"}\n{"cmd":"hint","text":"last"}')
        self.close_writer()
        self.assertFalse(self.callback(overlay.GLib.IO_IN | overlay.GLib.IO_HUP))
        self.assertEqual(self.app.hint.call_count, 2)
        self.app.hint.assert_called_with("last")
        self.assertEqual(self.app._stdin_buffer, b'')

    def test_bad_lines_and_empty_eof(self):
        os.write(self.write_fd, b'\xff\n{bad}\n[]\n\n{"cmd":"hint","text":"ok"}\n\xf0')
        self.close_writer()
        self.assertFalse(self.callback())
        self.app.hint.assert_called_once_with("ok")
        self.assertFalse(self.callback())

    def test_first_card_diagnostic_once_per_card(self):
        for card_id in ("a", "a", "b"):
            self.app.on_line(json.dumps({"cmd": "card", "id": card_id}))
        self.assertEqual(self.app.card.call_count, 3)
        self.assertEqual(self.stderr.getvalue().count("first card received id="), 2)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.app = overlay.OverlayApp.__new__(overlay.OverlayApp)
        self.app.cards = {}
        self.app._shown = []
        self.app.history = True
        self.app._min_scroll_height = 0
        self.app.entries = overlay.Gtk.Box(orientation=overlay.Gtk.Orientation.VERTICAL)
        self.app.hint_label = overlay.Gtk.Label(label="")
        if REAL_GTK:
            self.app.window = overlay.Gtk.Window()
            self.app.scroll = overlay.Gtk.ScrolledWindow()
            self.app.scroll.set_child(self.app.entries)
            self.app.window.set_child(self.app.scroll)
            self.addCleanup(self.app.window.destroy)
        else:
            self.app.window = Widget()
            self.app.scroll = Widget()
        self.app._monitor_size = lambda: (1920, 1080)

    def card(self, card_id, source="source", direction="out"):
        self.app.card(card_id, direction, source, "target", "ready")

    def assert_order(self, expected):
        if REAL_GTK:
            context = overlay.GLib.MainContext.default()
            for _ in range(20):
                if not context.pending():
                    break
                context.iteration(False)
        actual = []
        child = self.app.entries.get_first_child()
        while child is not None:
            actual.append(next(key for key, item in self.app.cards.items() if item[0] == child))
            child = child.get_next_sibling()
        self.assertEqual(actual, expected)
        self.assertEqual(self.app._shown, expected)
        self.assertTrue(self.app.scroll.get_visible())

    def test_history_toggle_new_cards_restore_and_late_updates(self):
        for key in ("a", "b", "c"):
            self.card(key)
        self.assert_order(["c", "b", "a"])
        self.app.set_history(False)
        self.assert_order(["c"])
        self.card("d")
        self.assert_order(["d"])
        original = self.app.cards["a"]
        self.card("a", "late", "in")
        self.assertEqual(self.app.cards["a"], original)
        self.assertIsNone(original[0].get_parent())
        self.assertEqual(original[1].get_text(), "target")
        self.assertEqual(original[2].get_text(), "late")
        self.assert_order(["d"])
        for _ in range(3):
            self.app.set_history(True)
            self.assert_order(["d", "c", "b", "a"])
            self.app.set_history(False)
            self.assert_order(["d"])
        self.app.clear("d")
        self.assert_order(["c"])
        self.app.clear("a")
        self.app.set_history(True)
        self.assert_order(["c", "b"])

    def test_history_disabled_before_first_card_and_clear(self):
        self.app.set_history(False)
        self.card("a")
        self.assert_order(["a"])
        self.app.clear("a")
        self.assert_order([])
        self.assertEqual(self.app._min_scroll_height, 0)
        self.assertFalse(self.app.window.get_visible())
        self.card("b")
        self.app.clear_all()
        self.assert_order([])
        self.assertEqual(self.app.scroll.get_size_request()[1], 0)

    def test_updates_never_reparent_or_reorder(self):
        self.card("a")
        self.card("b")
        original = self.app.cards["a"]
        changes = original[0].parent_changes if not REAL_GTK else None
        for text in ("long " * 100, "short", "streaming"):
            self.card("a", text)
            self.assertEqual(self.app.cards["a"], original)
            self.assertEqual(original[1].get_text(), text)
            self.assert_order(["b", "a"])
        if not REAL_GTK:
            self.assertEqual(original[0].parent_changes, changes)

    @unittest.skipIf(REAL_GTK, "Synthetic measurements use fake widgets")
    def test_panel_floor_is_monotonic_until_empty(self):
        self.card("a")
        self.app.entries.natural_height = 400
        self.card("b")
        self.app.entries.natural_height = 50
        self.card("b", "short")
        self.app.set_history(False)
        self.app.clear("b")
        self.assertEqual(self.app.scroll.get_size_request()[1], 400)
        self.app.entries.natural_height = 5000
        self.card("a")
        self.assertEqual(self.app._min_scroll_height, 1032)
        self.app.entries.natural_height = 0
        self.app.clear("a")
        self.assertEqual(self.app._min_scroll_height, 0)


if __name__ == "__main__":
    unittest.main()
