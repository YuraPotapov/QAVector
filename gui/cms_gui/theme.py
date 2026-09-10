"""The "Industry" design system, translated to Qt.

The look comes from the design export (``_ds/industry-*/styles.css``): a light
blueprint/wireframe surface - square corners, hairline dividers, one slate-blue
accent, condensed headings, monospace for anything the machine produced.

Everything is a token here rather than a literal in a widget, for the same
reason the CSS file is the source of truth in the design: retuning the system
means editing this file, not hunting through six pages.
"""

import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

# --- palette (from the design system's OKLCH ramps) -------------------------
BG = "#f2f2f3"
SURFACE = "#e9e9ea"
TEXT = "#1d1f20"
ACCENT = "#5980a6"

NEUTRAL = {100: "#f5f5f8", 200: "#e7e7ea", 300: "#d4d4d7", 400: "#b7b7ba",
           500: "#98989b", 600: "#7a7a7d", 700: "#5d5d60", 800: "#424244",
           900: "#2b2b2d"}
ACCENT_RAMP = {100: "#eef6ff", 200: "#d6ebff", 300: "#b5d9fd", 400: "#94bce3",
               500: "#749dc4", 600: "#597ea3", 700: "#416180", 800: "#2c455d",
               900: "#1d2d3d"}

DIVIDER = "#cfcfd0"
DIVIDER_STRONG = "#a8a8ab"

# --- title bar --------------------------------------------------------------
# The head of the window, drawn in place of the desktop's (cms_gui/titlebar.py).
# The icon's own ground rather than the page: it is the band that says which
# tool this is, so it is the same slate in both themes - the brand, not a
# surface - and set_dark_mode leaves it alone.
TITLEBAR_BG = ACCENT_RAMP[900]
TITLEBAR_TEXT = ACCENT_RAMP[100]
TITLEBAR_MUTED = ACCENT_RAMP[500]
TITLEBAR_HOVER = ACCENT_RAMP[800]
TITLEBAR_PRESSED = ACCENT_RAMP[700]
# The hairline round a frameless window, where the desktop's frame was. The
# bar's own slate, so the head and the edge read as one frame.
WINDOW_EDGE = TITLEBAR_BG

# Status colours, reused by the run tree, the log pane and the tags.
OK = ACCENT_RAMP[700]
WARN = "#a8712a"
BAD = "#a33a2e"
BAD_TINT = "#f7e7e5"

# --- log levels --------------------------------------------------------------
# A backend log is read by scanning, so severity has to be legible at a glance
# without reading the word. Its own ramp rather than OK/WARN/BAD: those are
# interface marks on a light surface, and a log pane is a wall of monospace in
# whichever theme is on - so these are tuned per theme and switched below.
#
# DEBUG recedes (it is usually most of the lines), INFO is green, WARN amber,
# ERROR red, and CRITICAL escalates the same red with weight and a wash behind
# it - a new hue would read as a different KIND of thing, when it is the same
# thing gone further.
LOG_LEVEL = {
    "DEBUG": "#8a8a8d",
    "INFO": "#2f7d43",
    "WARN": "#b06f18",
    "ERROR": "#c0392b",
    "CRITICAL": "#8e1b12",
}
LOG_CRITICAL_BG = "#f7ddda"

def set_dark_mode(enabled: bool):
    global BG, SURFACE, TEXT, ACCENT, NEUTRAL, ACCENT_RAMP, DIVIDER, DIVIDER_STRONG, OK, WARN, BAD, BAD_TINT
    global LOG_LEVEL, LOG_CRITICAL_BG
    
    if enabled:
        BG = "#1a1b1e"
        SURFACE = "#25262b"
        TEXT = "#c1c2c5"
        DIVIDER = "#373a40"
        DIVIDER_STRONG = "#5c5f66"
        
        # Invert neutral ramp for dark mode
        NEUTRAL = {
            100: "#2b2b2d", 200: "#424244", 300: "#5d5d60", 400: "#7a7a7d",
            500: "#98989b", 600: "#b7b7ba", 700: "#d4d4d7", 800: "#e7e7ea",
            900: "#f5f5f8"
        }
        
        BAD_TINT = "#4a2522" # Darker wash behind red marks

        # Lifted off the near-black background: the light-theme values are dark
        # inks meant for paper, and on #1a1b1e they read as barely-there smudges.
        LOG_LEVEL = {
            "DEBUG": "#6f7075",
            "INFO": "#5fbf7d",
            "WARN": "#e0a33f",
            "ERROR": "#ef6a5e",
            "CRITICAL": "#ff9d92",
        }
        LOG_CRITICAL_BG = "#4a2522"
    else:
        BG = "#f2f2f3"
        SURFACE = "#e9e9ea"
        TEXT = "#1d1f20"
        DIVIDER = "#cfcfd0"
        DIVIDER_STRONG = "#a8a8ab"
        
        NEUTRAL = {
            100: "#f5f5f8", 200: "#e7e7ea", 300: "#d4d4d7", 400: "#b7b7ba",
            500: "#98989b", 600: "#7a7a7d", 700: "#5d5d60", 800: "#424244",
            900: "#2b2b2d"
        }
        
        BAD_TINT = "#f7e7e5"

        LOG_LEVEL = {
            "DEBUG": "#8a8a8d",
            "INFO": "#2f7d43",
            "WARN": "#b06f18",
            "ERROR": "#c0392b",
            "CRITICAL": "#8e1b12",
        }
        LOG_CRITICAL_BG = "#f7ddda"

# --- type -------------------------------------------------------------------
# The design asks for Barlow / Barlow Condensed / JetBrains Mono. They are web
# fonts, so they may not be installed: each family is a stack, and Qt falls
# through to the first one present. Drop the .ttf files into
# cms_gui/assets/fonts/ and they are loaded ahead of anything system-wide.
FONT_BODY = ['Barlow', 'Inter', 'Noto Sans', 'DejaVu Sans', 'sans-serif']
FONT_HEADING = ['Barlow Condensed', 'Barlow', 'Oswald', 'DejaVu Sans Condensed',
                'Noto Sans', 'sans-serif']
FONT_MONO = ['JetBrains Mono', 'DejaVu Sans Mono', 'Consolas', 'Menlo', 'monospace']

ASSET_FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")


def load_fonts():
    """Register any bundled font files; returns the families actually added."""
    families = []
    try:
        names = sorted(os.listdir(ASSET_FONTS))
    except OSError:
        return families
    for name in names:
        if not name.lower().endswith((".ttf", ".otf")):
            continue
        font_id = QFontDatabase.addApplicationFont(os.path.join(ASSET_FONTS, name))
        if font_id != -1:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return families


def _stack(families):
    return ", ".join('"%s"' % f if " " in f else f for f in families)


BODY_CSS = _stack(FONT_BODY)
HEADING_CSS = _stack(FONT_HEADING)
MONO_CSS = _stack(FONT_MONO)


def mono_font(point_size=None):
    """A QFont for machine text, resolved against what is installed."""
    from PySide6.QtGui import QFont
    font = QFont()
    font.setFamilies(FONT_MONO)
    font.setStyleHint(QFont.Monospace)
    if point_size:
        font.setPointSize(point_size)
    return font


def color(name):
    """QColor for a token name, for the places that paint rather than style."""
    return QColor(name)


# --- marks ------------------------------------------------------------------
# There used to be a glyph table here: each icon was a character picked at
# runtime from a list of candidates, by asking the resolved body font whether it
# could draw it. On Windows the answer was almost always no, so the interface
# fell back to its ASCII stand-ins - a rail reading "= o > +" and a toolbar
# offering "* Settings". A program cannot fix that by choosing another
# character, so the marks are painted instead: see cms_gui/icons.py.


# --- stylesheet -------------------------------------------------------------
#: How far inside its cell a table view draws the cell's contents - the
#: `QTableView::item` padding below, named because a widget PUT in a cell has to
#: know it. The view insets the widget by this much, so a row has to be this much
#: taller than the widget's own minimum, or the widget renders at that minimum
#: anyway and hangs over the gridline it was supposed to sit inside.
CELL_INSET_V = 3
CELL_INSET_H = 4

# One sheet for the whole app. Square corners everywhere and 1px hairlines are
# the design's "wireframe object" rule (styles.css: .card,.btn,.input,.tag
# {border-radius: 0}), so they are set globally rather than per widget.
STYLESHEET = """
* {{
    font-family: {body};
    font-size: 13px;
    color: {text};
}}
QWidget {{ background: {bg}; }}
QMainWindow, QDialog {{ background: {bg}; }}
/* A label is text, not a panel. Inheriting the page background from the rule
   above made every label paint an opaque rectangle of it, which showed up as a
   band of the wrong colour wherever a label sat on a tinted surface - the
   CONFIGURE and OBSERVE headings on the sidebar. Anything that genuinely needs a
   fill (the Tag pill) sets its own. */
QLabel {{ background: transparent; }}

QToolTip {{
    background: {n800}; color: {n100};
    border: 1px solid {n900}; padding: 4px 7px;
}}

/* --- headings ------------------------------------------------------------ */
QLabel[role="h1"] {{
    font-family: {heading}; font-size: 26px; font-weight: 600;
    letter-spacing: 0.3px; padding: 0;
}}
QLabel[role="h2"] {{ font-family: {heading}; font-size: 17px; font-weight: 600; }}
QLabel[role="kicker"] {{
    font-family: {heading}; font-size: 11px; font-weight: 600;
    letter-spacing: 2px; color: {n600};
}}
QLabel[role="lede"] {{ font-size: 13px; color: {n700}; }}
QLabel[role="mono"] {{ font-family: {mono}; font-size: 12px; color: {n700}; }}
QLabel[role="muted"] {{ color: {n600}; }}
QLabel[role="hint"] {{ font-size: 11px; color: {n600}; }}
QLabel[role="summary"] {{ font-size: 12px; color: {n800}; }}
/* The line an empty zone leads with: the subject, said once, in the page's own
   heading face so it is plainly a state and not a row that lost its text. */
QLabel[role="emptytitle"] {{
    font-family: {heading}; font-size: 13px; font-weight: 600; color: {n700};
}}
QLabel[role="error"] {{ font-size: 12px; color: {bad}; }}
QLabel[role="error-bold"] {{ font-size: 11px; color: {bad}; font-weight: bold; }}
QLabel[role="preview"] {{ font-family: {mono}; font-size: 11px; color: {n600}; }}

/* --- buttons ------------------------------------------------------------- */
QPushButton {{
    font-family: {heading}; font-size: 14px; font-weight: 600;
    background: transparent; color: {text};
    border: 1px solid {divider}; border-radius: 0;
    padding: 5px 13px; min-height: 20px;
}}
QPushButton:hover {{ background: {n200}; }}
QPushButton:pressed {{ background: {n300}; }}
QPushButton:disabled {{ color: {n500}; border-color: {n300}; }}
QPushButton[variant="primary"] {{
    background: {accent}; color: {bg}; border-color: {accent};
}}
QPushButton[variant="primary"]:hover {{ background: {a600}; border-color: {a600}; }}
QPushButton[variant="primary"]:pressed {{ background: {a700}; border-color: {a700}; }}
QPushButton[variant="primary"]:disabled {{
    background: {n300}; border-color: {n300}; color: {n500};
}}
QPushButton[variant="ghost"] {{
    border-color: transparent; color: {a700};
    font-family: {body}; font-size: 12px; padding: 2px 6px;
}}
QPushButton[variant="ghost"]:hover {{ background: {a100}; }}
/* A ghost button that lives INSIDE a table row. Same shape, neutral ink: a row
   paints its selection in the accent, and accent-on-accent is the washed-out
   half-legible strip you get when the ghost variant is used in a cell. Neutral
   reads on the row's own background and on the selected one alike, and the
   accent comes back on hover, where there is a hover fill behind it. */
QPushButton[variant="cell"] {{
    border-color: transparent; color: {n700}; background: transparent;
    font-family: {body}; font-size: 12px; padding: 2px 6px;
}}
QPushButton[variant="cell"]:hover {{ background: {a100}; color: {a700}; }}
QPushButton[variant="cell"]:disabled {{ color: {n400}; }}
QPushButton[variant="nav"] {{
    text-align: left; border: none; border-left: 3px solid transparent;
    font-family: {body}; font-size: 13px; font-weight: 400;
    padding: 7px 13px; border-radius: 0;
}}
QPushButton[variant="nav"]:hover {{ background: {n200}; }}
QPushButton[variant="nav"][active="true"] {{
    background: {a200}; color: {a900}; border-left: 3px solid {accent};
}}
/* The rail is built from tool buttons - the one widget Qt lays out flush left
   when it carries both an icon and a label - so the nav rules above are
   mirrored here. The reset of the global QToolButton look below matters as much
   as the layout: without it the rail would inherit RUN's condensed mono. */
QToolButton[variant="nav"] {{
    text-align: left; border: none; border-left: 3px solid transparent;
    font-family: {body}; font-size: 13px; font-weight: 400; letter-spacing: 0;
    background: transparent; color: {text};
    padding: 7px 13px; border-radius: 0; min-height: 20px;
}}
QToolButton[variant="nav"]:hover {{ background: {n200}; }}
QToolButton[variant="nav"][active="true"] {{
    background: {a200}; color: {a900}; border-left: 3px solid {accent};
}}
/* The handle that collapses the rail. The base QToolButton rule below puts a
   hairline box around every tool button, which around a lone chevron reads as a
   control to think about rather than something to nudge. */
QToolButton[variant="railhandle"] {{
    background: transparent; border: none; border-radius: 0;
    padding: 4px 6px; min-height: 0;
}}
QToolButton[variant="railhandle"]:hover {{ background: {n200}; }}
/* RUN is a QToolButton so it can carry a menu, and Qt styles that as a separate
   class - left to itself it paints in Fusion's default while the identical
   button in the footer paints in the design. These mirror the QPushButton rules
   above; the menu-button section is the arrow half, which has to lose its own
   border or the split shows as a seam down the middle of a solid button. */
QToolButton {{
    font-family: {mono}; font-size: 11px; font-weight: 600;
    letter-spacing: 0.6px;
    background: transparent; color: {text};
    border: 1px solid {divider}; border-radius: 0;
    padding: 5px 13px; min-height: 20px;
}}
QToolButton:hover {{ background: {n200}; }}
QToolButton:pressed {{ background: {n300}; }}
QToolButton:disabled {{ color: {n500}; border-color: {n300}; }}
QToolButton[variant="primary"] {{
    background: {accent}; color: {bg}; border-color: {accent};
}}
QToolButton[variant="primary"]:hover {{ background: {a600}; border-color: {a600}; }}
QToolButton[variant="primary"]:pressed {{ background: {a700}; border-color: {a700}; }}
QToolButton[variant="primary"]:disabled {{
    background: {n300}; border-color: {n300}; color: {n500};
}}
/* The arrow half. Qt draws the arrow itself here, following the button's own
   text colour, so it sits on the accent fill correctly - putting a caret in the
   label instead leaves it left of the divider with an empty strip beside it. */
QToolButton::menu-button {{
    border: none; border-left: 1px solid {divider};
    width: 18px;
}}
/* On the accent fill the hairline has to be light, or it reads as a gap. */
QToolButton[variant="primary"]::menu-button {{
    border-left: 1px solid rgba(255,255,255,0.35);
}}
QToolButton::menu-arrow {{ width: 8px; height: 8px; }}
/* Room for the arrow half, which is drawn OVER the button rather than beside
   it. Keyed on carrying a menu, not on being the primary one: any tool button
   with a menu needs it, and without it the label sits under the arrow. */
QToolButton[hasmenu="true"] {{ padding-right: 24px; }}
/* The overflow button, whose whole label IS the menu hint. Qt would draw its
   own arrow under the glyph - a second mark saying what the first already says
   - so the indicator is taken away and the padding closed up around it. */
QToolButton[menuglyph="true"] {{ padding: 5px 9px; }}
QToolButton[menuglyph="true"]::menu-indicator {{ image: none; width: 0; height: 0; }}

/* Set on Save while the controls no longer match the saved configuration they
   came from. Last of the button rules on purpose: an attribute selector and a
   pseudo-class carry the same weight in Qt, so the later one is the one that
   paints, and "there is something unsaved here" has to win over :hover. */
QPushButton[dirty="true"] {{
    color: {bad}; border-color: {bad}; background: {bad_tint};
}}
QPushButton[dirty="true"]:hover, QPushButton[dirty="true"]:pressed {{
    color: {bg}; background: {bad}; border-color: {bad};
}}

/* --- inputs -------------------------------------------------------------- */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {surface}; color: {text};
    border: 1px solid {divider}; border-radius: 0;
    padding: 4px 8px; min-height: 20px;
    selection-background-color: {a300}; selection-color: {text};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {divider_strong}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {accent}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {n200}; color: {n500};
}}
QLineEdit[mono="true"], QComboBox[mono="true"] {{ font-family: {mono}; font-size: 12px; }}
/* The search row under a table's header: one box per column, standing in the
   band the header leaves for it. Slimmer than a form input on purpose - it is a
   fitting on the table rather than a field to fill in, and at form height it
   would stand taller than the rows it filters. The strip behind it carries the
   header's own tone so the two read as one band. */
QWidget[role="searchrow"] {{ background: {n200}; }}
QLineEdit[role="search"] {{
    background: {bg}; border: 1px solid {divider}; border-radius: 0;
    padding: 0 5px; min-height: 0; font-size: 11px;
}}
QLineEdit[role="search"]:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{
    image: none; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {n600}; width: 0; height: 0; margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: {bg}; border: 1px solid {divider}; color: {text};
    selection-background-color: {a200}; selection-color: {a900};
    outline: none;
}}
/* Tick boxes and radio dots are painted by :class:`IndicatorStyle`, not styled
   here: a stylesheet can fill an indicator but cannot draw a mark inside it, so
   a ticked box came out as a plain accent square with nothing in it. Deliberately
   no `::indicator` rule below - one would take the drawing back off the style. */
/* `spacing` is the gap between the box and its label; the padding is the gap
   between one row and the next. Stacked tick boxes with none of it - the flag
   list on the Command page, the artifacts in Reports - run together into a
   single block of text. */
QCheckBox, QRadioButton {{ spacing: 8px; padding: 3px 0; background: transparent; }}
QListView::item {{ padding: 2px 2px; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {n500}; }}

/* The two ends of the number stepper (:class:`widgets.Stepper`). Styled as
   buttons rather than as `QSpinBox::up-button`: a stylesheet lays those out
   against the frame rather than inside it, so they came out as two floating
   grey squares beside the box, and `::up-arrow` has no way to draw a triangle
   without a bitmap. The edge rules drop the border the neighbour already has,
   which is what makes [-][ 4 ][+] read as one object and not as three. */
QPushButton[variant="step"] {{
    font-family: {mono}; font-size: 15px; font-weight: 500; color: {n700};
    background: {n100}; border: 1px solid {divider}; border-radius: 0;
    padding: 0; min-width: 26px;
}}
QPushButton[variant="step"]:hover {{ background: {a200}; color: {a800}; }}
QPushButton[variant="step"]:pressed {{ background: {a300}; }}
QPushButton[variant="step"]:disabled {{
    background: {n200}; color: {n400}; border-color: {n300};
}}
QPushButton[variant="step"][edge="left"] {{ border-right: none; }}
QPushButton[variant="step"][edge="right"] {{ border-left: none; }}

/* --- tables -------------------------------------------------------------- */
QTableView, QTreeView, QListView {{
    background: {bg}; alternate-background-color: {bg};
    border: none; gridline-color: {n200};
    selection-background-color: {a200}; selection-color: {a900};
    outline: none;
}}
QTableView::item, QTreeView::item {{
    padding: {cell_v}px {cell_h}px; border: none;
}}
/* A selected row looks the same whether or not the table has the keyboard.
   Without this Qt paints the current cell in the active colour and the rest of
   its row in the inactive one, so a row selected by clicking a button in it
   came out two-tone - one bright cell and four dim ones. */
QTableView::item:selected:!active, QTreeView::item:selected:!active {{
    background: {a200}; color: {a900};
}}
/* Rows that carry their own buttons mark selection with a quiet band instead of
   the accent flood. A widget put in a cell paints its own background and cannot
   be told the row under it is selected, so the accent version came out as four
   light-blue cells and one dark one with the buttons stranded on it - and any
   ink legible on the flood is illegible off it. A band a shade off the page
   reads as selected without deciding anything about what sits on top. */
QTableView[rows="quiet"]::item:selected,
QTableView[rows="quiet"]::item:selected:!active {{
    background: {n300}; color: {text};
}}
/* Filled, not bare. A header painted in the page's own colour is not a band at
   all - it is small grey text floating above some rows - and a page carrying
   four tables then reads as one undifferentiated field with words scattered
   through it. SURFACE is the tone for exactly this: a shade off the page, darker
   in light mode and lighter in dark, so a header caps its table either way.
   The selected row above sits one further step out, so the two never read as
   each other - a selection that looked like a header would put the confusion
   back somewhere else. */
QHeaderView::section {{
    background: {n200}; color: {n700};
    font-family: {heading}; font-size: 11px; font-weight: 600;
    letter-spacing: 1px; text-transform: uppercase;
    border: none; border-bottom: 1px solid {divider};
    padding: 6px 6px;
}}
QTableCornerButton::section {{ background: {n200}; border: none; }}

/* --- containers ---------------------------------------------------------- */
QFrame[role="panel"] {{ background: transparent; border: 1px solid {divider}; }}
/* A panel for something that is always there, as against the ones a person
   makes: Connections and the block for logs belonging to no project, beside the
   projects themselves. Same frame, filled rather than clear, so the page reads
   as "these are the parts, and those are yours" before a word of it is read.
   SURFACE rather than a grey step for the reason at QHeaderView above - it is a
   shade off the page in either mode, so the distinction survives dark mode
   instead of inverting into a lighter block that reads as the highlighted one. */
QFrame[role="panel-fixed"] {{ background: {surface}; border: 1px solid {divider}; }}
/* Nothing configured yet: its own ground, so the page says "empty" rather than
   looking like it failed to draw. */
QFrame[role="emptyzone"] {{
    background: {surface}; border: 1px dashed {divider_strong};
}}
QFrame[role="sidebar"] {{ background: {n100}; border-right: 1px solid {divider}; }}
QFrame[role="bar"] {{ background: {bg}; border-bottom: 1px solid {divider}; }}
QFrame[role="footer"] {{ background: {n100}; border-top: 1px solid {divider}; }}
QFrame[role="hline"] {{ background: {divider}; max-height: 1px; border: none; }}
QFrame[role="vline"] {{ background: {divider}; max-width: 1px; border: none; }}

QScrollArea {{ border: none; background: {bg}; }}
QScrollBar:vertical {{ background: {n200}; width: 10px; margin: 0; border: none; }}
QScrollBar::handle:vertical {{ background: {n400}; min-height: 24px; border-radius: 0; }}
QScrollBar::handle:vertical:hover {{ background: {n500}; }}
QScrollBar:horizontal {{ background: {n200}; height: 10px; margin: 0; border: none; }}
QScrollBar::handle:horizontal {{ background: {n400}; min-width: 24px; border-radius: 0; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{
    background: {n300}; border: none; border-radius: 0; height: 4px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {accent}; }}

QMenuBar {{ background: {bg}; border-bottom: 1px solid {divider}; }}
QMenuBar::item {{ padding: 4px 9px; background: transparent; }}
QMenuBar::item:selected {{ background: {n200}; }}
QMenu {{ background: {bg}; border: 1px solid {divider}; padding: 3px; }}
QMenu::item {{ padding: 5px 22px 5px 12px; }}
QMenu::item:selected {{ background: {a200}; color: {a900}; }}
QMenu::separator {{ height: 1px; background: {divider}; margin: 4px 6px; }}

QSplitter::handle {{ background: {divider}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QStatusBar {{ background: {n200}; border-top: 1px solid {divider}; }}
QStatusBar::item {{ border: none; }}

/* --- title bar ----------------------------------------------------------- */
/* The window's own head, in place of the desktop's - see cms_gui/titlebar.py.
   Everything in it names its own colours rather than taking the page's: it is
   the one band that stays slate whichever theme the rest of the window is in. */
QFrame[role="titlebar"] {{ background: {tb_bg}; border: none; }}
QLabel[role="brand"] {{
    font-family: {heading}; font-size: 13px; font-weight: 600;
    letter-spacing: 1.6px; color: {tb_text};
}}
QLabel[role="brandtag"] {{ font-family: {mono}; font-size: 10px; color: {tb_muted}; }}
"""


def stylesheet():
    return STYLESHEET.format(
        bg=BG, surface=SURFACE, text=TEXT, accent=ACCENT,
        bad=BAD, bad_tint=BAD_TINT,
        divider=DIVIDER, divider_strong=DIVIDER_STRONG,
        body=BODY_CSS, heading=HEADING_CSS, mono=MONO_CSS,
        n100=NEUTRAL[100], n200=NEUTRAL[200], n300=NEUTRAL[300], n400=NEUTRAL[400],
        n500=NEUTRAL[500], n600=NEUTRAL[600], n700=NEUTRAL[700], n800=NEUTRAL[800],
        n900=NEUTRAL[900],
        a100=ACCENT_RAMP[100], a200=ACCENT_RAMP[200], a300=ACCENT_RAMP[300],
        a600=ACCENT_RAMP[600], a700=ACCENT_RAMP[700], a800=ACCENT_RAMP[800],
        a900=ACCENT_RAMP[900],
        cell_v=CELL_INSET_V, cell_h=CELL_INSET_H,
        tb_bg=TITLEBAR_BG, tb_text=TITLEBAR_TEXT, tb_muted=TITLEBAR_MUTED,
    )


# --- painted indicators ------------------------------------------------------
# The one part of the design a Qt stylesheet cannot express. `::indicator` can be
# given a size, a border and a fill, but nothing that puts a mark inside it short
# of `image: url(...)`, which would mean shipping bitmaps next to a design system
# whose whole point is that the colours live in this file. Painting the primitive
# keeps the tick sharp at any scale factor, and it reaches the check boxes in the
# list rows (Accounts, Extensions, Scenarios) with the same code as the standalone
# ones - those are drawn by the style, never by the stylesheet.

INDICATOR = 16          # the box, in px; Fusion's default is 13 and reads cramped
_CHECK = ((0.24, 0.53), (0.42, 0.71), (0.76, 0.30))     # tick path, as fractions


def _indicator_colors(state):
    """(fill, edge, mark) for a set of QStyle state flags."""
    enabled = bool(state & QStyle.State_Enabled)
    marked = bool(state & (QStyle.State_On | QStyle.State_NoChange))
    hover = bool(state & QStyle.State_MouseOver) and enabled
    if not enabled:
        return NEUTRAL[200], NEUTRAL[300], NEUTRAL[500] if marked else None
    if marked:
        accent = ACCENT_RAMP[600] if hover else ACCENT
        return accent, accent, BG
    return SURFACE, ACCENT if hover else DIVIDER_STRONG, None


def _paint_box(painter, rect, state):
    side = min(rect.width(), rect.height())
    box = QRectF(0, 0, side - 1, side - 1)
    box.moveCenter(QRectF(rect).center())
    # Snap to the half-pixel grid: a 1px border drawn on an integer coordinate is
    # spread over two rows and comes out grey.
    box.moveTopLeft(QPointF(round(box.left()) + 0.5, round(box.top()) + 0.5))
    fill, edge, mark = _indicator_colors(state)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor(edge), 1))
    painter.setBrush(QColor(fill))
    painter.drawRect(box)               # square, like every other object here
    if mark:
        pen = QPen(QColor(mark), max(1.6, side * 0.13))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        if state & QStyle.State_NoChange:
            # "some of these are ticked" - a bar, not a half-drawn tick.
            painter.drawLine(QPointF(box.left() + side * 0.26, box.center().y()),
                             QPointF(box.left() + side * 0.74, box.center().y()))
        else:
            points = [QPointF(box.left() + x * side, box.top() + y * side)
                      for x, y in _CHECK]
            painter.drawPolyline(points)
    painter.restore()


def _paint_dot(painter, rect, state):
    side = min(rect.width(), rect.height())
    circle = QRectF(0, 0, side - 1, side - 1)
    circle.moveCenter(QRectF(rect).center())
    fill, edge, mark = _indicator_colors(state)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor(edge), 1))
    painter.setBrush(QColor(fill))
    painter.drawEllipse(circle)
    if mark:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(mark))
        painter.drawEllipse(circle.center(), side * 0.17, side * 0.17)
    painter.restore()


class IndicatorStyle(QProxyStyle):
    """Fusion, with the tick boxes and radio dots painted by this file.

    Fusion is the one style that looks the same on all three platforms, which is
    what makes a single stylesheet enough to carry the design; this wraps it so
    the two primitives the sheet cannot reach are ours as well.
    """

    def drawPrimitive(self, element, option, painter, widget=None):
        if element in (QStyle.PE_IndicatorCheckBox,
                       QStyle.PE_IndicatorItemViewItemCheck):
            _paint_box(painter, option.rect, option.state)
        elif element == QStyle.PE_IndicatorRadioButton:
            _paint_dot(painter, option.rect, option.state)
        else:
            super().drawPrimitive(element, option, painter, widget)

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PM_IndicatorWidth, QStyle.PM_IndicatorHeight,
                      QStyle.PM_ExclusiveIndicatorWidth,
                      QStyle.PM_ExclusiveIndicatorHeight):
            return INDICATOR
        if metric in (QStyle.PM_CheckBoxLabelSpacing,
                      QStyle.PM_RadioButtonLabelSpacing):
            return 8
        return super().pixelMetric(metric, option, widget)


def app_style(base="Fusion"):
    """The style to hand to ``QApplication.setStyle``."""
    return IndicatorStyle(QStyleFactory.create(base) if isinstance(base, str)
                          else base)
