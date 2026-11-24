"""Shared, plugin-scoped presentation for MAGOH Spell.

All rules are applied with ``widget.setStyleSheet`` on MAGOH widgets only: the
QGIS application palette and global stylesheet are never touched.

The module holds the visual tokens (the existing MAGOH petrol palette), one
stylesheet built from them, and a few small layout components reused by the
Project window, the side panel and the secondary dialogs. Components only
arrange widgets: no data logic, no signals other than the ones they create.
Text passed in is expected to be already localised by the caller.
"""
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt.QtGui import QIcon, QFont, QFontMetrics
from qgis.PyQt.QtWidgets import (
    QApplication, QWidget, QFrame, QLabel, QHBoxLayout, QVBoxLayout, QToolButton,
    QPushButton, QScrollArea, QFormLayout, QTabWidget, QSizePolicy, QComboBox, QMessageBox,
)


_HERE = Path(__file__).parent
_UI_ICONS = _HERE / "icons" / "ui"

# --- Tokens -----------------------------------------------------------------
# Colours already used by MAGOH Spell up to V1.5, unchanged in V1.6; only their roles are fixed here.
PETROL = "#005f5f"          # primary actions, selection, focus
PETROL_HOVER = "#007171"
PETROL_PRESSED = "#004b4b"
PETROL_DARK = "#064c59"     # brand word, strong headings
PETROL_TEXT = "#075a63"     # text on tinted backgrounds
TINT = "#e2f2f2"            # context/status strips
TINT_HOVER = "#edf5f5"
TINT_SELECTED = "#d9efef"   # list/tree selection
INK = "#24343a"             # body text
INK_SOFT = "#3d555c"
MUTED = "#596970"           # hints and secondary text
MUTED_SOFT = "#718087"      # disabled text
LINE = "#dce4e7"            # hairlines and card borders
LINE_STRONG = "#cbd8dc"     # input borders
SURFACE = "#f8fafb"         # window background
SURFACE_ALT = "#f0f5f6"     # table headers, alternate rows
CARD = "#ffffff"
DISABLED_BG = "#edf1f3"

TOKENS = dict(
    petrol=PETROL, petrol_hover=PETROL_HOVER, petrol_pressed=PETROL_PRESSED,
    petrol_dark=PETROL_DARK, petrol_text=PETROL_TEXT, tint=TINT, tint_hover=TINT_HOVER,
    tint_selected=TINT_SELECTED, ink=INK, ink_soft=INK_SOFT, muted=MUTED,
    muted_soft=MUTED_SOFT, line=LINE, line_strong=LINE_STRONG, surface=SURFACE,
    surface_alt=SURFACE_ALT, card=CARD, disabled_bg=DISABLED_BG,
)

# Spacing scale (logical pixels; Qt applies the display scaling factor).
SPACE_XS, SPACE_S, SPACE_M, SPACE_L = 4, 6, 10, 14
RADIUS = 7
CARD_RADIUS = 10
MIN_BASE_POINTS = 10.0


def _icon_url(name):
    # Forward slashes and quotes: valid on Windows paths with spaces.
    return '"%s"' % (_UI_ICONS / name).as_posix()


STYLE_TEMPLATE = """
QWidget {{ color: {ink}; font-size: {base}pt; }}
QDialog, QWidget#MagohSurface, QWidget#MagohPage {{ background: {surface}; }}
QStackedWidget > QWidget, QScrollArea, QScrollArea > QWidget > QWidget#MagohScrollBody {{ background: {surface}; }}
QScrollArea {{ border: none; }}
QLabel {{ background: transparent; }}

/* Headers and fixed action bars */
QWidget#MagohHeader {{ background: {card}; border-bottom: 1px solid {line}; }}
QLabel#MagohBrand {{ font-size: {brand}pt; font-weight: 700; color: {petrol_dark}; }}
QWidget#MagohDialogHeader {{ background: {petrol}; border-bottom: 3px solid {petrol_dark}; }}
QLabel#MagohDialogTitle {{ font-size: {title}pt; font-weight: 600; color: white; }}
QLabel#MagohDialogSubtitle {{ color: #d3ecea; font-size: {small}pt; }}
QLabel#MagohSubtitle {{ color: {muted}; font-size: {small}pt; }}
QWidget#MagohActions {{ background: {surface}; border-top: 1px solid {line}; }}

/* Text roles */
QLabel#TitleLabel {{ font-size: {title}pt; font-weight: 600; color: {petrol_dark}; }}
QLabel#MagohSectionTitle {{ font-weight: 600; color: {petrol_dark}; }}
QLabel#MagohHint {{ color: {muted}; font-size: {small}pt; }}
QLabel#MagohKey {{ color: {muted}; font-size: {small}pt; }}
QLabel#MagohValue {{ color: {ink}; }}
QLabel#MagohToolTitle {{ font-weight: 600; color: {ink}; }}
QLabel#MagohContext {{ background: {tint}; color: {petrol_text}; border-radius: {radius}px; padding: 7px 9px; }}

/* Cards: one flat level, no nested frames */
QFrame#MagohCard, QFrame#MagohToolRow {{ background: {card}; border: 1px solid {line}; border-radius: {card_radius}px; }}
QFrame#MagohCard QLabel, QFrame#MagohToolRow QLabel {{ background: transparent; }}
QGroupBox {{ background: {card}; border: 1px solid {line}; border-radius: {card_radius}px;
    margin-top: {group_top}px; padding: 6px 2px 2px 2px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 2px;
    padding: 0 0 3px 0; color: {petrol_dark}; background: transparent; }}

/* Buttons */
QPushButton {{ background: {card}; color: {ink}; border: 1px solid {line_strong};
    border-radius: {radius}px; padding: 5px 12px; min-height: {control_h}px; font-weight: 400; }}
QPushButton:hover {{ background: {tint_hover}; border-color: #438888; }}
QPushButton:pressed {{ background: {tint_selected}; }}
QPushButton[primary="true"], QPushButton:checked {{ background: {petrol}; color: white; border: 1px solid {petrol}; font-weight: 600; }}
QPushButton[primary="true"]:hover, QPushButton:checked:hover {{ background: {petrol_hover}; border-color: {petrol_hover}; }}
QPushButton[primary="true"]:pressed {{ background: {petrol_pressed}; }}
QPushButton:disabled, QPushButton[primary="true"]:disabled {{ background: {disabled_bg}; color: {muted_soft}; border-color: {line}; }}
QPushButton[hero="true"] {{ min-height: {hero_h}px; text-align: left; padding: 6px 14px; font-size: {hero}pt; }}
QPushButton[compact="true"] {{ padding: 3px 7px; min-height: {compact_h}px; font-weight: 400; }}
QPushButton#MagohSegLeft, QPushButton#MagohSegRight {{ padding: 3px 10px; min-height: {compact_h}px; font-weight: 600; }}
QPushButton#MagohSegLeft {{ border-top-right-radius: 0; border-bottom-right-radius: 0; }}
QPushButton#MagohSegRight {{ border-top-left-radius: 0; border-bottom-left-radius: 0; border-left: none; }}
/* Keyboard focus: a 2 px ring (light on petrol), padding reduced by 1 px so nothing moves */
QPushButton:focus {{ border: 2px solid {petrol}; padding: 4px 11px; }}
QPushButton[primary="true"]:focus, QPushButton:checked:focus {{ border: 2px solid #9ad3cf; padding: 4px 11px; }}
QPushButton[hero="true"]:focus {{ padding: 5px 13px; }}
QPushButton[compact="true"]:focus {{ padding: 2px 6px; }}
QPushButton#MagohSegLeft:focus, QPushButton#MagohSegRight:focus {{ padding: 2px 9px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 3px; }}
QToolButton:hover {{ background: {tint_hover}; border-color: {line}; }}
QToolButton:pressed {{ background: {tint_selected}; }}
QToolButton:focus {{ border-color: {petrol}; }}

/* Inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {card}; color: {ink};
    border: 1px solid {line_strong}; border-radius: {radius}px; padding: 4px 7px; min-height: {control_h}px;
    selection-background-color: {tint_selected}; selection-color: {petrol_dark}; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: #9fbcc3; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {petrol}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ background: {disabled_bg}; color: {muted_soft}; border-color: {line}; }}
QLineEdit:read-only {{ background: {surface_alt}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; }}
QComboBox::down-arrow {{ image: url({chevron_down}); width: 12px; height: 12px; }}
QComboBox::down-arrow:disabled {{ image: url({chevron_down_disabled}); }}
QComboBox QAbstractItemView {{ background: {card}; color: {ink}; border: 1px solid {line_strong};
    selection-background-color: {tint_selected}; selection-color: {petrol_dark}; outline: none; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; border: none; border-left: 1px solid {line}; border-top-right-radius: {radius}px; background: transparent; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; border-left: 1px solid {line}; border-bottom-right-radius: {radius}px; background: transparent; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: {tint_hover}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({chevron_up}); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({chevron_down}); width: 10px; height: 10px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{ image: url({chevron_up_disabled}); }}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ image: url({chevron_down_disabled}); }}

QCheckBox, QRadioButton {{ spacing: 8px; padding: 2px 0; background: transparent; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {muted_soft}; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid #8ba5ad; border-radius: 4px; background: {card}; }}
QCheckBox::indicator:hover {{ border-color: {petrol}; }}
QCheckBox::indicator:focus {{ border-color: {petrol}; }}
QCheckBox::indicator:checked {{ background: {petrol}; border-color: {petrol}; image: url({check}); }}
QCheckBox::indicator:disabled {{ background: {disabled_bg}; border-color: {line_strong}; }}
QCheckBox::indicator:checked:disabled {{ background: {disabled_bg}; border-color: {line_strong}; image: url({check_disabled}); }}
QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid #8ba5ad; border-radius: 8px; background: {card}; }}
QRadioButton::indicator:checked {{ border: 5px solid {petrol}; width: 6px; height: 6px; }}

/* Navigation: main sections underlined, sub-sections as pills */
QTabWidget::pane {{ border: none; background: {surface}; top: 0px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {muted}; border: none; border-bottom: 2px solid transparent;
    padding: 6px 7px; margin-right: 1px; }}
QTabBar::tab:hover {{ color: {ink}; background: {tint_hover}; }}
QTabBar::tab:selected {{ color: {petrol}; border-bottom-color: {petrol}; background: transparent; }}
QTabBar::tab:focus {{ color: {petrol_dark}; }}
QTabWidget#MagohMainTabs > QTabBar {{ border-bottom: 1px solid {line}; }}
QTabWidget#MagohMainTabs::tab-bar {{ left: 4px; }}
QTabWidget#MagohSubTabs::tab-bar {{ left: 10px; }}
QTabWidget#MagohSubTabs > QTabBar::tab {{ border: 1px solid {line}; border-radius: 12px; padding: 3px 9px;
    margin: 8px 4px 2px 0; color: {ink_soft}; background: {card}; font-size: {small}pt; }}
QTabWidget#MagohSubTabs > QTabBar::tab:hover {{ border-color: #9fcaca; }}
QTabWidget#MagohSubTabs > QTabBar::tab:selected {{ background: {tint}; color: {petrol_text}; border-color: #7fb8b8; }}
QTabBar QToolButton {{ background: {surface}; border: 1px solid {line}; border-radius: 4px; }}

/* Lists, trees and text panes */
QTreeWidget, QTableWidget, QListWidget, QTextBrowser, QPlainTextEdit, QTextEdit, QTreeView, QListView, QTableView {{
    background: {card}; alternate-background-color: #f6f9fa; color: {ink};
    border: 1px solid {line}; border-radius: {radius}px;
    selection-background-color: {tint_selected}; selection-color: {petrol_dark}; }}
QTreeWidget:focus, QListWidget:focus, QTextBrowser:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {petrol}; }}
QTreeWidget::item, QListWidget::item {{ padding: 4px 2px; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: {tint_hover}; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background: {tint_selected}; color: {petrol_dark}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {surface_alt}; color: {muted}; padding: 5px 6px; border: none;
    border-bottom: 1px solid {line}; font-size: {small}pt; font-weight: 600; }}

/* Slim scrollbars, scoped to MAGOH */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c3d1d5; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:horizontal {{ background: #c3d1d5; border-radius: 3px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: #9fb4ba; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QProgressBar {{ background: {surface_alt}; border: 1px solid {line}; border-radius: 5px; text-align: center; min-height: 14px; }}
QProgressBar::chunk {{ background: {petrol}; border-radius: 4px; }}
QToolTip {{ background: {card}; color: {ink}; border: 1px solid {line_strong}; padding: 5px; }}
"""


def base_point_size():
    """QGIS font size chosen by the user, never below the previous 10 pt."""
    try:
        points = QApplication.font().pointSizeF()
    except Exception:
        points = 0.0
    return max(MIN_BASE_POINTS, float(points or 0.0))


def _fmt(points):
    return ("%.1f" % points).rstrip("0").rstrip(".")


def build_style(base=None):
    base = base or base_point_size()
    font = QFont(QApplication.font())
    font.setPointSizeF(base)
    font.setBold(True)
    line_h = QFontMetrics(font).height()
    values = dict(TOKENS)
    values.update(
        base=_fmt(base), small=_fmt(max(8.0, base - 1.0)), title=_fmt(base + 2.0),
        brand=_fmt(base + 3.0), hero=_fmt(base + 0.5),
        radius=RADIUS, card_radius=CARD_RADIUS,
        control_h=max(20, line_h + 4), compact_h=max(18, line_h + 1), hero_h=max(34, line_h * 2 + 8),
        group_top=line_h + 6,
        check=_icon_url("check.svg"), check_disabled=_icon_url("check_disabled.svg"),
        chevron_down=_icon_url("chevron_down.svg"), chevron_up=_icon_url("chevron_up.svg"),
        chevron_down_disabled=_icon_url("chevron_down_disabled.svg"),
        chevron_up_disabled=_icon_url("chevron_up_disabled.svg"),
    )
    return STYLE_TEMPLATE.format(**values)


def ui_icon(name):
    return QIcon(str(_UI_ICONS / f"{name}.svg"))


def artwork_pixmap(size, name="hat_m"):
    """The artist's PNG, unchanged; Qt picks the closest file and scales it."""
    icon = QIcon()
    for px in (16, 24, 32, 48, 64, 128, 256):
        path = _HERE / "icons" / f"{name}_{px}.png"
        if path.exists():
            icon.addFile(str(path), QSize(px, px))
    if icon.isNull():
        icon = QIcon(str(_HERE / "icons" / f"{name}.png"))
    return icon.pixmap(size, size)


def tidy_layouts(widget):
    """Uniform form and tab behaviour for every MAGOH surface (idempotent)."""
    try:
        for form in widget.findChildren(QFormLayout):
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setHorizontalSpacing(SPACE_M)
            form.setVerticalSpacing(SPACE_M - 2)
    except Exception:
        pass
    try:
        # Combo boxes sized on their longest value (layer or field names) would
        # widen the dock and the dialogs: keep them flexible, full text in the popup.
        for combo in widget.findChildren(QComboBox):
            if combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow:
                combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(8)
    except Exception:
        pass
    try:
        tabs = widget.findChildren(QTabWidget)
        if isinstance(widget, QTabWidget):
            tabs.append(widget)
        for tab in tabs:
            bar = tab.tabBar()
            bar.setElideMode(Qt.TextElideMode.ElideRight)
            bar.setUsesScrollButtons(True)
            bar.setExpanding(False)
            tab.setDocumentMode(True)
    except Exception:
        pass


def apply_style(widget):
    """Scope styling to the supplied MAGOH widget and its children."""
    style = build_style()
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(style)
    tidy_layouts(widget)


# --- Components -------------------------------------------------------------

def set_role(button, primary=False, compact=False, hero=False):
    """Mark a QPushButton's visual role; the stylesheet does the rest."""
    button.setProperty("primary", bool(primary))
    button.setProperty("compact", bool(compact))
    button.setProperty("hero", bool(hero))
    try:
        button.style().unpolish(button)
        button.style().polish(button)
    except Exception:
        pass
    return button


def label(text, role=None, wrap=True, parent=None):
    widget = QLabel(text, parent)
    if role:
        widget.setObjectName(role)
    widget.setWordWrap(wrap)
    return widget


def hint(text, parent=None):
    return label(text, "MagohHint", True, parent)


def card(parent=None, margins=(SPACE_M, SPACE_M, SPACE_M, SPACE_M), spacing=SPACE_S):
    """A single flat white panel. Returns (frame, vertical layout)."""
    frame = QFrame(parent)
    frame.setObjectName("MagohCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return frame, layout


def section(title, parent=None, spacing=SPACE_S):
    """Caption above a card: replaces nested group boxes. Returns (widget, card layout)."""
    holder = QWidget(parent)
    outer = QVBoxLayout(holder)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(SPACE_XS)
    caption = label(title, "MagohSectionTitle", False, holder)
    outer.addWidget(caption)
    frame, layout = card(holder, spacing=spacing)
    outer.addWidget(frame)
    holder.caption = caption
    holder.card = frame
    return holder, layout


def action_bar(parent=None, margins=(SPACE_S, SPACE_S, SPACE_S, SPACE_S)):
    """Fixed footer for actions that must stay reachable. Returns (widget, layout)."""
    bar = QWidget(parent)
    bar.setObjectName("MagohActions")
    bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QHBoxLayout(bar)
    layout.setContentsMargins(*margins)
    layout.setSpacing(SPACE_S)
    return bar, layout


class VScrollArea(QScrollArea):
    """Vertical-only scroll area whose minimum width follows its content.

    Without this, a window could be narrowed below what its content needs and
    the right edge of buttons and fields would be cut off.
    """

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        body = self.widget()
        if body is None:
            return hint
        extra = self.verticalScrollBar().sizeHint().width() + 2 * self.frameWidth()
        return QSize(max(hint.width(), body.minimumSizeHint().width() + extra), hint.height())


def scroll_page(widget=None, margins=(SPACE_M, SPACE_M, SPACE_M, SPACE_M), spacing=SPACE_M):
    """Vertical scroll area without horizontal scrolling. Returns (scroll, body, layout)."""
    scroll = VScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = widget or QWidget()
    body.setObjectName(body.objectName() or "MagohScrollBody")
    layout = body.layout()
    if layout is None:
        layout = QVBoxLayout(body)
        layout.setContentsMargins(*margins)
        layout.setSpacing(spacing)
    scroll.setWidget(body)
    return scroll, body, layout


def tool_row(title, description, button_text, callback, primary=False, tooltip=None, parent=None):
    """One tool: name, one-line purpose and its command. Returns (frame, button)."""
    frame = QFrame(parent)
    frame.setObjectName("MagohToolRow")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(SPACE_M + 2, SPACE_M, SPACE_M + 2, SPACE_M)
    layout.setSpacing(SPACE_XS)
    layout.addWidget(label(title, "MagohToolTitle", True, frame))
    if description:
        layout.addWidget(hint(description, frame))
    row = QHBoxLayout()
    row.setContentsMargins(0, SPACE_XS, 0, 0)
    button = QPushButton(button_text, frame)
    set_role(button, primary=primary)
    if tooltip:
        button.setToolTip(tooltip)
    button.clicked.connect(callback)
    button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    row.addWidget(button)
    row.addStretch(1)
    layout.addLayout(row)
    return frame, button


def icon_button(icon_name, tooltip, callback=None, parent=None, size=26):
    button = QToolButton(parent)
    button.setIcon(ui_icon(icon_name))
    button.setIconSize(QSize(16, 16))
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setFixedSize(size, size)
    button.setAutoRaise(True)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    if callback is not None:
        button.clicked.connect(callback)
    return button


def brand_header(subtitle, parent=None, logo_size=40):
    """Brand strip with the artist's hat PNG unchanged; Qt handles display scaling."""
    header = QWidget(parent)
    header.setObjectName("MagohHeader")
    header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    row = QHBoxLayout(header)
    row.setContentsMargins(SPACE_M, SPACE_S, SPACE_S, SPACE_S)
    row.setSpacing(SPACE_M - 2)
    logo = QLabel(header)
    logo.setObjectName("MagohPanelLogo")
    logo.setPixmap(QIcon(str(_HERE / "icons" / "hat_m.png")).pixmap(logo_size, logo_size))
    logo.setFixedSize(logo_size + 4, logo_size + 4)
    logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
    row.addWidget(logo)
    words = QVBoxLayout()
    words.setSpacing(0)
    words.setContentsMargins(0, 0, 0, 0)
    title = QLabel("MAGOH Spell", header)
    title.setObjectName("MagohBrand")
    detail = QLabel(subtitle, header)
    detail.setObjectName("MagohSubtitle")
    detail.setWordWrap(True)
    words.addStretch(1)
    words.addWidget(title)
    words.addWidget(detail)
    words.addStretch(1)
    row.addLayout(words, 1)
    header.brand_title = title
    header.brand_subtitle = detail
    header.logo = logo
    return header


def dock_header(dock, subtitle, float_label, close_label, dock_label=None, translate=None):
    """Compact title bar for the side panel, keeping Qt's native drag/dock behaviour.

    ``float_label`` is shown while docked and ``dock_label`` while floating.
    ``translate`` maps a stored label to the current language at toggle time.
    """
    translate = translate or (lambda text: text)
    dock_label = dock_label or float_label
    header = brand_header(subtitle, dock, logo_size=32)
    header.layout().setContentsMargins(SPACE_S + 2, SPACE_XS, SPACE_XS, SPACE_XS)
    float_button = icon_button("float", float_label, lambda: dock.setFloating(not dock.isFloating()), header)
    float_button.setObjectName("MagohFloat")
    close_button = icon_button("close", close_label, dock.hide, header)
    close_button.setObjectName("MagohClose")
    controls = QHBoxLayout()
    controls.setSpacing(2)
    controls.addWidget(float_button)
    controls.addWidget(close_button)
    header.layout().addLayout(controls)
    header.layout().setAlignment(controls, Qt.AlignmentFlag.AlignTop)

    def sync(floating):
        text = translate(dock_label if floating else float_label)
        float_button.setIcon(ui_icon("dock" if floating else "float"))
        float_button.setToolTip(text)
        float_button.setAccessibleName(text)

    try:
        dock.topLevelChanged.connect(sync)
    except Exception:
        pass
    header.sync_float_state = lambda: sync(dock.isFloating())
    sync(dock.isFloating())
    apply_style(header)
    return header


def dialog_header(title, subtitle=None, parent=None):
    """Header strip for secondary MAGOH windows (no logo: reserved to main surfaces)."""
    header = QWidget(parent)
    # Fascia verde petrolio: il "tocco di verde" comune a tutte le finestre secondarie.
    header.setObjectName("MagohDialogHeader")
    header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    column = QVBoxLayout(header)
    column.setContentsMargins(SPACE_L, SPACE_M, SPACE_L, SPACE_M)
    column.setSpacing(2)
    heading = label(title, "MagohDialogTitle", True, header)
    column.addWidget(heading)
    header.title_label = heading
    header.subtitle_label = None
    if subtitle:
        header.subtitle_label = label(subtitle, "MagohDialogSubtitle", True, header)
        column.addWidget(header.subtitle_label)
    return header


def dialog_frame(dialog, title, subtitle=None, scroll=True):
    """Give a secondary dialog the shared structure.

    Header strip, a body (scrolling vertically if needed) and a fixed footer
    whose buttons stay reachable at any window size. Returns (body_layout,
    footer_layout); the dialog's own layout is created here.
    """
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    dialog.magoh_header = dialog_header(title, subtitle, dialog)
    outer.addWidget(dialog.magoh_header)
    if scroll:
        area, body, layout = scroll_page(margins=(SPACE_L, SPACE_M + 2, SPACE_L, SPACE_M + 2))
        outer.addWidget(area, 1)
        dialog.magoh_body = body
    else:
        body = QWidget(dialog)
        body.setObjectName("MagohPage")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(SPACE_L, SPACE_M + 2, SPACE_L, SPACE_M + 2)
        layout.setSpacing(SPACE_M)
        outer.addWidget(body, 1)
        dialog.magoh_body = body
    bar, footer = action_bar(dialog, margins=(SPACE_L, SPACE_M, SPACE_L, SPACE_M))
    outer.addWidget(bar)
    dialog.magoh_footer = bar
    return layout, footer


class ElidedLabel(QLabel):
    """Single-line value that never widens its window: long paths are elided.

    ``text()`` returns the full value (so localisation passes keep it intact);
    the full value is also offered as tooltip when it does not fit.
    """

    def __init__(self, text="", parent=None, mode=Qt.TextElideMode.ElideMiddle):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setObjectName("MagohValue")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setText(text)

    def setText(self, text):
        self._full = "" if text is None else str(text)
        self._update()

    def text(self):
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update()

    def _update(self):
        width = max(10, self.width() - 2)
        shown = self.fontMetrics().elidedText(self._full, self._mode, width)
        QLabel.setText(self, shown)
        self.setToolTip(self._full if shown != self._full else "")


def key_value_grid(rows, parent=None):
    """Muted keys and elided values in two columns. Returns (widget, {key: value label})."""
    from qgis.PyQt.QtWidgets import QGridLayout
    holder = QWidget(parent)
    grid = QGridLayout(holder)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(SPACE_L)
    grid.setVerticalSpacing(SPACE_XS + 1)
    values = {}
    for index, (key, caption) in enumerate(rows):
        name = label(caption, "MagohKey", False, holder)
        name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        value = ElidedLabel("", holder)
        grid.addWidget(name, index, 0)
        grid.addWidget(value, index, 1)
        values[key] = value
    grid.setColumnStretch(1, 1)
    holder.grid = grid
    return holder, values


def segmented(labels, parent=None):
    """Two exclusive checkable buttons drawn as one control. Returns (widget, buttons)."""
    holder = QWidget(parent)
    holder.setObjectName("MagohSegmented")
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    buttons = []
    for index, text in enumerate(labels):
        button = QPushButton(text, holder)
        button.setObjectName("MagohSegLeft" if index == 0 else "MagohSegRight")
        button.setCheckable(True)
        button.setAutoDefault(False)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row.addWidget(button)
        buttons.append(button)
    return holder, buttons


class MagohMessageBox(QMessageBox):
    """QMessageBox with the MAGOH look, for MAGOH modules only.

    Drop-in replacement for the static helpers used by the plugin
    (information, warning, critical, question): same arguments, same returned
    StandardButton. The style is set on each box, never on QGIS.
    """

    @staticmethod
    def _show(icon, parent, title, text, buttons=None, default_button=None):
        Std = QMessageBox.StandardButton
        if buttons is None:
            buttons = Std.Ok
        box = QMessageBox(icon, title, text, buttons, parent)
        if default_button is not None and default_button != Std.NoButton:
            box.setDefaultButton(default_button)
        accept_roles = (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole)
        for button in box.buttons():
            if isinstance(button, QPushButton):
                set_role(button, primary=box.buttonRole(button) in accept_roles)
        apply_style(box)
        box.exec() if hasattr(box, "exec") else box.exec_()
        clicked = box.clickedButton()
        result = box.standardButton(clicked) if clicked is not None else Std.NoButton
        return result

    @staticmethod
    def information(parent, title, text, buttons=None, defaultButton=None):
        return MagohMessageBox._show(QMessageBox.Icon.Information, parent, title, text, buttons, defaultButton)

    @staticmethod
    def warning(parent, title, text, buttons=None, defaultButton=None):
        return MagohMessageBox._show(QMessageBox.Icon.Warning, parent, title, text, buttons, defaultButton)

    @staticmethod
    def critical(parent, title, text, buttons=None, defaultButton=None):
        return MagohMessageBox._show(QMessageBox.Icon.Critical, parent, title, text, buttons, defaultButton)

    @staticmethod
    def question(parent, title, text, buttons=None, defaultButton=None):
        Std = QMessageBox.StandardButton
        if buttons is None:
            buttons = Std.Yes | Std.No
        return MagohMessageBox._show(QMessageBox.Icon.Question, parent, title, text, buttons, defaultButton)


def apply_teal_style(widget):
    """Traduce le etichette note e applica il tema MAGOH a una finestra."""
    from .i18n import _translate_widget_tree
    _translate_widget_tree(widget)
    apply_style(widget)
