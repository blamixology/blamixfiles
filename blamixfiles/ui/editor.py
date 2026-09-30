"""Built-in editor: opens local or remote files in a tab, with Pygments syntax
highlighting, find/replace, and Ctrl+S straight back to the server (conflict check,
atomic replace, encoding/line endings/permissions kept)."""
from __future__ import annotations

import bisect
import difflib
import hashlib
import re

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QKeySequence, QPainter, QShortcut,
                           QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QTextEdit, QToolButton, QVBoxLayout, QWidget)

from ..core.textfile import EDIT_LIMIT, EOL_NAMES, VIEW_LIMIT, NotText, decode, encode
from ..core.vfs import Entry
from .fmt import human_size
from .platform_ui import MONO_DEFAULT
from .session import Session
from .theme import C, icon

FULL_LEX_LIMIT = 1_000_000     # whole-document lexing (correct multi-line strings/comments) below this
COMPARE_LIMIT = 2_000_000      # before saving, compare the server copy byte-for-byte up to this size

# token type -> (color, bold, italic); looked up through the token's parents
_STYLE = {
    "Comment": ("#5f6b85", False, True),
    "Comment.Preproc": ("#c49bff", False, False),
    "Keyword": ("#c49bff", False, False),
    "Keyword.Constant": ("#ffb86b", False, False),
    "Keyword.Type": ("#6fd8e8", False, False),
    "Name.Builtin": ("#6fd8e8", False, False),
    "Name.Function": ("#7aa2ff", False, False),
    "Name.Class": ("#f5d37a", False, False),
    "Name.Decorator": ("#6fd8e8", False, False),
    "Name.Tag": ("#ff7b8a", False, False),
    "Name.Attribute": ("#f5d37a", False, False),
    "Name.Variable": ("#ffa3b1", False, False),
    "Name.Constant": ("#ffb86b", False, False),
    "Name.Namespace": ("#f5d37a", False, False),
    "Name.Label": ("#7aa2ff", False, False),
    "Literal.String": ("#7ee0a1", False, False),
    "Literal.String.Escape": ("#ffb86b", False, False),
    "Literal.String.Interpol": ("#ffb86b", False, False),
    "Literal.Number": ("#ffb86b", False, False),
    "Literal": ("#ffb86b", False, False),
    "Operator.Word": ("#c49bff", False, False),
    "Generic.Heading": ("#7aa2ff", True, False),
    "Generic.Subheading": ("#7aa2ff", True, False),
    "Generic.Inserted": ("#7ee0a1", False, False),
    "Generic.Deleted": ("#ff7b8a", False, False),
    "Generic.Emph": ("", False, True),
    "Generic.Strong": ("", True, False),
    "Error": ("#ff5d73", False, False),
}

# line comment prefix by Pygments lexer name (for Ctrl+/)
_COMMENTS = {"//": ("javascript", "typescript", "php", "c", "c++", "java", "go", "rust", "c#", "kotlin",
                    "scss", "swift", "dart", "json5", "groovy"),
             "--": ("sql", "lua", "haskell", "mysql", "postgresql", "plpgsql"),
             ";": ("ini", "asm", "lisp", "clojure", "scheme"),
             "%": ("tex", "latex", "erlang", "matlab")}


def comment_prefix(lexer_name: str) -> str:
    low = lexer_name.lower()
    for prefix, names in _COMMENTS.items():
        if any(low == n or low.startswith(n + " ") for n in names):
            return prefix
    return "#"


def mono_font() -> QFont:
    fams = set(QFontDatabase.families())
    for f in (x.strip() for x in MONO_DEFAULT.split(",")):
        if f in fams:
            font = QFont(f)
            break
    else:
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPointSizeF(10.5)
    font.setStyleHint(QFont.Monospace)
    return font


def guess_lexer(filename: str, text: str):
    try:
        from pygments.lexers import get_lexer_for_filename, guess_lexer_for_filename
        from pygments.lexers.special import TextLexer
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    opts = dict(stripnl=False, stripall=False, ensurenl=False)
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    special = {"nginx.conf": "nginx", "dockerfile": "docker", "makefile": "make", ".env": "bash",
               ".bashrc": "bash", ".profile": "bash", ".zshrc": "bash", ".htaccess": "apacheconf",
               "caddyfile": "text", "jenkinsfile": "groovy", "vagrantfile": "ruby"}
    from pygments.lexers import get_lexer_by_name
    low = base.lower()
    try:
        if low in special:
            return get_lexer_by_name(special[low], **opts)
        if low.endswith((".conf", ".vhost")) and ("server {" in text or "location " in text):
            return get_lexer_by_name("nginx", **opts)
        if low.endswith(".service") or low.endswith(".timer") or low.endswith(".socket"):
            return get_lexer_by_name("ini", **opts)
        if low.startswith(".env"):
            return get_lexer_by_name("bash", **opts)
        return get_lexer_for_filename(base, text[:4000], **opts)
    except ClassNotFound:
        pass
    first = text.split("\n", 1)[0]
    if first.startswith("#!"):
        for key, name in (("python", "python"), ("bash", "bash"), ("sh", "bash"), ("node", "javascript"),
                          ("perl", "perl"), ("ruby", "ruby"), ("php", "php")):
            if key in first:
                return get_lexer_by_name(name, **opts)
    try:
        return guess_lexer_for_filename(base, text[:4000], **opts)
    except ClassNotFound:
        return TextLexer(**opts)


class Highlighter(QSyntaxHighlighter):
    def __init__(self, doc: QTextDocument, lexer):
        super().__init__(doc)
        self.lexer = lexer
        self._cache: dict = {}
        self._blocks: dict[int, list] = {}
        self.full = False
        self._timer = QTimer(singleShot=True, interval=250)
        self._timer.timeout.connect(self.relex)

    def fmt(self, ttype):
        if ttype in self._cache:
            return self._cache[ttype]
        t = ttype
        spec = None
        while t is not None and len(t):
            spec = _STYLE.get(".".join(t))
            if spec:
                break
            t = t.parent
        f = None
        if spec:
            f = QTextCharFormat()
            color, bold, italic = spec
            if color:
                f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            if italic:
                f.setFontItalic(True)
        self._cache[ttype] = f
        return f

    def start(self) -> None:
        doc = self.document()
        self.full = self.lexer is not None and doc.characterCount() < FULL_LEX_LIMIT
        if self.full:
            # only real edits (re-applying formats also reports a 0/0 change)
            doc.contentsChange.connect(lambda _pos, rem, add: (rem or add) and self._timer.start())
            self.relex()
        else:
            self.rehighlight()

    def relex(self) -> None:
        if self.lexer is None:
            return
        text = self.document().toPlainText()
        starts = [0] + [m.end() for m in re.finditer("\n", text)]
        blocks: dict[int, list] = {}
        for index, ttype, value in self.lexer.get_tokens_unprocessed(text):
            f = self.fmt(ttype)
            if f is None or not value:
                continue
            pos, end = index, index + len(value)
            line = bisect.bisect_right(starts, pos) - 1
            while pos < end and line < len(starts):
                line_end = starts[line + 1] - 1 if line + 1 < len(starts) else len(text)
                seg_end = min(end, line_end)
                if seg_end > pos:
                    blocks.setdefault(line, []).append((pos - starts[line], seg_end - pos, f))
                line += 1
                pos = starts[line] if line < len(starts) else end
        self._blocks = blocks
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if self.lexer is None:
            return
        if self.full:
            for start, length, f in self._blocks.get(self.currentBlock().blockNumber(), ()):
                self.setFormat(start, length, f)
            return
        for index, ttype, value in self.lexer.get_tokens_unprocessed(text):
            f = self.fmt(ttype)
            if f is not None:
                self.setFormat(index, len(value), f)


class _Gutter(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):  # noqa: N802
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, e):  # noqa: N802
        ed = self.editor
        p = QPainter(self)
        p.fillRect(e.rect(), QColor(C["bg"]))
        block = ed.firstVisibleBlock()
        num = block.blockNumber()
        top = round(ed.blockBoundingGeometry(block).translated(ed.contentOffset()).top())
        bottom = top + round(ed.blockBoundingRect(block).height())
        cur = ed.textCursor().blockNumber()
        h = ed.fontMetrics().height()
        while block.isValid() and top <= e.rect().bottom():
            if block.isVisible() and bottom >= e.rect().top():
                p.setPen(QColor(C["text"] if num == cur else C["faint"]))
                p.drawText(0, top, self.width() - 10, h, Qt.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(ed.blockBoundingRect(block).height())
            num += 1


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Code")
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.indent_unit = "    "
        self.comment = "#"
        self.gutter = _Gutter(self)
        self.blockCountChanged.connect(self._update_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_line)
        self._update_width()
        self._highlight_line()

    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_width(self, *_a) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def _highlight_line(self) -> None:
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor("#141925"))
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])
        self.gutter.update()

    # ---- editing helpers
    def keyPressEvent(self, e):  # noqa: N802
        if self.isReadOnly():
            return super().keyPressEvent(e)
        cur = self.textCursor()
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & Qt.ShiftModifier:
            line = cur.block().text()
            indent = line[:len(line) - len(line.lstrip(" \t"))]
            if line.rstrip().endswith((":", "{", "[", "(")):
                indent += self.indent_unit
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        if e.key() == Qt.Key_Tab and cur.hasSelection():
            return self._shift_lines(+1)
        if e.key() == Qt.Key_Backtab:
            return self._shift_lines(-1)
        if e.key() == Qt.Key_Tab:
            self.insertPlainText(self.indent_unit)
            return
        super().keyPressEvent(e)

    def _selected_blocks(self):
        cur = self.textCursor()
        doc = self.document()
        first = doc.findBlock(cur.selectionStart())
        last = doc.findBlock(max(cur.selectionStart(), cur.selectionEnd() - (1 if cur.hasSelection() else 0)))
        b = first
        while True:
            yield b
            if b == last or not b.isValid():
                break
            b = b.next()

    def _shift_lines(self, direction: int) -> None:
        cur = self.textCursor()
        cur.beginEditBlock()
        for b in list(self._selected_blocks()):
            c = QTextCursor(b)
            if direction > 0:
                c.insertText(self.indent_unit)
            else:
                text = b.text()
                n = 1 if text.startswith("\t") else len(text) - len(text.lstrip(" "))
                n = min(n, len(self.indent_unit)) if not text.startswith("\t") else 1
                c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, n)
                c.removeSelectedText()
        cur.endEditBlock()

    def toggle_comment(self) -> None:
        blocks = [b for b in self._selected_blocks() if b.text().strip()]
        if not blocks:
            return
        prefix = self.comment
        all_commented = all(b.text().lstrip().startswith(prefix) for b in blocks)
        cur = self.textCursor()
        cur.beginEditBlock()
        for b in blocks:
            text = b.text()
            lead = len(text) - len(text.lstrip())
            c = QTextCursor(b)
            c.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, lead)
            if all_commented:
                n = len(prefix) + (1 if text[lead + len(prefix):lead + len(prefix) + 1] == " " else 0)
                c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, n)
                c.removeSelectedText()
            else:
                c.insertText(prefix + " ")
        cur.endEditBlock()


class FindBar(QWidget):
    def __init__(self, editor: CodeEditor, parent=None):
        super().__init__(parent)
        self.setObjectName("FindBar")
        self.ed = editor
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        self.find = QLineEdit(placeholderText="Find")
        self.repl = QLineEdit(placeholderText="Replace")
        self.case = QCheckBox("Aa")
        self.case.setToolTip("Match case")
        self.regex = QCheckBox(".*")
        self.regex.setToolTip("Regular expression")
        self.info = QLabel("", objectName="Hint")
        lay.addWidget(self.find, 2)
        prev_b = QPushButton("↑")
        next_b = QPushButton("↓")
        lay.addWidget(prev_b)
        lay.addWidget(next_b)
        lay.addWidget(self.case)
        lay.addWidget(self.regex)
        lay.addWidget(self.repl, 2)
        one = QPushButton("Replace")
        every = QPushButton("All")
        lay.addWidget(one)
        lay.addWidget(every)
        lay.addWidget(self.info)
        close = QToolButton()
        close.setIcon(icon("x"))
        close.clicked.connect(self.hide)
        lay.addWidget(close)
        self.find.returnPressed.connect(lambda: self.go(False))
        next_b.clicked.connect(lambda: self.go(False))
        prev_b.clicked.connect(lambda: self.go(True))
        one.clicked.connect(self.replace_one)
        every.clicked.connect(self.replace_all)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.hide, context=Qt.WidgetWithChildrenShortcut)

    def open(self, replace: bool) -> None:
        self.show()
        sel = self.ed.textCursor().selectedText()
        if sel and " " not in sel:
            self.find.setText(sel)
        self.repl.setVisible(replace)
        self.find.setFocus()
        self.find.selectAll()

    def _pattern(self):
        from PySide6.QtCore import QRegularExpression
        text = self.find.text()
        if not self.regex.isChecked():
            text = QRegularExpression.escape(text)
        rx = QRegularExpression(text)
        if not self.case.isChecked():
            rx.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
        return rx

    def go(self, backwards: bool) -> bool:
        if not self.find.text():
            return False
        flags = QTextDocument.FindBackward if backwards else QTextDocument.FindFlag(0)
        rx = self._pattern()
        found = self.ed.find(rx, flags)
        if not found:   # wrap around
            c = self.ed.textCursor()
            c.movePosition(QTextCursor.End if backwards else QTextCursor.Start)
            self.ed.setTextCursor(c)
            found = self.ed.find(rx, flags)
        self.info.setText("" if found else "No matches")
        return found

    def replace_one(self) -> None:
        c = self.ed.textCursor()
        if c.hasSelection() and self._pattern().match(c.selectedText()).hasMatch():
            c.insertText(self.repl.text())
        self.go(False)

    def replace_all(self) -> None:
        if not self.find.text():
            return
        rx = self._pattern()
        c = QTextCursor(self.ed.document())
        c.beginEditBlock()
        n = 0
        cur = self.ed.document().find(rx, 0)
        while not cur.isNull() and cur.hasSelection():
            cur.insertText(self.repl.text())
            n += 1
            cur = self.ed.document().find(rx, cur.position())
        c.endEditBlock()
        self.info.setText(f"{n} replaced")


class DiffDialog(QDialog):
    def __init__(self, name: str, mine: str, theirs: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Changes: {name}")
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        view = QPlainTextEdit(readOnly=True)
        view.setFont(mono_font())
        diff = difflib.unified_diff(theirs.splitlines(), mine.splitlines(),
                                    "on the server", "your version", lineterm="")
        view.setPlainText("\n".join(diff) or "No differences.")
        Highlighter(view.document(), _diff_lexer()).start()
        lay.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignRight)


def _diff_lexer():
    try:
        from pygments.lexers import DiffLexer
        return DiffLexer(stripnl=False, ensurenl=False)
    except ImportError:
        return None


class EditorTab(QWidget):
    title_changed = Signal(str)
    message = Signal(str, bool)
    saved = Signal(str)                  # path

    def __init__(self, session: Session, entry: Entry, parent=None):
        super().__init__(parent)
        self.session = session
        self.entry = entry
        self.path = entry.path
        self.meta = None
        self.base_mtime = 0.0
        self.base_size = 0
        self.base_text = ""
        self.base_hash = ""
        self.loaded = False
        self._saving = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget(objectName="Toolbar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        where = QLabel(f"{session.label}:  {entry.path}")
        where.setObjectName("Muted")
        bl.addWidget(where, 1)
        if session.site is not None and session.site.production:
            bl.addWidget(QLabel("PRODUCTION", objectName="Prod"))
        self.save_btn = QPushButton(icon("save", "#0b0d12"), " Save", objectName="Primary")
        self.save_btn.clicked.connect(self.save)
        self.save_btn.setEnabled(False)
        bl.addWidget(self.save_btn)
        reload_b = QToolButton()
        reload_b.setIcon(icon("refresh"))
        reload_b.setToolTip("Reload from " + ("disk" if session.is_local else "server"))
        reload_b.clicked.connect(self.reload)
        bl.addWidget(reload_b)
        lay.addWidget(bar)
        self.ed = CodeEditor()
        self.ed.setReadOnly(True)
        self.ed.setPlainText("Loading …")
        lay.addWidget(self.ed, 1)
        self.findbar = FindBar(self.ed)
        self.findbar.hide()
        lay.addWidget(self.findbar)
        self.status = QLabel("", objectName="Hint")
        self.status.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.status)
        self.ed.document().modificationChanged.connect(self._modified)
        self.ed.cursorPositionChanged.connect(self._update_status)

        for seq, fn in (("Ctrl+S", self.save), ("Ctrl+F", lambda: self.findbar.open(False)),
                        ("Ctrl+H", lambda: self.findbar.open(True)), ("Ctrl+G", self.goto_line),
                        ("Ctrl+/", self.ed.toggle_comment), ("F3", lambda: self.findbar.go(False)),
                        ("Shift+F3", lambda: self.findbar.go(True))):
            QShortcut(QKeySequence(seq), self, activated=fn, context=Qt.WidgetWithChildrenShortcut)
        self.load()

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def dirty(self) -> bool:
        return self.loaded and self.ed.document().isModified()

    def tab_title(self) -> str:
        return ("• " if self.dirty else "") + self.name

    # ------------------------------------------------------------ load
    def load(self) -> None:
        def fetch(b):
            st = b.stat(self.path)
            if st is None:
                raise FileNotFoundError(2, "File not found", self.path)
            if st.size > VIEW_LIMIT:
                raise NotText(f"{human_size(st.size)} is too big to open here. Download it instead.")
            return st, b.read_bytes(self.path)
        self.session.run(fetch, self._loaded, self._load_failed)

    def _load_failed(self, msg: str) -> None:
        self.ed.setPlainText(f"Can't open this file:\n\n{msg}")
        self.message.emit(msg, True)

    def _loaded(self, result) -> None:
        st, data = result
        try:
            text, meta = decode(data)
        except NotText as e:
            return self._load_failed(str(e))
        self.meta = meta
        self.base_mtime, self.base_size = st.mtime, len(data)
        self.base_hash = hashlib.sha256(data).hexdigest()
        self.base_text = text
        lexer = guess_lexer(self.path, text)
        self.lexer_name = lexer.name if lexer is not None else "Plain text"
        self.ed.comment = comment_prefix(self.lexer_name)
        uses_tabs = sum(1 for ln in text.split("\n", 2000)[:2000] if ln.startswith("\t"))
        uses_spaces = sum(1 for ln in text.split("\n", 2000)[:2000] if ln.startswith("  "))
        self.ed.indent_unit = "\t" if uses_tabs > uses_spaces else ("  " if self._two_space(text) else "    ")
        self.ed.setPlainText(text)
        self.hl = Highlighter(self.ed.document(), lexer)
        self.hl.start()
        read_only = len(data) > EDIT_LIMIT
        self.ed.setReadOnly(read_only)
        self.ed.document().setModified(False)
        self.loaded = True
        self.ed.moveCursor(QTextCursor.Start)
        self._update_status()
        if read_only:
            self.message.emit(f"{self.name} is larger than {human_size(EDIT_LIMIT)}: opened read-only", False)
        self.title_changed.emit(self.tab_title())

    @staticmethod
    def _two_space(text: str) -> bool:
        widths = [len(ln) - len(ln.lstrip(" ")) for ln in text.split("\n", 500)[:500] if ln.startswith(" ")]
        return bool(widths) and min(widths) == 2

    def reload(self) -> None:
        if self.dirty and QMessageBox.question(self, "Reload", "Discard your changes and reload?") != QMessageBox.Yes:
            return
        self.loaded = False
        self.load()

    # ------------------------------------------------------------ save
    def save(self) -> None:
        if not self.loaded or self.ed.isReadOnly() or self._saving:
            return
        text = self.ed.toPlainText()
        try:
            data = encode(text, self.meta)
        except UnicodeEncodeError:
            if QMessageBox.question(
                    self, "Encoding", f"Some characters can't be saved as {self.meta.encoding}. "
                    "Save the file as UTF-8 instead?") != QMessageBox.Yes:
                return
            self.meta.encoding, self.meta.bom = "utf-8", False
            data = encode(text, self.meta)
        self._saving = True
        self.save_btn.setEnabled(False)
        self.status.setText("Checking for changes on the server …" if not self.session.is_local else "Saving …")

        def check(b):
            st = b.stat(self.path)
            if st is None:                 # deleted meanwhile: saving recreates it
                return False, None
            changed = st.size != self.base_size or abs(st.mtime - self.base_mtime) > 1
            theirs = None
            # same size and timestamp (servers often have 1 s resolution): compare the content
            if changed or st.size <= COMPARE_LIMIT:
                theirs = b.read_bytes(self.path) if st.size <= VIEW_LIMIT else None
                if theirs is not None:
                    changed = hashlib.sha256(theirs).hexdigest() != self.base_hash
            return changed, theirs
        self.session.run(check, lambda r: self._checked(r, text, data), self._save_failed)

    def _checked(self, result, text: str, data: bytes) -> None:
        changed, theirs = result
        if changed:
            theirs_text = decode(theirs)[0] if theirs is not None else ""
            box = QMessageBox(QMessageBox.Warning, "Changed on the server",
                              f"<b>{self.name}</b> was changed by someone else since you opened it.",
                              parent=self)
            overwrite = box.addButton("Overwrite", QMessageBox.DestructiveRole)
            diff = box.addButton("Show differences", QMessageBox.ActionRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            while True:
                box.exec()
                if box.clickedButton() is diff:
                    DiffDialog(self.name, text, theirs_text, self).exec()
                    continue
                break
            if box.clickedButton() is not overwrite:
                self._saving = False
                self.save_btn.setEnabled(True)
                self._update_status()
                return

        def write(b):
            b.write_bytes(self.path, data)
            return b.stat(self.path)
        self.status.setText("Saving …")
        self.session.run(write, lambda st: self._saved(st, text, data), self._save_failed)

    def _saved(self, st, text: str, data: bytes) -> None:
        size = len(data)
        self._saving = False
        self.base_mtime = st.mtime if st else self.base_mtime
        self.base_size = st.size if st else size
        self.base_hash = hashlib.sha256(data).hexdigest()
        self.base_text = text
        self.ed.document().setModified(False)
        self._update_status()
        where = "" if self.session.is_local else f" to {self.session.label}"
        self.message.emit(f"Saved {self.name}{where}", False)
        self.saved.emit(self.path)

    def _save_failed(self, msg: str) -> None:
        self._saving = False
        self.save_btn.setEnabled(self.dirty)
        self.status.setText(f"Save failed: {msg}")
        self.message.emit(f"Could not save {self.name}: {msg}", True)

    # ------------------------------------------------------------ misc
    def goto_line(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        n, ok = QInputDialog.getInt(self, "Go to line", "Line:", self.ed.textCursor().blockNumber() + 1,
                                    1, self.ed.blockCount())
        if ok:
            c = QTextCursor(self.ed.document().findBlockByNumber(n - 1))
            self.ed.setTextCursor(c)
            self.ed.centerCursor()

    def _modified(self, _m: bool) -> None:
        self.save_btn.setEnabled(self.dirty and not self.ed.isReadOnly())
        self.title_changed.emit(self.tab_title())

    def _update_status(self) -> None:
        if not self.loaded:
            return
        c = self.ed.textCursor()
        m = self.meta
        enc = m.encoding.upper() + (" BOM" if m.bom else "")
        eol = EOL_NAMES.get(m.eol, "LF") + (" (mixed)" if m.mixed_eol else "")
        ro = " · read-only" if self.ed.isReadOnly() else ""
        self.status.setText(f"Ln {c.blockNumber() + 1}, Col {c.positionInBlock() + 1} · {enc} · {eol} · "
                            f"{self.lexer_name}{ro}")
