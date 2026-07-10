# video_ai_editor/keyword_scoring_panel.py
"""
Advanced Keyword Scoring editor panel (plan U3).

Qt subsection for keywords.advanced_scoring: master toggle, global matching
knobs (cooldown / overlap / normalization) in a left column, and drag-
reorderable keyword-group cards with chip-style word editing on the right
(R1-R5, R7). All non-Qt behavior lives in modules/keyword_scoring_editor.py
(U2); this widget owns an editor model dict and emits `section_changed`
with the serialized section + persist-gate verdict on every committed edit.
main.py owns persistence (write-through / save_config / Run gate, U5) and
fulfills imports (U6) via `import_requested` + `add_group()`.

Qt pitfall handled here (see the plan's U3 technical design): QListWidget
InternalMove serializes the dragged item through mime data and recreates it,
destroying the attached card widget — and `rowsMoved` never fires on that
path. So each item's Qt.UserRole stores the group's model index as of the
last rebuild (an id would be ambiguous while duplicate-id validation errors
exist); the drop re-derives the model order from that data and a FULL card
rebuild restores the widgets.

modules.keyword_scoring is never imported at module level (it transitively
imports whisper); validation goes through U2's should_persist(), which
imports it lazily and only while the master toggle is on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDoubleSpinBox, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from modules.keyword_scoring_editor import (
    new_group, reset_section, serialize_section, should_persist,
)

# Chips past this count collapse behind a "+N more" expander (U3).
CHIP_COLLAPSE_THRESHOLD = 12
# Two-step delete confirm resets after this long (no-modal-dialog convention).
DELETE_CONFIRM_TIMEOUT_MS = 3000

_ERROR_STYLE = "color: #c33; font-size: 10px;"  # main.py:1668 precedent

_NORMALIZATION_LABELS = (
    ("lowercase", "Lowercase"),
    ("remove_accents", "Remove accents"),
    ("remove_punctuation", "Remove punctuation"),
    ("collapse_whitespace", "Collapse whitespace"),
)

_CHIP_STYLE = (
    "QPushButton{background:#2a3a5a;color:#aaccff;border:none;border-radius:9px;"
    "padding:2px 8px;font-size:10px;text-align:center;}"
    "QPushButton:hover{background:#5a3a3a;color:#ffaaaa;}"
)
_EXPANDER_STYLE = (
    "QPushButton{background:transparent;color:#8899cc;border:1px solid #3a3a5a;"
    "border-radius:9px;padding:2px 8px;font-size:10px;}"
    "QPushButton:hover{border-color:#6688cc;}"
)


class _FlowLayout(QLayout):
    """Minimal wrapping flow layout for the word chips (port of the Qt example).

    No chip widget or wrapping layout exists anywhere in this codebase (KTD7),
    so this is the smallest possible primitive: left-to-right, wrap on
    overflow, height-for-width aware so group cards report a correct
    per-item sizeHint to the QListWidget.
    """

    def __init__(self, parent=None, h_spacing: int = 4, v_spacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802 (Qt override)
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._do_layout(0, 0, width, apply_geometry=False)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect.x(), rect.y(), rect.width(), apply_geometry=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(),
                      margins.top() + margins.bottom())
        return size

    def _do_layout(self, ox: int, oy: int, width: int, apply_geometry: bool) -> int:
        margins = self.contentsMargins()
        x = ox + margins.left()
        y = oy + margins.top()
        right = ox + width - margins.right()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > right and line_height > 0:
                x = ox + margins.left()
                y += line_height + self._v
                line_height = 0
            if apply_geometry:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x += hint.width() + self._h
            line_height = max(line_height, hint.height())
        return (y + line_height + margins.bottom()) - oy


class _GroupListWidget(QListWidget):
    """QListWidget in InternalMove mode hosting the group cards.

    `order_dropped` fires one event-loop turn after a drop completes (by then
    both the insert and the deferred source-row removal have happened), so the
    panel can re-derive model order from item data and run the full rebuild
    that restores the widgets the drop destroyed.
    """

    order_dropped = Signal()
    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSpacing(3)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def dropEvent(self, event):  # noqa: N802
        super().dropEvent(event)
        QTimer.singleShot(0, self.order_dropped.emit)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.resized.emit()


class _GroupCard(QFrame):
    """One keyword-group card (mirrors search_panel._SegmentRow's structure).

    Mutates the group dict it was given IN PLACE (the dict lives inside the
    panel's model) and signals the panel to run the commit flow. id/label
    commit on editingFinished, weight on editingFinished — never per
    keystroke. Deleting a non-empty group is a two-step confirm.
    """

    changed = Signal()           # a field/word edit was committed
    delete_requested = Signal()  # delete confirmed (or one-click for empty)
    size_changed = Signal()      # chip expand/collapse or chip count change

    def __init__(self, group: Dict[str, Any], parent=None):
        super().__init__(parent)
        self._group = group
        self._expanded = False
        self._confirming_delete = False

        self.setObjectName("groupCard")
        self.setStyleSheet(
            "QFrame#groupCard{background:#1a1a2e;border:1px solid #3a3a5a;"
            "border-radius:6px;}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 5)
        root.setSpacing(3)

        # -- header row: drag affordance, id, label, weight, enabled, delete --
        header = QHBoxLayout()
        header.setSpacing(4)

        drag_lbl = QLabel("⠿")
        drag_lbl.setToolTip("Drag to reorder")
        drag_lbl.setStyleSheet("color:#556; font-size:12px;")
        drag_lbl.setCursor(Qt.OpenHandCursor)
        header.addWidget(drag_lbl)

        self.id_input = QLineEdit(str(group.get("id", "") or ""))
        self.id_input.setPlaceholderText("id")
        self.id_input.setToolTip("Machine id (must be unique and non-blank)")
        self.id_input.setFixedWidth(110)
        self.id_input.editingFinished.connect(self._commit_id)
        header.addWidget(self.id_input)

        self.label_input = QLineEdit(str(group.get("label", "") or ""))
        self.label_input.setPlaceholderText("label (optional)")
        self.label_input.setToolTip("Display-only name; ignored by matching")
        self.label_input.editingFinished.connect(self._commit_label)
        header.addWidget(self.label_input, 1)

        weight_lbl = QLabel("weight:")
        weight_lbl.setStyleSheet("color:#8899cc; font-size:10px;")
        header.addWidget(weight_lbl)

        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 1000000.0)
        self.weight_spin.setDecimals(2)
        self.weight_spin.setSingleStep(1.0)
        self.weight_spin.setKeyboardTracking(False)
        try:
            self.weight_spin.setValue(float(group.get("weight", 0)))
        except (TypeError, ValueError):
            self.weight_spin.setValue(0.0)
        self.weight_spin.setFixedWidth(70)
        self.weight_spin.editingFinished.connect(self._commit_weight)
        header.addWidget(self.weight_spin)

        self.enabled_checkbox = QCheckBox("On")
        self.enabled_checkbox.setToolTip("Include this group in matching")
        self.enabled_checkbox.setChecked(bool(group.get("enabled", False)))
        self.enabled_checkbox.toggled.connect(self._commit_enabled)
        header.addWidget(self.enabled_checkbox)

        self.delete_btn = QPushButton("✕")
        self.delete_btn.setFixedWidth(28)
        self.delete_btn.setToolTip("Delete this group")
        self.delete_btn.setStyleSheet("color: #c33; border: none; font-weight: bold;")
        self.delete_btn.clicked.connect(self._on_delete_clicked)
        self.delete_btn.installEventFilter(self)  # clicking elsewhere resets confirm
        header.addWidget(self.delete_btn)

        root.addLayout(header)

        # -- per-card validation errors (hidden while valid) --
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(_ERROR_STYLE)
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        # -- chip area: word pills + trailing add-field in a flow layout --
        self._chip_container = QWidget()
        self._chips_layout = _FlowLayout(self._chip_container)
        root.addWidget(self._chip_container)

        self.add_word_input = QLineEdit()
        self.add_word_input.setPlaceholderText("add word…")
        self.add_word_input.setFixedWidth(110)
        self.add_word_input.setToolTip("Type a word or phrase and press Enter")
        self.add_word_input.returnPressed.connect(self._on_add_word)

        self._confirm_timer = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.setInterval(DELETE_CONFIRM_TIMEOUT_MS)
        self._confirm_timer.timeout.connect(self._reset_delete_button)

        self._rebuild_chips()

    # -- committed field edits (editingFinished, never per keystroke) --------

    def _commit_id(self):
        text = self.id_input.text().strip()
        if text == str(self._group.get("id", "") or ""):
            return
        self._group["id"] = text
        self.changed.emit()

    def _commit_label(self):
        text = self.label_input.text().strip()
        if text == str(self._group.get("label", "") or ""):
            return
        if text:
            self._group["label"] = text
        else:
            # Optional field (R5): drop the key rather than writing label: ''
            self._group.pop("label", None)
        self.changed.emit()

    def _commit_weight(self):
        value = self.weight_spin.value()
        if value == int(value):
            value = int(value)  # keep integral weights integral in YAML
        try:
            current = float(self._group.get("weight", 0))
        except (TypeError, ValueError):
            current = None
        if current is not None and float(value) == current:
            return
        self._group["weight"] = value
        self.changed.emit()

    def _commit_enabled(self, checked: bool):
        self._group["enabled"] = bool(checked)
        self.changed.emit()

    # -- delete (two-step confirm for non-empty groups) ----------------------

    def _on_delete_clicked(self):
        if not (self._group.get("words") or []):
            self.delete_requested.emit()  # fresh empty group: one click
            return
        if self._confirming_delete:
            self._confirm_timer.stop()
            self._reset_delete_button()
            self.delete_requested.emit()
            return
        self._confirming_delete = True
        self.delete_btn.setText("confirm?")
        self.delete_btn.setFixedWidth(64)
        self.delete_btn.setStyleSheet(
            "color: #fff; background: #c33; border: none; font-weight: bold;"
            "border-radius: 3px; padding: 2px;"
        )
        self._confirm_timer.start()

    def _reset_delete_button(self):
        self._confirming_delete = False
        self.delete_btn.setText("✕")
        self.delete_btn.setFixedWidth(28)
        self.delete_btn.setStyleSheet("color: #c33; border: none; font-weight: bold;")

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.delete_btn and event.type() == QEvent.FocusOut:
            if self._confirming_delete:
                self._confirm_timer.stop()
                self._reset_delete_button()
        return super().eventFilter(obj, event)

    # -- chip word editing ----------------------------------------------------

    def _on_add_word(self):
        text = self.add_word_input.text().strip()
        if not text:
            return
        words = self._group.setdefault("words", [])
        if text in words:
            self.add_word_input.clear()  # dupe within this group: ignored
            return
        words.append(text)
        self.add_word_input.clear()
        self._rebuild_chips()
        self.size_changed.emit()
        self.changed.emit()
        self.add_word_input.setFocus()

    def _remove_word(self, word: str):
        words = self._group.get("words") or []
        if word in words:
            words.remove(word)
            self._rebuild_chips()
            self.size_changed.emit()
            self.changed.emit()

    def _toggle_expanded(self):
        self._expanded = not self._expanded
        self._rebuild_chips()
        self.size_changed.emit()  # panel refreshes the QListWidgetItem sizeHint

    def _rebuild_chips(self):
        layout = self._chips_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if widget is self.add_word_input:
                widget.setParent(None)  # persistent; re-added at the end
            else:
                widget.deleteLater()

        words = self._group.get("words") or []
        collapsed = (not self._expanded) and len(words) > CHIP_COLLAPSE_THRESHOLD
        visible = words[:CHIP_COLLAPSE_THRESHOLD] if collapsed else words

        for word in visible:
            chip = QPushButton(f"{word} ✕")
            chip.setFlat(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setStyleSheet(_CHIP_STYLE)
            chip.setToolTip("Remove this word")
            chip.clicked.connect(lambda _=False, w=word: self._remove_word(w))
            layout.addWidget(chip)

        if len(words) > CHIP_COLLAPSE_THRESHOLD:
            hidden = len(words) - CHIP_COLLAPSE_THRESHOLD
            expander = QPushButton(
                "less ▴" if self._expanded else f"+{hidden} more ▾"
            )
            expander.setFlat(True)
            expander.setCursor(Qt.PointingHandCursor)
            expander.setStyleSheet(_EXPANDER_STYLE)
            expander.clicked.connect(self._toggle_expanded)
            layout.addWidget(expander)

        layout.addWidget(self.add_word_input)
        self.add_word_input.show()

    # -- panel-facing helpers --------------------------------------------------

    def group(self) -> Dict[str, Any]:
        return self._group

    def set_errors(self, messages: List[str]):
        if messages:
            self.error_label.setText("\n".join(messages))
            self.error_label.setVisible(True)
        else:
            self.error_label.clear()
            self.error_label.setVisible(False)


class AdvancedScoringPanel(QWidget):
    """The 'Advanced keyword scoring' subsection (Transcript Settings, R1).

    Owns the U2 editor model + coercion flags. Construction with non-empty
    coercion flags renders the resettable parse-error card instead of the
    editor (KTD5). Persistence itself lives in main.py (U5); this widget only
    reports committed state via `section_changed(serialized_dict, gate_ok)`.
    """

    section_changed = Signal(dict, bool)   # (serialized section, gate_ok)
    import_requested = Signal()            # main.py fulfills via add_group()
    master_toggled = Signal(bool)          # simple-keywords grey-out (R6/U4)

    def __init__(self, model: Dict[str, Any], coercion_flags: Optional[List[str]],
                 parent=None):
        super().__init__(parent)
        self._model = model
        self._coercion_flags: List[str] = list(coercion_flags or [])
        self._cards: List[_GroupCard] = []
        self._gate_ok: Optional[bool] = None  # validated lazily (whisper import)
        self._errors: List[Dict[str, Any]] = []
        self._pending_import_enabled = True
        self._pending_import_tooltip = ""
        self._editor_built = False

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 4, 0, 0)
        self._root.setSpacing(4)

        if self._coercion_flags:
            self._build_parse_error_card()
        else:
            self._build_editor_ui()

    # -- public API for main.py (U4-U6) ---------------------------------------

    def current_model(self) -> Dict[str, Any]:
        return self._model

    def is_gate_ok(self) -> bool:
        if self._gate_ok is None:
            self._run_validation()
        return bool(self._gate_ok)

    def current_errors(self) -> List[Dict[str, Any]]:
        if self._gate_ok is None:
            self._run_validation()
        return list(self._errors)

    def has_parse_error(self) -> bool:
        return bool(self._coercion_flags)

    def add_group(self, group_dict: Dict[str, Any]):
        """Append a group (e.g. the U6 import result) and run the commit flow."""
        self._model.setdefault("groups", []).append(group_dict)
        if self._editor_built:
            self._rebuild_cards()
        self._commit()

    def set_import_enabled(self, enabled: bool, tooltip: str = ""):
        self._pending_import_enabled = bool(enabled)
        self._pending_import_tooltip = tooltip
        if self._editor_built:
            self.import_btn.setEnabled(bool(enabled))
            self.import_btn.setToolTip(tooltip)

    # -- parse-error state (KTD5) ----------------------------------------------

    def _build_parse_error_card(self):
        card = QFrame()
        card.setObjectName("parseErrorCard")
        card.setStyleSheet(
            "QFrame#parseErrorCard{background:#2e1a1a;border:1px solid #5a3a3a;"
            "border-radius:6px;}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        title = QLabel(
            "Advanced keyword scoring section could not be parsed — "
            "reset it here or fix config.yaml by hand."
        )
        title.setWordWrap(True)
        title.setStyleSheet("color: #c33; font-weight: bold;")
        layout.addWidget(title)

        detail = QLabel("\n".join(f"• {flag}" for flag in self._coercion_flags))
        detail.setWordWrap(True)
        detail.setStyleSheet("color: #a66; font-size: 10px;")
        layout.addWidget(detail)

        btn_row = QHBoxLayout()
        self.reset_section_btn = QPushButton("Reset section")
        self.reset_section_btn.setToolTip(
            "Replace the unparseable section with an empty default (disabled, "
            "no groups). Nothing is written to config.yaml until your next "
            "valid edit."
        )
        self.reset_section_btn.clicked.connect(self._on_reset_section)
        btn_row.addWidget(self.reset_section_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._parse_error_card = card
        self._root.addWidget(card)

    def _on_reset_section(self):
        # KTD5/U2 contract: reset replaces the in-memory model and clears the
        # flags but does NOT emit section_changed — no disk write until the
        # user's next gated edit.
        self._model = reset_section()
        self._coercion_flags = []
        self._gate_ok = None
        self._errors = []
        card = getattr(self, "_parse_error_card", None)
        if card is not None:
            self._root.removeWidget(card)
            card.deleteLater()
            self._parse_error_card = None
        self._build_editor_ui()

    # -- editor UI --------------------------------------------------------------

    def _build_editor_ui(self):
        # Master toggle (KTD7: checkbox + body show/hide, no collapsible box)
        self.advanced_enabled_checkbox = QCheckBox(
            "Advanced keyword scoring (weighted groups)"
        )
        self.advanced_enabled_checkbox.setChecked(bool(self._model.get("enabled", False)))
        self.advanced_enabled_checkbox.setToolTip(
            "When enabled, keyword matching uses the weighted groups below and "
            "the simple search-keywords list is ignored."
        )
        self._root.addWidget(self.advanced_enabled_checkbox)

        self._body = QWidget()
        body_layout = QHBoxLayout(self._body)
        body_layout.setContentsMargins(18, 0, 0, 0)
        body_layout.setSpacing(10)

        # --- left column: global matching knobs (R2) ---
        left = QVBoxLayout()
        left.setSpacing(4)

        cooldown_row = QHBoxLayout()
        cooldown_lbl = QLabel("Cooldown (s):")
        cooldown_row.addWidget(cooldown_lbl)
        self.cooldown_spin = QDoubleSpinBox()
        self.cooldown_spin.setRange(0.0, 3600.0)
        self.cooldown_spin.setDecimals(1)
        self.cooldown_spin.setSingleStep(0.5)
        self.cooldown_spin.setKeyboardTracking(False)
        try:
            self.cooldown_spin.setValue(float(self._model.get("cooldown_seconds", 5)))
        except (TypeError, ValueError):
            self.cooldown_spin.setValue(5.0)
        self.cooldown_spin.setToolTip(
            "Minimum seconds between two scored matches of the same group"
        )
        self.cooldown_spin.editingFinished.connect(self._commit_cooldown)
        cooldown_row.addWidget(self.cooldown_spin)
        cooldown_row.addStretch()
        left.addLayout(cooldown_row)

        self.overlap_checkbox = QCheckBox("Prevent overlapping matches")
        self.overlap_checkbox.setChecked(
            bool(self._model.get("prevent_overlapping_matches", True))
        )
        self.overlap_checkbox.toggled.connect(self._commit_overlap)
        left.addWidget(self.overlap_checkbox)

        norm_box = QGroupBox("Normalization")
        norm_layout = QVBoxLayout()
        norm_layout.setSpacing(2)
        self.norm_checkboxes: Dict[str, QCheckBox] = {}
        norm_model = self._model.get("normalization") or {}
        for flag, label in _NORMALIZATION_LABELS:
            checkbox = QCheckBox(label)
            checkbox.setChecked(bool(norm_model.get(flag, True)))
            checkbox.toggled.connect(
                lambda checked, f=flag: self._commit_normalization(f, checked)
            )
            self.norm_checkboxes[flag] = checkbox
            norm_layout.addWidget(checkbox)
        norm_box.setLayout(norm_layout)
        left.addWidget(norm_box)
        left.addStretch()

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(180)
        body_layout.addWidget(left_widget)

        # --- right column: group cards (R3, R4) ---
        right = QVBoxLayout()
        right.setSpacing(4)

        header = QHBoxLayout()
        groups_lbl = QLabel("Keyword groups")
        groups_lbl.setStyleSheet("font-weight: bold;")
        header.addWidget(groups_lbl)
        header.addStretch()

        self.import_btn = QPushButton("Import from simple keywords…")
        self.import_btn.setToolTip(
            "Create a new group from the simple search-keywords list, "
            "weighted at the current keyword points value."
        )
        self.import_btn.clicked.connect(self.import_requested.emit)
        self.import_btn.setEnabled(self._pending_import_enabled)
        if self._pending_import_tooltip:
            self.import_btn.setToolTip(self._pending_import_tooltip)
        header.addWidget(self.import_btn)

        self.add_group_btn = QPushButton("+ Add group")
        self.add_group_btn.setToolTip(
            "Add an empty group (starts disabled until it has words)"
        )
        self.add_group_btn.clicked.connect(self._on_add_group_clicked)
        header.addWidget(self.add_group_btn)
        right.addLayout(header)

        self.groups_empty_label = QLabel(
            "No groups yet — add one or import the simple keyword list."
        )
        self.groups_empty_label.setStyleSheet("color: #888; font-size: 10px;")
        right.addWidget(self.groups_empty_label)

        self.group_list = _GroupListWidget()
        self.group_list.setMinimumHeight(140)
        self.group_list.order_dropped.connect(self._on_order_dropped)
        self.group_list.resized.connect(self._refresh_size_hints)
        right.addWidget(self.group_list, 1)

        body_layout.addLayout(right, 1)
        self._root.addWidget(self._body)

        # Section-level validation summary (config-level errors + count)
        self.section_error_label = QLabel()
        self.section_error_label.setWordWrap(True)
        self.section_error_label.setStyleSheet(_ERROR_STYLE)
        self.section_error_label.setVisible(False)
        self._root.addWidget(self.section_error_label)

        self._editor_built = True
        self._rebuild_cards()

        # Both-paths-in-sync: apply the master state without a signal round-trip,
        # then connect (so construction never emits section_changed).
        enabled = bool(self._model.get("enabled", False))
        self._body.setVisible(enabled)
        self._body.setEnabled(enabled)
        self.advanced_enabled_checkbox.toggled.connect(self._on_master_toggled)

    # -- master toggle (KTD7) ----------------------------------------------------

    def _on_master_toggled(self, checked: bool):
        self._model["enabled"] = bool(checked)
        self._body.setVisible(checked)
        self._body.setEnabled(checked)
        self.master_toggled.emit(bool(checked))
        self._commit()

    # -- global knob commits -------------------------------------------------------

    def _commit_cooldown(self):
        value = self.cooldown_spin.value()
        if value == int(value):
            value = int(value)
        try:
            current = float(self._model.get("cooldown_seconds", 5))
        except (TypeError, ValueError):
            current = None
        if current is not None and float(value) == current:
            return
        self._model["cooldown_seconds"] = value
        self._commit()

    def _commit_overlap(self, checked: bool):
        self._model["prevent_overlapping_matches"] = bool(checked)
        self._commit()

    def _commit_normalization(self, flag: str, checked: bool):
        self._model.setdefault("normalization", {})[flag] = bool(checked)
        self._commit()

    # -- group card management -------------------------------------------------------

    def _on_add_group_clicked(self):
        existing_ids = [g.get("id") for g in self._model.get("groups") or []]
        self.add_group(new_group(existing_ids))

    def _on_delete_group(self, group: Dict[str, Any]):
        groups = self._model.get("groups") or []
        for i, candidate in enumerate(groups):
            if candidate is group:
                del groups[i]
                break
        self._rebuild_cards()
        self._commit()

    def _rebuild_cards(self):
        """Full rebuild: the one structural-change path (avoid-list pattern,
        and the recovery step after every InternalMove drop)."""
        self.group_list.clear()  # deletes any surviving item widgets
        self._cards = []
        groups = self._model.get("groups") or []
        for index, group in enumerate(groups):
            card = _GroupCard(group)
            card.changed.connect(self._commit)
            card.delete_requested.connect(
                lambda g=group: self._on_delete_group(g)
            )
            card.size_changed.connect(
                lambda c=card: self._refresh_item_hint(c)
            )
            item = QListWidgetItem()
            # Model index as of THIS rebuild: survives the drop's item
            # serialization (the attached widget does not).
            item.setData(Qt.UserRole, index)
            self.group_list.addItem(item)
            self.group_list.setItemWidget(item, card)
            item.setSizeHint(self._card_hint(card))
            self._cards.append(card)
        self.groups_empty_label.setVisible(not groups)

    def _on_order_dropped(self):
        groups = self._model.get("groups") or []
        order = [
            self.group_list.item(row).data(Qt.UserRole)
            for row in range(self.group_list.count())
        ]
        if sorted(order) != list(range(len(groups))):
            # Inconsistent drop result — restore the model's order verbatim.
            self._rebuild_cards()
            return
        if order == list(range(len(groups))):
            # No-op drop: the widget was still destroyed by the serialization
            # round-trip, so a rebuild is required — but nothing changed.
            self._rebuild_cards()
            return
        self._model["groups"] = [groups[i] for i in order]
        self._rebuild_cards()
        self._commit()

    # -- item size hints (chip wrap + expander) ------------------------------------

    def _card_hint(self, card: _GroupCard) -> QSize:
        hint = card.sizeHint()
        width = self.group_list.viewport().width()
        if width > 16 and card.hasHeightForWidth():
            hint = QSize(width, card.heightForWidth(width))
        return hint

    def _refresh_item_hint(self, card: _GroupCard):
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            if self.group_list.itemWidget(item) is card:
                item.setSizeHint(self._card_hint(card))
                break
        self.group_list.doItemsLayout()

    def _refresh_size_hints(self):
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            card = self.group_list.itemWidget(item)
            if card is not None:
                item.setSizeHint(self._card_hint(card))

    # -- commit flow + validation rendering (R7) ------------------------------------

    def _commit(self):
        """Every committed edit funnels here: validate, render errors, emit."""
        gate_ok, _errors = self._run_validation()
        self.section_changed.emit(serialize_section(self._model), gate_ok)

    def _run_validation(self):
        # should_persist imports modules.keyword_scoring lazily, and only
        # when the master toggle is on (disabled sections always pass).
        gate_ok, errors = should_persist(self._model)
        self._gate_ok = gate_ok
        self._errors = errors
        self._render_errors(errors)
        return gate_ok, errors

    def _render_errors(self, errors: List[Dict[str, Any]]):
        if not self._editor_built:
            return
        per_card: Dict[int, List[str]] = {}
        section_msgs: List[str] = []
        for error in errors:
            group_index = error.get("group_index")
            message = str(error.get("message", ""))
            if isinstance(group_index, int) and 0 <= group_index < len(self._cards):
                per_card.setdefault(group_index, []).append(message)
            else:
                section_msgs.append(message)
        for index, card in enumerate(self._cards):
            card.set_errors(per_card.get(index, []))
        if errors:
            count = len(errors)
            summary = (
                f"{count} validation issue{'s' if count != 1 else ''} — "
                "this section is not saved until fixed (or the master toggle "
                "is turned off)."
            )
            if section_msgs:
                summary += "\n" + "\n".join(section_msgs)
            self.section_error_label.setText(summary)
            self.section_error_label.setVisible(True)
        else:
            self.section_error_label.clear()
            self.section_error_label.setVisible(False)
        self._refresh_size_hints()


__all__ = ["AdvancedScoringPanel"]
