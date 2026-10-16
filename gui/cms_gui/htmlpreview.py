"""Local HTML artifacts rendered with CSS, without running report scripts."""

import os

from PySide6.QtCore import QMarginsF, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPageLayout, QPageSize
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineSettings,
                                    QWebEngineUrlRequestInterceptor)
from PySide6.QtWebEngineWidgets import QWebEngineView


def _inside(path, root):
    if not path or not root:
        return False
    try:
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except ValueError:
        return False


class _LocalRequests(QWebEngineUrlRequestInterceptor):
    """Styles and images can come from this run, never from the network.

    ``roots`` is every folder a page may read from: the run on show, or - for
    a book printed to PDF - the folder it was put together in and the guides'
    folders its pictures are in.
    """

    roots = ()

    def allows(self, path):
        return any(_inside(path, root) for root in self.roots)

    def interceptRequest(self, info):
        url = info.requestUrl()
        allowed = (url.scheme() in ("data", "about") or
                   (url.isLocalFile() and self.allows(url.toLocalFile())))
        info.block(not allowed)


class _ReportPage(QWebEnginePage):
    local_file_requested = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.document_url = QUrl()
        self.requests = _LocalRequests(self)
        self.setUrlRequestInterceptor(self.requests)
        settings = self.settings()
        for attribute in (QWebEngineSettings.JavascriptEnabled,
                          QWebEngineSettings.LocalContentCanAccessRemoteUrls,
                          QWebEngineSettings.LocalStorageEnabled,
                          QWebEngineSettings.PluginsEnabled,
                          QWebEngineSettings.HyperlinkAuditingEnabled):
            settings.setAttribute(attribute, False)
        # Relative stylesheets/images work; the interceptor confines them to
        # the selected run. Links to other artifacts go back through the tree.
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        self.newWindowRequested.connect(self._new_window)

    def acceptNavigationRequest(self, url, kind, main_frame):
        if not main_frame:
            return False
        document = QUrl(url)
        document.setFragment(None)
        if document == self.document_url:
            return True
        if kind == QWebEnginePage.NavigationTypeLinkClicked:
            self._follow(url)
        return False

    def _new_window(self, request):
        if request.isUserInitiated():
            self._follow(request.requestedUrl())

    def _follow(self, url):
        if url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)
        elif url.isLocalFile() and self.requests.allows(url.toLocalFile()):
            self.local_file_requested.emit(os.path.abspath(url.toLocalFile()))


#: What a page is printed on: A4, with the margins the run's guide is laid out
#: for (engine.livedoc's @page rule), in millimetres.
PDF_MARGINS = QMarginsF(14, 14, 14, 16)


def page_layout():
    """The layout a guide is printed with - see :data:`PDF_MARGINS`."""
    return QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                       PDF_MARGINS, QPageLayout.Millimeter)


class HtmlPreview(QWebEngineView):
    local_file_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        page = _ReportPage(self)
        self.setPage(page)
        page.local_file_requested.connect(self.local_file_requested)

    def load_file(self, path, root, fragment=""):
        """Show ``path``; with ``fragment``, scrolled to that anchor.

        A fragment alone is a jump inside the page already on show - the same
        document, not read again - so a fragment comes with the file's time of
        change in the query too: a new address, which is read from disk anew.
        """
        self.stop()
        page = self.page()
        page.requests.roots = (os.path.realpath(root),)
        url = QUrl.fromLocalFile(os.path.abspath(path))
        if fragment:
            try:
                url.setQuery("v=%d" % os.stat(path).st_mtime_ns)
            except OSError:
                pass
        page.document_url = QUrl(url)
        if fragment:
            url.setFragment(fragment)
        # Loading from disk avoids setHtml's data-URL size limit, and preserves
        # the base URL for relative links, images and stylesheets.
        self.load(url)
