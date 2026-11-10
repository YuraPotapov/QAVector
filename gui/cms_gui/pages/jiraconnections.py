"""The Connections, Activity and Settings tabs of Integrations -> Jira.

A connection is a Jira site and an account, set once for the project; cycles name
it by its id. Its token is typed here once and never shown again: the dialog
sends it to the core in a file, and every answer says only whether one is set.
Nothing on these tabs asks Jira anything except Test.
"""

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                               QSizePolicy, QSpinBox, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .. import icons, theme, widgets

PRODUCTS = (("Jira Cloud", "cloud"), ("Jira Server or Data Center", "server"))
#: How the log names what was done.
OPERATIONS = {"transition": "Status", "comment": "Comment", "edit": "Edit",
              "assign": "Assignee", "create": "Create", "attach": "File"}

AUTHS = (("Sign in with Atlassian", "oauth"), ("Email and API token", "basic"),
         ("Personal access token", "bearer"))


def _product(value):
    return "Cloud" if value == "cloud" else "Server / Data Center"


def status_text(connection, kind=""):
    """What the Status column says: the last real answer, or the last Test."""
    if not connection.get("token_set"):
        return "No token"
    words = {"ok": "Connected", "signin": "Sign-in refused", "forbidden": "No access",
             "unreachable": "Unreachable", "busy": "Busy", "error": "Problem"}
    if kind:
        return words.get(kind, "Problem")
    check = connection.get("last_check") or {}
    if not check:
        return "Not checked"
    when = str(check.get("at") or "")[:10]
    if check.get("ok"):
        return "Signed in as %s · %s" % (check.get("account") or "?", when)
    return "Failed · %s" % when


def _problem():
    label = QLabel("")
    label.setProperty("role", "error")
    label.setWordWrap(True)
    label.setVisible(False)
    return label


def _table(headers):
    table = widgets.Table(0, len(headers), reclick_clears=False)
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setShowGrid(False)
    header = table.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    for column in range(len(headers)):
        header.setSectionResizeMode(column, QHeaderView.Interactive)
    return table


def _cell(text):
    item = QTableWidgetItem(str(text) if text not in (None, "") else "—")
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


# -- Connections --------------------------------------------------------------
class ConnectionsTab(QWidget):
    """The project's Jira connections: add, change, test, remove."""

    #: (document for ``--integrations save``, done(payload) or None)
    save_requested = Signal(dict, object)
    #: (connection id, done(payload) or None)
    test_requested = Signal(str, object)
    remove_requested = Signal(str, object)
    #: (request, done(payload), cancel Event) - a browser sign-in
    sign_in_requested = Signal(dict, object, object)
    #: (the window to open the OAuth app's settings over)
    app_requested = Signal(object)

    HEADERS = ("Name", "Product", "Site", "Account", "Status")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connections, self.health = [], {}
        #: Developer mode: the dialog offers the project's own OAuth app.
        self.developer = False
        self.dialog = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "A connection is a Jira site and an account, set once for this project. "
            "A cycle step names it - connection: its id - instead of carrying a "
            "site, an email and a token. The token is kept encrypted and never "
            "shown again."))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for index, width in ((0, 170), (1, 150), (3, 210), (4, 240)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._sync)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit())
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(
            self.table, "No connections in this project yet - + Connection adds one.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)
        self.add_button = QPushButton("+ Connection")
        self.add_button.clicked.connect(self.add)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(lambda: self.edit())
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip("Sign in to Jira with this connection, once")
        self.test_button.clicked.connect(self.test)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.clicked.connect(self.remove)
        self.message = QLabel("")
        self.message.setProperty("role", "muted")
        column.addWidget(widgets.row(self.add_button, self.edit_button, self.test_button,
                                     self.remove_button, self.message, None))
        self._sync()

    def set_connections(self, connections, health=None):
        self.connections = list(connections or [])
        self.health = dict(health or {})
        chosen = self.selected()
        chosen_id = chosen.get("id") if chosen else ""
        self.table.setRowCount(0)
        for one in self.connections:
            row = self.table.rowCount()
            self.table.insertRow(row)
            account = {"basic": one.get("email"), "bearer": "Access token",
                       "oauth": "Signed in with Atlassian"}.get(one.get("auth"), "")
            if one.get("gateway"):
                account = "%s · scoped token" % account
            values = (one.get("name"), _product(one.get("flavour")), one.get("site"),
                      account,
                      status_text(one, self.health.get(one.get("id"), "")))
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 0:
                    item.setData(Qt.UserRole, one.get("id"))
                    item.setToolTip("id: %s" % one.get("id"))
                self.table.setItem(row, column, item)
            if one.get("id") == chosen_id:
                self.table.selectRow(row)
        self.search.apply()
        self._sync()

    def select(self, connection_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) == connection_id:
                self.table.selectRow(row)
                return True
        return False

    def selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        wanted = item.data(Qt.UserRole) if item is not None else ""
        return next((one for one in self.connections if one.get("id") == wanted), None)

    def _sync(self):
        chosen = self.selected() is not None
        for button in (self.edit_button, self.test_button, self.remove_button):
            button.setEnabled(chosen)
        if chosen and not self.selected().get("token_set"):
            self.test_button.setEnabled(False)
            self.test_button.setToolTip("Set its token first")
        else:
            self.test_button.setToolTip("Sign in to Jira with this connection, once")

    def _say_empty(self):
        self.note.say_filtered("No connection matches the search."
                               if self.search.searching() else "")

    # -- doing ----------------------------------------------------------------
    def add(self):
        self._open(None)

    def edit(self, connection=None):
        connection = connection or self.selected()
        if connection is not None:
            self._open(connection)

    def _open(self, connection):
        dialog = ConnectionDialog(self, connection,
                                  taken=[one.get("name", "") for one in self.connections
                                         if one is not connection],
                                  developer=self.developer)
        dialog.save_requested.connect(self.save_requested)
        dialog.sign_in_requested.connect(self.sign_in_requested)
        dialog.app_requested.connect(lambda: self.app_requested.emit(dialog))
        self.dialog = dialog
        dialog.exec()
        self.dialog = None
        dialog.deleteLater()

    def test(self):
        chosen = self.selected()
        if chosen is None:
            return
        self.test_button.setEnabled(False)
        self.message.setText("Signing in…")

        def done(payload):
            self._sync()
            if payload.get("ok"):
                self.message.setText("Signed in as %s." % (
                    (payload.get("result") or {}).get("account") or "?"))
            else:
                self.message.setText(payload.get("error") or "The sign-in failed.")

        self.test_requested.emit(chosen["id"], done)

    def remove(self):
        chosen = self.selected()
        if chosen is None:
            return
        detail = ("Its token is forgotten too, and whatever names this connection "
                  "stops working until it names another. Nothing in Jira changes.")
        if not widgets.confirm(self, "Remove connection",
                               "Remove %s?" % chosen.get("name"), detail,
                               agree="Remove", kind="warn"):
            return

        def done(payload):
            self.message.setText("" if payload.get("ok")
                                 else payload.get("error") or "Not removed.")

        self.remove_requested.emit(chosen["id"], done)


class ConnectionDialog(QDialog):
    """Add a connection, or change one.

    **Sign in with Atlassian** (Jira Cloud): one click opens the browser, and the
    site comes back from the sign-in - no email, no token typed. It uses the
    project's own OAuth app, set up once (:class:`OAuthAppDialog`).

    **Email and API token** / **personal access token**: the token box is always
    empty - typing in it replaces the token, leaving it empty keeps the one set.
    """

    save_requested = Signal(dict, object)
    #: (request for ``--integrations sign-in``, done(payload), cancel Event)
    sign_in_requested = Signal(dict, object, object)
    app_requested = Signal()

    def __init__(self, parent, connection=None, taken=(), developer=False):
        super().__init__(parent)
        self.developer = bool(developer)
        self.connection = dict(connection or {})
        self._taken = {name.casefold() for name in taken if name}
        self._pending = ""
        self._signed = bool(self.connection.get("auth") == "oauth"
                            and self.connection.get("token_set"))
        self._cancel = None
        self.setWindowTitle("Edit connection" if connection else "Add a Jira connection")
        self.frame = widgets.dress(self)
        self.setMinimumWidth(480)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        self.name = QLineEdit(self.connection.get("name", ""))
        self.name.setPlaceholderText("Product team")
        column.addWidget(widgets.field(
            "Name", self.name,
            "Cycles name it by its id%s, which never changes when you rename it."
            % (" (%s)" % self.connection["id"] if self.connection.get("id") else "")))
        # The full width of the dialog, like every field in it: the cap is for
        # the main window, whose width a picker must not set.
        self.auth = QComboBox()
        for text, value in AUTHS:
            self.auth.addItem(text, value)
        self.auth.setCurrentIndex(max(0, self.auth.findData(
            self.connection.get("auth", "oauth" if not connection else "basic"))))
        self.auth.currentIndexChanged.connect(self._auth_changed)
        auth_field = widgets.field("Authentication", self.auth)
        # What signing in does, or how it went: the field's hint, drawn like
        # the one under Name rather than as a paragraph of its own.
        self.oauth_state = QLabel("")
        self.oauth_state.setProperty("role", "hint")
        self.oauth_state.setWordWrap(True)
        auth_field.layout().addWidget(self.oauth_state)
        column.addWidget(auth_field)

        # -- signed in through the browser
        self.oauth_box = QWidget()
        box = QVBoxLayout(self.oauth_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        self.sign_in = QPushButton("Sign in with Atlassian")
        self.sign_in.setProperty("variant", "primary")
        self.sign_in.clicked.connect(self._sign_in)
        self.app_button = QPushButton("Sign-in setup…")
        self.app_button.setProperty("variant", "ghost")
        self.app_button.setToolTip("Developer mode: the project's own Atlassian OAuth app, "
                                   "or another sign-in service")
        self.app_button.clicked.connect(self.app_requested)
        self.app_button.setVisible(self.developer)
        box.addWidget(widgets.row(self.sign_in, self.app_button, None))
        self.sites = QComboBox()
        self.sites.currentIndexChanged.connect(self._site_picked)
        self.sites_field = widgets.field("Site", self.sites,
                                         "The Jira site this connection works with.")
        box.addWidget(self.sites_field)
        column.addWidget(self.oauth_box)

        # -- an email and a token, or a personal access token
        self.token_box = QWidget()
        box = QVBoxLayout(self.token_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        self.product = QComboBox()
        for text, value in PRODUCTS:
            self.product.addItem(text, value)
        self.product.setCurrentIndex(max(0, self.product.findData(
            self.connection.get("flavour", "cloud"))))
        box.addWidget(widgets.field("Product", self.product))
        self.site = QLineEdit(self.connection.get("site", "")
                              if self.connection.get("auth") != "oauth" else "")
        self.site.setProperty("mono", "true")
        self.site.setPlaceholderText("https://yourcompany.atlassian.net")
        box.addWidget(widgets.field("Site", self.site))
        self.email = QLineEdit(self.connection.get("email", ""))
        self.email.setPlaceholderText("you@yourcompany.example")
        self.email_field = widgets.field("Account email", self.email,
                                         "The account the API token belongs to.")
        box.addWidget(self.email_field)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText(
            "Set - leave empty to keep it" if self.connection.get("token_set")
            and self.connection.get("auth") != "oauth" else "")
        box.addWidget(widgets.field(
            "Token", self.token,
            "An API token, classic or with scopes - one with scopes is recognised on "
            "first use. Kept encrypted in this project's secrets store and never "
            "shown again."))
        self.verify = QCheckBox("Verify the site's certificate")
        self.verify.setChecked(bool(self.connection.get("verify_tls", True)))
        self.verify.setToolTip("Off only for a server with a self-signed certificate "
                               "on a network you trust.")
        box.addWidget(self.verify)
        column.addWidget(self.token_box)

        self.problem = _problem()
        column.addWidget(self.problem)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save = QPushButton("Save" if connection else "Add")
        self.save.setProperty("variant", "primary")
        self.save.setDefault(True)
        self.save.clicked.connect(self._save)
        buttons.addWidget(self.save)
        column.addLayout(buttons)
        if self.connection.get("auth") == "oauth" and self.connection.get("site"):
            self.sites.addItem(self.connection["site"], {
                "id": self.connection.get("cloud_id", ""), "url": self.connection["site"],
                "name": ""})
        self._auth_changed()

    def oauth(self):
        return self.auth.currentData() == "oauth"

    def _auth_changed(self, *_args):
        oauth = self.oauth()
        self.oauth_box.setVisible(oauth)
        self.oauth_state.setVisible(oauth)
        self.token_box.setVisible(not oauth)
        self.email_field.setVisible(self.auth.currentData() == "basic")
        self._say_oauth()
        self._fit()

    def _fit(self):
        """As tall as the dialog's content at the width it has. adjustSize
        measures the wrapped hints at the narrow width the layout would like,
        which left space under a field at full width - and did not grow the
        dialog for a problem shown later, so the problem covered the button."""
        layout = self.layout()
        layout.activate()
        # Not shown yet, its width is Qt's default for any window, not its own.
        width = max(self.width() if self.isVisible() else 0, self.minimumWidth())
        self.resize(width, max(layout.totalHeightForWidth(width),
                               layout.totalMinimumSize().height()))

    def showEvent(self, event):
        super().showEvent(event)
        self._fit()

    def _say_oauth(self):
        waiting = self._cancel is not None
        self.sites_field.setVisible(self.sites.count() > 0 and not waiting)
        if waiting:
            self.oauth_state.setText("Waiting for you in the browser… Sign in there and "
                                     "allow access; this window carries on by itself.")
            self.sign_in.setText("Cancel sign-in")
            self.sign_in.setProperty("variant", "")
        else:
            self.sign_in.setText("Sign in again" if (self._pending or self._signed)
                                 else "Sign in with Atlassian")
            self.sign_in.setProperty("variant", "" if (self._pending or self._signed)
                                     else "primary")
            if self._pending or self._signed:
                account = getattr(self, "_account", "") or (
                    self.connection.get("last_check") or {}).get("account", "")
                self.oauth_state.setText("Signed in%s." % (" as %s" % account if account
                                                            else ""))
            else:
                self.oauth_state.setText(
                    "Jira Cloud only. Opens your browser to sign in to Atlassian - no "
                    "email or token to type.")
        self.sign_in.style().unpolish(self.sign_in)
        self.sign_in.style().polish(self.sign_in)
        self.save.setEnabled(not waiting)
        self._fit()

    # -- signing in -----------------------------------------------------------
    def _sign_in(self):
        if self._cancel is not None:
            self._cancel.set()
            return
        import threading
        self._cancel = threading.Event()
        self._say([])
        self._say_oauth()
        request = {"connection": self.connection["id"]} if (
            self.connection.get("id") and self.connection.get("auth") == "oauth") else {}

        def done(payload):
            self._cancel = None
            if payload.get("ok"):
                self._account = payload.get("account") or ""
                if payload.get("connection"):
                    # Signed in again to the site this connection already had.
                    self._signed, self._pending = True, ""
                    self.connection.update(payload["connection"])
                else:
                    self._pending = payload.get("pending") or ""
                    self._fill_sites(payload.get("sites") or [])
            elif payload.get("kind") == "app":
                self._say([payload.get("error") or ""])
                if self.developer:
                    self.app_requested.emit()
            elif payload.get("kind") != "cancelled":
                self._say([payload.get("error") or "Not signed in."])
            self._say_oauth()

        self.sign_in_requested.emit(request, done, self._cancel)

    def _fill_sites(self, sites):
        keep = (self.sites.currentData() or {}).get("id") or self.connection.get("cloud_id")
        self.sites.blockSignals(True)
        self.sites.clear()
        for one in sites:
            self.sites.addItem("%s  ·  %s" % (one.get("name") or "", one.get("url") or ""),
                               one)
        index = next((i for i, one in enumerate(sites) if one.get("id") == keep), 0)
        self.sites.setCurrentIndex(index)
        self.sites.blockSignals(False)
        self._site_picked()

    def _site_picked(self, *_args):
        site = self.sites.currentData() or {}
        if site.get("name") and not self.name.text().strip():
            self.name.setText(site["name"])

    # -- saving ---------------------------------------------------------------
    def document(self):
        entry = dict(self.connection)
        entry.pop("token_set", None)
        entry.update({"provider": "jira", "name": self.name.text().strip(),
                      "auth": self.auth.currentData()})
        if self.oauth():
            site = self.sites.currentData() or {}
            entry.update({"flavour": "cloud", "site": site.get("url", ""),
                          "cloud_id": site.get("id", ""), "email": "", "verify_tls": True})
            document = {"connection": entry}
            if self._pending:
                document["oauth_pending"] = self._pending
            return document
        entry.pop("cloud_id", None)
        entry.update({"flavour": self.product.currentData(),
                      "site": self.site.text().strip(),
                      "email": self.email.text().strip() if self.auth.currentData() == "basic"
                      else "",
                      "verify_tls": self.verify.isChecked()})
        return {"connection": entry, "token": self.token.text().strip()}

    def _save(self):
        document = self.document()
        name = document["connection"]["name"]
        if name and name.casefold() in self._taken:
            self._say(["A connection called %r already exists." % name])
            return
        if self.oauth() and not (self._pending or self._signed):
            self._say(["Sign in with Atlassian first."])
            return
        if (not self.oauth() and not document.get("token")
                and not (self.connection.get("token_set")
                         and self.connection.get("auth") == self.auth.currentData())):
            self._say(["Type the token: a connection without one cannot be used."])
            return
        self.save.setEnabled(False)
        self.save.setText("Saving…")

        def done(payload):
            self.save.setEnabled(True)
            self.save.setText("Save" if self.connection.get("id") else "Add")
            if payload.get("ok"):
                self.token.clear()
                self.accept()
            else:
                self._say(payload.get("problems") or [payload.get("error") or "Not saved."])

        self.save_requested.emit(document, done)

    def reject(self):
        if self._cancel is not None:
            self._cancel.set()
        super().reject()

    def _say(self, problems):
        self.problem.setText("\n".join(one for one in problems if one))
        self.problem.setVisible(any(problems))
        self._fit()


class OAuthAppDialog(QDialog):
    """Developer mode: how this project signs in with Atlassian.

    Ordinarily through the sign-in service the build came with, which keeps the
    app's secret off every desktop. A developer can name another service, or set
    the project's own OAuth app instead - its client ID and secret - which then
    comes first.
    """

    save_requested = Signal(dict, object)

    CONSOLE = "https://developer.atlassian.com/console/myapps/"

    def __init__(self, parent, app=None):
        super().__init__(parent)
        app = dict(app or {})
        self.setWindowTitle("Atlassian sign-in")
        self.frame = widgets.dress(self)
        self.setMinimumWidth(520)
        column = QVBoxLayout(self)
        column.setSpacing(8)
        mode = {"own": "this project's own OAuth app",
                "service": "the sign-in service at %s" % app.get("service", ""),
                }.get(app.get("mode"), "nothing - there is no service and no app")
        self.mode = QLabel("Sign in with Atlassian goes through %s." % mode)
        self.mode.setWordWrap(True)
        column.addWidget(self.mode)
        self.service = QLineEdit(app.get("service_own", ""))
        self.service.setProperty("mono", "true")
        self.service.setPlaceholderText(app.get("service_default")
                                        or "None in this build")
        column.addWidget(widgets.field(
            "Sign-in service", self.service,
            "Where signing in goes when no own app is set. Empty uses the one this "
            "build came with."))
        column.addWidget(widgets.hline())
        steps = QLabel(
            "Own OAuth app (optional) - once, at developer.atlassian.com:"
            "<ol style='margin-left:-20px'>"
            "<li>Create an OAuth 2.0 integration.</li>"
            "<li>Permissions: add the Jira API with read:jira-work, write:jira-work and "
            "read:jira-user.</li>"
            "<li>Authorization: set the callback URL below.</li>"
            "<li>Distribution: enable sharing if anyone else signs in with it.</li>"
            "<li>Settings: copy its Client ID and Secret here.</li></ol>")
        steps.setTextFormat(Qt.RichText)
        steps.setWordWrap(True)
        column.addWidget(steps)
        console = QPushButton("Open the developer console")
        icons.button(console, "external", "Open the developer console")
        console.setProperty("variant", "ghost")
        console.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.CONSOLE)))
        column.addWidget(widgets.row(console, None))
        self.client_id = QLineEdit(app.get("client_id", ""))
        self.client_id.setProperty("mono", "true")
        self.client_id.setPlaceholderText("Empty: sign in through the service")
        column.addWidget(widgets.field("Client ID", self.client_id))
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.secret.setPlaceholderText("Set - leave empty to keep it"
                                       if app.get("secret_set") else "")
        column.addWidget(widgets.field("Secret", self.secret,
                                       "Kept encrypted in this project's secrets store."))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(app.get("port") or 47321))
        self.port.setMaximumWidth(140)
        self.port.valueChanged.connect(self._port_changed)
        column.addWidget(widgets.field(
            "Callback port", self.port,
            "This machine only. Change it if another program uses it - and the callback "
            "URL at Atlassian with it."))
        self.callback = QLineEdit()
        self.callback.setReadOnly(True)
        self.callback.setProperty("mono", "true")
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.callback.text()))
        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(self.callback, 1)
        line.addWidget(copy)
        column.addWidget(widgets.field("Callback URL", holder))
        self.problem = _problem()
        column.addWidget(self.problem)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save = QPushButton("Save")
        self.save.setProperty("variant", "primary")
        self.save.setDefault(True)
        self.save.clicked.connect(self._save)
        buttons.addWidget(self.save)
        column.addLayout(buttons)
        self._port_changed()

    def _port_changed(self, *_args):
        self.callback.setText("http://localhost:%d/callback" % self.port.value())

    def document(self):
        return {"client_id": self.client_id.text().strip(),
                "secret": self.secret.text().strip(), "port": self.port.value(),
                "service": self.service.text().strip()}

    def _save(self):
        self.save.setEnabled(False)

        def done(payload):
            self.save.setEnabled(True)
            if payload.get("ok"):
                self.secret.clear()
                self.accept()
            else:
                self.problem.setText(" ".join(payload.get("problems") or [])
                                     or payload.get("error") or "Not saved.")
                self.problem.setVisible(True)

        self.save_requested.emit(self.document(), done)


# -- Activity -----------------------------------------------------------------
class ActivityTab(QWidget):
    """What was changed in Jira through QAVector, and what failed."""

    HEADERS = ("Time", "Connection", "Operation", "Issue", "Result", "Source")

    def __init__(self, parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 14, 0, 0)
        column.setSpacing(8)
        column.addWidget(widgets.lede(
            "Changes made in Jira from this page and from cycle steps that name a "
            "connection, with what failed. Reads are not listed."))
        panel = widgets.BlueprintPanel(padding=(1, 1, 1, 1))
        self.table = _table(self.HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        for index, width in ((0, 150), (1, 150), (2, 130), (3, 100), (5, 180)):
            self.table.setColumnWidth(index, width)
        self.search = widgets.SearchRow(self.table)
        self.search.changed.connect(self._say_empty)
        self.note = widgets.empty_note(
            self.table, "Nothing has been changed in Jira through QAVector yet. Status "
                        "changes, comments, edits and files will be listed here.")
        panel.layout().addWidget(self.table)
        column.addWidget(panel, 1)

    #: The tab wants its rows: the project's own log file, never Jira.
    wanted = Signal()

    def shown(self):
        self.wanted.emit()

    def set_rows(self, rows, names=None):
        """Log entries, newest first, as the table says them."""
        names = names or {}
        self.table.setRowCount(0)
        for one in rows or []:
            row = self.table.rowCount()
            self.table.insertRow(row)
            # Said by whoever made the change; this page only shows it.
            source = one.get("source") or {}
            where = source.get("label") or "This page"
            values = (str(one.get("at") or "").replace("T", " ")[:16],
                      names.get(one.get("connection")) or one.get("connection"),
                      OPERATIONS.get(one.get("op"), one.get("op")), one.get("target"),
                      ("Done" if one.get("ok") else "Failed")
                      + (" - %s" % one["summary"] if one.get("summary") else ""), where)
            for column, value in enumerate(values):
                item = _cell(value)
                if column == 4 and not one.get("ok"):
                    item.setForeground(QColor(theme.BAD))
                if column == 5 and source.get("detail"):
                    item.setToolTip(str(source["detail"]))
                self.table.setItem(row, column, item)
        self.search.apply()

    def _say_empty(self):
        self.note.say_filtered("Nothing matches the search." if self.search.searching()
                               else "")


# -- Settings -----------------------------------------------------------------
class SettingsTab(QWidget):
    """The chosen connection's own settings.

    The predefined query is shown as it comes to while it is typed: the core
    resolves it (``preview_requested``) from the environments file alone -
    Jira is not asked - so the page never has a second copy of the rules.
    """

    save_requested = Signal(dict, object)
    #: ``({"predefined_query", "variables_from"}, done(payload))``.
    preview_requested = Signal(dict, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connection = None
        #: ``() -> [(environment id, label)]``: the project's environments,
        #: given by whoever holds the project's paths.
        self.environments = lambda: []
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(350)
        self._preview_timer.timeout.connect(self._preview)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 14, 0, 0)
        outer.setSpacing(8)
        self.stack_empty = widgets.empty_zone(
            "No connection chosen",
            "Settings belong to a connection. Add one in Connections, or choose one "
            "at the top right.")
        # Its own height, at the top, as on the other tabs - not a dashed box
        # the height of the window.
        self.stack_empty.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        outer.addWidget(self.stack_empty)
        self.form = QWidget()
        column = QVBoxLayout(self.form)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        self.title = widgets.heading("", "h2")
        column.addWidget(self.title)
        self.query = QLineEdit()
        self.query.setObjectName("jiraPredefinedQuery")
        self.query.setProperty("mono", "true")
        self.query.setPlaceholderText("project = {{jira_project}}")
        self.query.setMaximumWidth(520)
        self.query.textEdited.connect(self._query_changed)
        column.addWidget(widgets.field(
            "Predefined query", self.query,
            "JQL that every query of this connection is ANDed with - the list, a typed "
            "query, Ctrl+K, cycle steps. {{name}} is a variable of the environment "
            "below, put in as a quoted value. Empty: the whole site."))
        self.variables_from = widgets.capped(QComboBox(), 320)
        self.variables_from.setObjectName("jiraVariablesFrom")
        self.variables_from.setAccessibleName("Variables from")
        self.variables_from.activated.connect(self._query_changed)
        column.addWidget(widgets.field(
            "Variables from", self.variables_from,
            "The environment whose variables the query uses. Add a variable such as "
            "jira_project to it on the Environments page."))
        self.comes_to = QLabel("")
        self.comes_to.setObjectName("jiraScopePreview")
        self.comes_to.setProperty("role", "mono")
        self.comes_to.setWordWrap(True)
        self.comes_to.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.comes_to.setVisible(False)
        column.addWidget(self.comes_to)
        self.scope_problem = _problem()
        column.addWidget(self.scope_problem)
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 240)
        self.minutes.setSpecialValueText("Never")
        self.minutes.setSuffix(" min")
        self.minutes.setMaximumWidth(140)
        self.minutes.valueChanged.connect(self._changed)
        column.addWidget(widgets.field(
            "Refresh the list every", self.minutes,
            "Never: the list is read only when you ask. Otherwise it is read again "
            "while this page is open - only the rows on screen."))
        self.problem = _problem()
        column.addWidget(self.problem)
        self.save = QPushButton("Save settings")
        self.save.setProperty("variant", "primary")
        self.save.clicked.connect(self._save)
        self.saved = QLabel("")
        self.saved.setProperty("role", "muted")
        column.addWidget(widgets.row(self.save, self.saved, None))
        column.addStretch(1)
        outer.addWidget(self.form)
        outer.addStretch(1)
        self.set_connection(None)

    def set_connection(self, connection):
        self.connection = dict(connection) if connection else None
        self.stack_empty.setVisible(connection is None)
        self.form.setVisible(connection is not None)
        self.save.setEnabled(False)
        self.saved.setText("")
        self.problem.setVisible(False)
        if connection is None:
            return
        settings = connection.get("settings") or {}
        for widget in (self.minutes, self.query, self.variables_from):
            widget.blockSignals(True)
        self.title.setText(connection.get("name") or connection.get("id"))
        self.minutes.setValue(int(settings.get("refresh_minutes") or 0))
        self.query.setText(settings.get("predefined_query") or "")
        self._fill_environments(settings.get("variables_from") or "")
        for widget in (self.minutes, self.query, self.variables_from):
            widget.blockSignals(False)
        self._preview_timer.start()

    def _fill_environments(self, chosen):
        """The project's environments, read again - one may have been added
        since - with the chosen one kept even when it is gone, so saving
        something else does not quietly forget it."""
        self.variables_from.clear()
        self.variables_from.addItem("None", "")
        try:
            found = list(self.environments() or [])
        except Exception:                   # noqa: BLE001 - offered as none
            found = []
        for env_id, label in found:
            self.variables_from.addItem(label, env_id)
        if chosen and self.variables_from.findData(chosen) < 0:
            self.variables_from.addItem("%s (not in this project)" % chosen, chosen)
        self.variables_from.setCurrentIndex(max(0, self.variables_from.findData(chosen)))

    def scope_settings(self):
        return {"predefined_query": self.query.text().strip(),
                "variables_from": self.variables_from.currentData() or ""}

    def _query_changed(self, *_args):
        self._changed()
        self._preview_timer.start()

    def _preview(self):
        """What the query comes to, said under it - by the core, from the
        environments file only."""
        if self.connection is None:
            return
        wanted = self.scope_settings()
        if not wanted["predefined_query"]:
            self._show_preview({"ok": True, "scope": "", "problems": []}, wanted)
            return
        self.preview_requested.emit(
            wanted, lambda payload, wanted=wanted: self._show_preview(payload, wanted))

    def _show_preview(self, payload, wanted):
        if wanted != self.scope_settings():
            return                          # typed on since: a newer one is coming
        problems = list(payload.get("problems") or [])
        if not payload.get("ok"):
            problems = problems or [payload.get("error") or "Could not be checked."]
        scope = payload.get("scope") or ""
        self.comes_to.setText("Comes to:  %s" % scope if scope else "")
        self.comes_to.setVisible(bool(scope) and not problems)
        self.scope_problem.setText(" ".join(problems))
        self.scope_problem.setVisible(bool(problems))

    def _changed(self, *_args):
        self.save.setEnabled(self.connection is not None)
        self.saved.setText("")

    def _save(self):
        if self.connection is None:
            return
        entry = dict(self.connection)
        entry.pop("token_set", None)
        settings = dict(entry.get("settings") or {})
        settings.pop("default_project", None)   # gone: the predefined query says it
        entry["settings"] = dict(settings,
                                 refresh_minutes=self.minutes.value(),
                                 **self.scope_settings())
        self.save.setEnabled(False)

        def done(payload):
            if payload.get("ok"):
                self.saved.setText("Saved.")
                self.problem.setVisible(False)
            else:
                self.save.setEnabled(True)
                self.problem.setText(" ".join(payload.get("problems") or [])
                                     or payload.get("error") or "Not saved.")
                self.problem.setVisible(True)

        self.save_requested.emit({"connection": entry}, done)
