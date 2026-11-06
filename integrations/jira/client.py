"""The site, the credentials, and one way of asking Jira something.

Shared by the cycle plugins (``cycle/plugins/jira.py``, ``jira_transition.py``)
and the Integrations page's operations: the authentication, the two flavours'
base paths and the business of turning an HTTP failure into something worth
reading are the same question whoever is asking, and two copies would drift on
the half that says why a 401 happened.

**No dependency.** ``urllib.request`` from the standard library. Adding a client
library to the core so a step can make a few requests would be the wrong trade
in a project whose runtime dependency list is one line long.

**Cloud and Server are different products here, and the caller says which.**
Atlassian removed the old search endpoint from Cloud and returns bodies as
Atlassian Document Format; Server keeps the older endpoint and returns text.
Guessing between them from a URL would be a coin-flip, so ``flavour`` is always
said, never inferred.
"""

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

#: Which product this is talking to. They differ in the search endpoint and in
#: how a comment's body comes back, and nothing about a URL says which is which.
CLOUD, SERVER = "cloud", "server"

#: How long any single request may take. A step's own ``timeout`` governs the
#: whole thing; this is so one hung connection cannot sit there forever.
REQUEST_TIMEOUT = 30

#: Atlassian's API gateway. A connection signed in through Atlassian, and one
#: whose API token has scopes, asks Jira Cloud here rather than at its site.
#: A module constant so a test can point it at a loopback server.
GATEWAY = "https://api.atlassian.com"


def gateway(cloud_id):
    """Where one Jira Cloud site's REST API answers through the gateway."""
    return "%s/ex/jira/%s" % (GATEWAY, cloud_id)


def cloud_id(site, verify=True):
    """A Jira Cloud site's id, as the gateway names it. Asks nothing of anyone's
    account: the site says it to whoever asks (``/_edge/tenant_info``)."""
    url = site.rstrip("/") + "/_edge/tenant_info"
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/json")
    context = None if verify else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                    context=context) as answer:
            found = json.loads(answer.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        raise JiraError("%s did not say its cloud id (%s)." % (site, exc.code), exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JiraError("Cannot reach %s: %s" % (site, exc))
    value = str((found or {}).get("cloudId") or "")
    if not value:
        raise JiraError("%s did not say its cloud id." % site)
    return value


class JiraError(Exception):
    """Jira refused, or could not be reached. Carries a readable reason.

    ``status`` is the HTTP status when there was one - 0 for "could not reach
    it" - so a caller can tell a refused sign-in from a missing issue without
    reading the words.
    """

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def api(flavour):
    """The REST API version a flavour speaks: ``"3"`` on Cloud, ``"2"`` on Server."""
    return "3" if flavour == CLOUD else "2"


class Session(object):
    """The site, the credentials, and one way of asking it something."""

    def __init__(self, site, auth, email, token, verify=True, api=None,
                 find_gateway=False):
        #: The site a person opens - what an issue's link is built from.
        self.site = site
        #: Where requests go. The site itself, except for a connection signed in
        #: through Atlassian, or an API token with scopes: those go to the gateway.
        self.api = (api or site).rstrip("/")
        self._verify = verify
        #: Jira Cloud with an email and a token: the token may have scopes, which
        #: the site refuses and only the gateway takes. Nothing about a token
        #: says which kind it is, so the first refusal tries the gateway once,
        #: and a session that got in that way stays there.
        self._find_gateway = bool(find_gateway) and auth == "basic" and not api
        #: The site's cloud id once the gateway was found to be the way in.
        self.cloud_id = ""
        if auth == "bearer":
            self._header = "Bearer " + token
        else:
            pair = ("%s:%s" % (email, token)).encode("utf-8")
            self._header = "Basic " + base64.b64encode(pair).decode("ascii")

    def __call__(self, path, params=None, body=None, method=None):
        """GET, or POST when ``body`` is given. Returns the parsed JSON.

        ``method`` overrides the verb for the writes that are not a POST - a
        PUT to edit an issue, a DELETE - with or without a body.
        """
        try:
            return self._ask(path, params, body, method)
        except JiraError as exc:
            if not self._through_gateway(exc):
                raise
            return self._ask(path, params, body, method)

    def _ask(self, path, params, body, method):
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method=method or ("POST" if body is not None else "GET"))
        if body is not None:
            request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", self._header)
        request.add_header("Accept", "application/json")
        return self._json(request, url)

    def upload(self, path, filename, data, mime="application/octet-stream"):
        """POST one file as ``multipart/form-data``. Returns the parsed JSON.

        Jira's attachment endpoint takes nothing else, and refuses the request
        as a cross-site forgery unless ``X-Atlassian-Token: no-check`` says
        otherwise. Built by hand rather than with a library for the reason the
        rest of this is: one form part does not justify a dependency.
        """
        try:
            return self._upload(path, filename, data, mime)
        except JiraError as exc:
            if not self._through_gateway(exc):
                raise
            return self._upload(path, filename, data, mime)

    @property
    def switched(self):
        """Whether this session found the gateway to be the way in."""
        return bool(self.cloud_id)

    def _through_gateway(self, exc):
        """After a refusal: whether the gateway is worth one more try."""
        if exc.status != 401 or not self._find_gateway:
            return False
        self._find_gateway = False
        try:
            found = cloud_id(self.site, self._verify)
        except JiraError:
            return False
        self.cloud_id = found
        self.api = gateway(found)
        return True

    def _upload(self, path, filename, data, mime):
        boundary = "qavector-" + uuid.uuid4().hex
        safe = str(filename or "file").replace('"', "").replace("\r", "").replace("\n", "")
        payload = b"".join((
            b"--", boundary.encode("ascii"), b"\r\n",
            ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
             % safe).encode("utf-8"),
            ("Content-Type: %s\r\n\r\n" % (mime or "application/octet-stream")).encode("utf-8"),
            data, b"\r\n--", boundary.encode("ascii"), b"--\r\n"))
        url = self.api + path
        request = urllib.request.Request(url, data=payload, method="POST")
        request.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
        request.add_header("X-Atlassian-Token", "no-check")
        request.add_header("Authorization", self._header)
        request.add_header("Accept", "application/json")
        return self._json(request, url)

    def _json(self, request, url):
        context = None if self._verify else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                        context=context) as answer:
                raw = answer.read().decode("utf-8", "replace").strip()
            # A write answers 204 with nothing in it, which is a success and
            # not a malformed reply.
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise JiraError(_why(exc, url), exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise JiraError("Cannot reach %s: %s" % (self.site, exc))
        except ValueError as exc:
            raise JiraError("%s did not answer with JSON: %s" % (url, exc))

    def fetch(self, url):
        """One attachment's bytes. Separate from ``__call__``, deliberately.

        That one parses JSON and asks for it in the Accept header; this wants
        whatever the file is. They also differ in what a URL means: an
        attachment's ``content`` is absolute and may point at a media host that
        is not the site itself, so it is used as given rather than appended -
        after checking it is the site's, because following an absolute URL out
        of a payload would send this account's credentials wherever the payload
        said to.
        """
        target = url if "://" in url else self.api + url
        if self.api != self.site and same_site(target, self.site):
            # An attachment's address is on the site; signed in through
            # Atlassian, the same path is asked of the gateway.
            target = self.api + urllib.parse.urlsplit(target).path
        if not (same_site(target, self.site) or same_site(target, self.api)):
            raise JiraError("%s is not on %s; not sending credentials there."
                            % (target, self.site))
        request = urllib.request.Request(target)
        request.add_header("Authorization", self._header)
        context = None if self._verify else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                        context=context) as answer:
                return answer.read()
        except urllib.error.HTTPError as exc:
            raise JiraError(_why(exc, target), exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise JiraError("Cannot reach %s: %s" % (target, exc))


def same_site(url, site):
    """Whether ``url`` is on the same host and scheme as the configured site."""
    one, other = urllib.parse.urlsplit(url), urllib.parse.urlsplit(site)
    return (one.scheme, one.netloc) == (other.scheme, other.netloc)


def _why(exc, url):
    """An HTTP failure as something worth reading.

    Jira puts the real reason in the body - "the JQL names a field that does
    not exist", "this account cannot browse that project" - and the status line
    alone sends somebody looking in the wrong place.
    """
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(payload, dict):
            messages = payload.get("errorMessages") or []
            errors = payload.get("errors") or {}
            detail = "; ".join([str(one) for one in messages]
                               + ["%s: %s" % pair for pair in errors.items()])
    except Exception:                       # noqa: BLE001 - best effort only
        detail = ""
    if exc.code == 401:
        detail = detail or ("the credentials were refused. On Cloud this is "
                            "your email plus an API token, not your password.")
    elif exc.code == 403:
        detail = detail or "the account may not do that."
    elif exc.code in (404, 410):
        detail = detail or ("no such endpoint. Check Product: Cloud and Server "
                            "do not share one.")
    return "Jira answered %s for %s%s" % (exc.code, url,
                                          " - " + detail if detail else "")
