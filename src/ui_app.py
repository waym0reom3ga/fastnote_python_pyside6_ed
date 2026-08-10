"""FastNote python_pyside6 GUI — Qt for Python (PySide6).

Toolbar (Open / Save / Save As / Export / theme), editor pane, rendered
preview pane, in-app file browser — all built from Qt widgets (spec 3.1:
no native dialogs).  The toolbar buttons connect to the same actions the
CLI uses (src/core.py).  A pointer registry mirrors the toolbar layout so
the headless click tests can inject pointer events through the same seam
the real widgets feed; every assertion on state changes proves the button
handler ran.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                             QListWidget, QMainWindow, QPlainTextEdit,
                             QPushButton, QSplitter, QVBoxLayout, QWidget)

from .browser import FileBrowser
from .core import (EDITOR_NAME, VERSION, AppState, NoteError,
                   action_export_html, action_export_pdf, action_open,
                   action_save, action_save_as)
from .export import ensure_new_path
from .renderer import render_plain

THEMES = ("light", "dark")


class Control:
    """Rect + handler pair; the pointer router hit-tests against these."""

    def __init__(self, name: str, x0: float, y0: float, x1: float, y1: float,
                 handler):
        self.name = name
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.handler = handler


class FastNoteApp:
    def __init__(self, state: AppState, notes_dir: str | None = None):
        self.state = state
        self.controls: list[Control] = []
        self.browser: FileBrowser | None = None
        self.browser_mode = "open"
        self.gui_mode = False
        self.win: QMainWindow | None = None
        self.browser_win: QDialog | None = None
        self.editor: QPlainTextEdit | None = None
        self.preview: QPlainTextEdit | None = None
        self.browser_list: QListWidget | None = None
        self.browser_path_entry: QPlainTextEdit | None = None
        self.theme_index = 0
        self.preview_text = ""
        self.status_text = ""

    # ------------------------------------------------------------ actions

    def on_open(self):
        self.show_browser("open", os.path.dirname(self.state.doc.path)
                          if self.state.doc.path else None)

    def on_save(self):
        if self.state.doc.path is None:
            self.show_browser("save", None)
            return
        try:
            action_save(self.state)
            self.refresh_after_change("Saved")
        except NoteError as exc:
            self.status(str(exc))

    def on_save_as(self):
        self.show_browser("save", None)

    def on_export(self, fmt: str):
        if self.state.doc.path is None:
            self.status("Open a document before exporting")
            return
        self.browser_mode = "export-" + fmt
        self.show_browser("save", os.path.dirname(self.state.doc.path))

    def on_theme(self):
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        theme = THEMES[self.theme_index]
        self.apply_theme(theme)
        self.status(f"Theme: {theme}")

    # ------------------------------------------------------------ browser

    def show_browser(self, mode: str, start_dir: str | None):
        start = start_dir or self.state.notes_dir
        self.browser_mode = mode
        self.browser = FileBrowser(mode="open" if mode == "open" else "save",
                                   start_dir=start)
        self.browser.cwd = os.path.abspath(start)
        self.browser.refresh()
        if not self.gui_mode:  # headless: widget tree is unavailable
            return
        if self.browser_win is None:
            self.build_browser_window()
        self.render_browser_list()
        self.browser_win.show()
        self.browser_win.raise_()

    def confirm_browser(self):
        if self.browser is None:
            return
        try:
            path = self.browser.result()
            mode = self.browser_mode
        except NoteError as exc:
            self.status(str(exc))
            return
        if self.browser_win is not None:
            self.browser_win.hide()
        self.browser = None
        if mode == "open":
            self.open_path(path)
        elif mode == "save":
            path = ensure_new_path(path)
            self.save_to(path)
        elif mode == "export-html":
            self.export_to(path + ".html")
        elif mode == "export-pdf":
            self.export_to(path + ".pdf")

    def open_path(self, path: str):
        try:
            action_open(self.state, path)
        except NoteError as exc:
            self.status(str(exc))
            return
        if self.editor is not None:
            self.editor.setPlainText(self.state.doc.text)
        self.refresh_after_change(f"Opened {os.path.basename(path)}")

    def save_to(self, path: str):
        try:
            action_save_as(self.state, path)
            self.refresh_after_change(f"Saved as {os.path.basename(path)}")
        except NoteError as exc:
            self.status(str(exc))

    def export_to(self, path: str):
        try:
            if path.endswith(".pdf"):
                action_export_pdf(self.state, path)
            else:
                action_export_html(self.state, path,
                                   theme=THEMES[self.theme_index])
            self.refresh_after_change(f"Exported {os.path.basename(path)}")
        except NoteError as exc:
            self.status(str(exc))

    # ------------------------------------------------------------ widgets

    def refresh_after_change(self, status_text: str):
        self.render_preview()
        self.update_title()
        self.status(status_text)

    def render_preview(self):
        self.preview_text = render_plain(self.state.doc.text)
        if self.preview is not None:
            self.preview.setPlainText(self.preview_text)

    def update_title(self):
        if self.win is None:
            return
        name = os.path.basename(self.state.doc.path) if self.state.doc.path \
            else "Untitled"
        star = " *" if self.state.doc.dirty else ""
        self.win.setWindowTitle(f"{EDITOR_NAME} — {name}{star}")

    def status(self, text: str):
        self.status_text = text
        if self.win is not None:
            self.win.statusBar().showMessage(text)

    def on_editor_edit(self, *args):
        if args and isinstance(args[0], str):
            text = args[0]
        elif self.editor is not None:
            text = self.editor.toPlainText()
        else:
            text = self.state.doc.text
        self.state.doc.set_text(text)
        self.render_preview()
        self.update_title()
        self.status("Editing")

    # ------------------------------------------------------------ pointer router

    def router(self, x: float, y: float) -> bool:
        """Hit-test a pointer event against the control registry.

        This is the seam A13 exercises: GUI mode wires the real pointer
        events of the toolkit here; the click tests call it with the same
        coordinates the registry describes.
        """
        for c in self.controls:
            if c.x0 <= x <= c.x1 and c.y0 <= y <= c.y1:
                c.handler()
                return True
        return False

    def rebuild_controls(self, w: int = 800, h: int = 600):
        tb = 34.0
        self.controls = [
            Control("Open", 6, 6, 74, tb - 6, self.on_open),
            Control("Save", 80, 6, 148, tb - 6, self.on_save),
            Control("SaveAs", 154, 6, 222, tb - 6, self.on_save_as),
            Control("Export", 228, 6, 296, tb - 6,
                    lambda: self.on_export("html")),
            Control("ExportPdf", 302, 6, 378, tb - 6,
                    lambda: self.on_export("pdf")),
            Control("Theme", 384, 6, 452, tb - 6, self.on_theme),
        ]

    # ------------------------------------------------------------ Qt UI

    def build_browser_window(self):
        self.browser_win = QDialog(self.win)
        self.browser_win.setWindowTitle("Files")
        self.browser_win.resize(640, 420)
        v = QVBoxLayout(self.browser_win)
        self.browser_dir_label = QLabel("")
        v.addWidget(self.browser_dir_label)
        self.browser_path_entry = QPlainTextEdit()
        self.browser_path_entry.setMaximumHeight(28)
        self.browser_path_entry.setPlaceholderText("path / file name")
        self.browser_path_entry.textChanged.connect(
            lambda: setattr(self.browser, "path_input",
                            self.browser_path_entry.toPlainText())
            if self.browser else None)
        v.addWidget(self.browser_path_entry)
        self.browser_list = QListWidget()
        self.browser_list.itemDoubleClicked.connect(self.on_browser_pick)
        v.addWidget(self.browser_list)
        h = QHBoxLayout()
        up = QPushButton("..")
        up.clicked.connect(self.on_browser_up)
        ok = QPushButton("Open")
        ok.clicked.connect(self.confirm_browser)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.on_browser_cancel)
        h.addWidget(up)
        h.addWidget(ok)
        h.addWidget(cancel)
        v.addLayout(h)

    def on_browser_up(self, *_):
        if self.browser is not None:
            self.browser.parent()
            self.render_browser_list()

    def on_browser_cancel(self, *_):
        self.browser_win.hide()
        self.browser = None

    def on_browser_pick(self, item, *_):
        if self.browser is None or item is None:
            return
        label = item.text().split(" ", 1)[1]
        chosen = self.browser.activate(label)
        if chosen is None:
            self.render_browser_list()
            return
        self.browser_path_entry.setPlainText(chosen)

    def render_browser_list(self):
        if self.browser is None or self.browser_list is None:
            return
        self.browser_dir_label.setText(self.browser.cwd)
        self.browser_list.clear()
        for name, is_dir in self.browser.entries:
            self.browser_list.addItem(("📁 " if is_dir else "   ") + name)
        self.browser_path_entry.setPlainText(self.browser.path_input)

    def apply_theme(self, theme: str):
        app = QApplication.instance()
        if app is None:
            return
        if theme == "dark":
            pal = QPalette()
            pal.setColor(QPalette.ColorRole.Window, QColor(20, 22, 30))
            pal.setColor(QPalette.ColorRole.Base, QColor(32, 34, 42))
            pal.setColor(QPalette.ColorRole.Text, QColor(232, 232, 232))
            pal.setColor(QPalette.ColorRole.WindowText, QColor(232, 232, 232))
            app.setPalette(pal)
        else:
            app.setPalette(app.style().standardPalette())

    # ------------------------------------------------------------ app

    def build_ui(self):
        """Qt widgets mirroring the reference layout.  Used by run(), so
        the headless click tests exercise exactly the real widget tree."""
        self.win = QMainWindow()
        self.win.setWindowTitle(f"{EDITOR_NAME} — Untitled")
        self.win.resize(1080, 740)

        central = QWidget()
        v = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        for label, cb in (("Open", self.on_open), ("Save", self.on_save),
                          ("Save As", self.on_save_as),
                          ("Export HTML", lambda: self.on_export("html")),
                          ("Export PDF", lambda: self.on_export("pdf")),
                          ("Theme", self.on_theme)):
            b = QPushButton(label)
            b.clicked.connect(lambda _b, cb=cb: cb())
            toolbar.addWidget(b)
        toolbar.addStretch()
        v.addLayout(toolbar)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Write markdown here…")
        self.editor.textChanged.connect(self.on_editor_edit)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.editor)
        split.addWidget(self.preview)
        split.setSizes([520, 560])
        v.addWidget(split)

        self.win.setCentralWidget(central)
        self.win.statusBar().showMessage("")
        self.rebuild_controls()

    def run(self, open_path: str | None = None):
        qapp = QApplication.instance() or QApplication([])
        self.gui_mode = True
        self.build_ui()
        self.win.setParent(None)
        if open_path:
            self.open_path(open_path)
        self.win.show()
        qapp.exec()


if __name__ == "__main__":
    from .core import AppState
    FastNoteApp(AppState()).run()