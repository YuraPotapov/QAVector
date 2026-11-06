"""Signing in to Jira Cloud through the browser: Atlassian OAuth 2.0 (3LO).

**Your own OAuth app.** Atlassian's authorization code flow needs the app's
client secret to turn the code into tokens, and has no official way round it
for a program installed on a desktop. A secret shipped inside a public
repository is no secret, so QAVector carries none: the person registers an app
once at developer.atlassian.com, with the callback ``http://localhost:<port>/
callback``, and gives its client id and secret to the project. The id and port
are in ``integrations.json``; the secret is in the project's secrets store,
beside the connections' tokens.

**Then one click.** :func:`sign_in` listens on that port on this machine only,
opens the browser at Atlassian's consent page, takes the code it is sent back
with, and exchanges it. What comes back is held in the secrets store under a
pending id until the person picks the site it is for; nothing of it is printed.

**Tokens refresh themselves.** An access token lives about an hour and a run can
last longer, so each use asks :func:`fresh_token`, which refreshes shortly
before expiry. Atlassian rotates refresh tokens - every refresh returns a new
one and disables the old - so the read, the refresh and the write happen under
a lock file, and a second process that waited for it reads the new token rather
than spending the old one again.
"""

import json
import os
import secrets as random_secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from cycle import secrets as secret_store
from integrations.jira import client

#: Where Atlassian authorizes. A module constant so a test can point it at a
#: loopback server; its APIs answer at ``client.GATEWAY``.
AUTH_BASE = "https://auth.atlassian.com"

#: What the app asks for: read and change issues, read users (who is signed in,
#: assignees), and a refresh token so a person signs in once rather than hourly.
SCOPES = ("read:jira-work", "write:jira-work", "read:jira-user", "offline_access")

#: The app this module signs in with, by the name its settings are kept under.
APP = "atlassian"
DEFAULT_PORT = 47321

#: How long a sign-in waits for the browser to come back.
SIGN_IN_SECONDS = 300

#: Refresh this long before the access token runs out, so a request made just
#: before expiry does not land just after it.
EARLY = 120

#: A pending sign-in nobody picked a site for is forgotten after this long.
PENDING_SECONDS = 3600

REQUEST_TIMEOUT = 30


class OAuthError(Exception):
    """Signing in, or refreshing, failed. ``kind`` is what a page acts on:
    ``app`` (the OAuth app is not set up), ``signin`` (sign in again),
    ``cancelled``, ``busy`` (the port is taken) or ``error``."""

    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


def _store():
    from integrations import store
    return store


# -- the app ------------------------------------------------------------------
def callback_url(port):
    return "http://localhost:%d/callback" % int(port)


def app(path=None, secrets_path=None):
    """The OAuth app's settings as a page shows them: never the secret."""
    found = dict(_store().apps(path).get(APP) or {})
    port = int(found.get("port") or DEFAULT_PORT)
    secret_set = bool(_secret(secrets_path))
    own = bool(found.get("client_id")) and secret_set
    return {"client_id": str(found.get("client_id") or ""), "port": port,
            "callback": callback_url(port), "secret_set": secret_set,
            "scopes": list(SCOPES), "service": service(path),
            # The project's own choice of service, and what it would be without it.
            "service_own": str(found.get("service") or ""),
            "service_default": (os.environ.get("QAVECTOR_SIGNIN_SERVICE", "")
                                or _built_in()).strip().rstrip("/"),
            # How "Sign in with Atlassian" would go now: the project's own app,
            # the sign-in service, or not at all.
            "mode": "own" if own else "service" if service(path) else "none"}


def save_app(client_id, secret, port, path=None, secrets_path=None, service=None):
    """Set the project's own app, and/or its sign-in service. Returns problems.

    A developer's settings. An empty ``secret`` keeps the one set; no client ID
    at all means no own app, and sign-in goes through the service. ``service``
    None leaves the service as it was, "" goes back to the build's.
    """
    client_id = str(client_id or "").strip()
    found = []
    try:
        port = int(port or DEFAULT_PORT)
        if not 1024 <= port <= 65535:
            raise ValueError()
    except (TypeError, ValueError):
        found.append("The callback port must be a number from 1024 to 65535.")
    if client_id and not secret and not _secret(secrets_path):
        found.append("Secret is required with a client ID.")
    kept = dict(_store().apps(path).get(APP) or {})
    if service is not None:
        service = str(service or "").strip().rstrip("/")
        if service and not service.startswith(("https://", "http://127.0.0.1",
                                               "http://localhost")):
            found.append("The sign-in service must be an https:// address.")
        kept["service"] = service
    if found:
        return found
    kept.update({"client_id": client_id, "port": port})
    if not kept.get("service"):
        kept.pop("service", None)
    _store().save_app(APP, kept, path)
    if secret:
        secret_store.set_secret(_store().CREDENTIALS, "app:" + APP, str(secret).strip(),
                                secrets_path)
    elif not client_id:
        secret_store.set_secret(_store().CREDENTIALS, "app:" + APP, "", secrets_path)
    return []


def _secret(secrets_path):
    return secret_store.get(_store().CREDENTIALS, "app:" + APP, secrets_path)


def service(path=None):
    """The sign-in service in force: the project's own setting (a developer's),
    the environment, or the one this build came with. "" when there is none.

    The address is never in the public code: a build gets it from
    ``signin-service.json`` beside the program, written when it was packaged.
    """
    configured = str((_store().apps(path).get(APP) or {}).get("service") or "")
    return (configured or os.environ.get("QAVECTOR_SIGNIN_SERVICE", "")
            or _built_in()).strip().rstrip("/")


def _built_in():
    import runtime_paths
    try:
        with open(os.path.join(runtime_paths.app_root(), "signin-service.json"),
                  encoding="utf-8") as handle:
            return str(json.load(handle).get("url") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _own_app(path, secrets_path):
    """``(settings, secret)`` of the project's own app, or None without one."""
    settings = app(path, secrets_path)
    secret = _secret(secrets_path)
    if settings["client_id"] and secret:
        return settings, secret
    return None


def _configured(path, secrets_path):
    settings = app(path, secrets_path)
    secret = _secret(secrets_path)
    if not settings["client_id"] or not secret:
        raise OAuthError("Set up the OAuth app first: its client ID and secret, from "
                         "developer.atlassian.com.", "app")
    return settings, secret


# -- signing in ---------------------------------------------------------------
def authorize_url(client_id, port, state):
    return "%s/authorize?%s" % (AUTH_BASE, urllib.parse.urlencode({
        "audience": "api.atlassian.com", "client_id": client_id,
        "scope": " ".join(SCOPES), "redirect_uri": callback_url(port),
        "state": state, "response_type": "code", "prompt": "consent"}))


def sign_in(path=None, secrets_path=None, open_browser=None, seconds=SIGN_IN_SECONDS,
            cancel=None, say=None):
    """Sign in through the browser. Returns ``{account, sites, pending}``.

    With the project's own OAuth app when it has one - a developer's choice -
    and otherwise through the sign-in service this build came with, which keeps
    the app's secret off every desktop. ``sites`` are the Jira sites the account
    let the app reach, as ``[{id, name, url}]``; ``pending`` names the tokens,
    held in the secrets store until :func:`adopt` gives them to a connection.
    Raises :class:`OAuthError`.
    """
    own = _own_app(path, secrets_path)
    if own is not None:
        tokens = _sign_in_own(own, open_browser, seconds, cancel, say)
    else:
        url = service(path)
        if not url:
            raise OAuthError("This QAVector has no Atlassian sign-in service. Use an API "
                             "token, or ask whoever builds QAVector for your team.", "app")
        tokens = _sign_in_service(url, open_browser, seconds, cancel, say)
    sites = [{"id": str(one.get("id") or ""), "name": str(one.get("name") or ""),
              "url": str(one.get("url") or "").rstrip("/")}
             for one in _api(tokens["access"], "/oauth/token/accessible-resources")
             if isinstance(one, dict) and one.get("id")]
    if not sites:
        raise OAuthError("The account let the app reach no Jira site.", "signin")
    account = ""
    try:
        me = _api(tokens["access"], "/ex/jira/%s/rest/api/3/myself" % sites[0]["id"])
        account = str(me.get("displayName") or "")
    except OAuthError:
        pass
    pending = "pending:" + uuid.uuid4().hex
    _write(pending, dict(tokens, at=time.time()), secrets_path)
    _forget_old_pending(secrets_path)
    return {"account": account, "sites": sites, "pending": pending}


def _sign_in_own(own, open_browser, seconds, cancel, say):
    """The project's own app: Atlassian comes back to this machine with a code,
    exchanged here with the app's secret."""
    settings, secret = own
    state = random_secrets.token_urlsafe(24)
    answer = _listen(settings["port"], state,
                     lambda port: authorize_url(settings["client_id"], port, state),
                     open_browser, seconds, cancel, say)
    if not answer.get("code"):
        raise OAuthError("Atlassian did not sign you in: %s"
                         % (answer.get("error") or "no code came back"), "signin")
    return _tokens({"grant_type": "authorization_code", "client_id": settings["client_id"],
                    "client_secret": secret, "code": answer["code"],
                    "redirect_uri": callback_url(settings["port"])})


def _sign_in_service(url, open_browser, seconds, cancel, say):
    """The sign-in service: it holds the app's secret, and hands the tokens back
    sealed to a key made for this sign-in alone (see signin-service/)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    state = random_secrets.token_urlsafe(24)
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint)
    answer = _listen(0, state, lambda port: "%s/atlassian/start?%s" % (
        url, urllib.parse.urlencode({"port": port, "state": state,
                                     "key": _b64url(public)})),
                     open_browser, seconds, cancel, say)
    if not answer.get("sealed"):
        raise OAuthError("Atlassian did not sign you in: %s"
                         % (answer.get("error") or "nothing came back"), "signin")
    try:
        payload = json.loads(unseal(private, state, answer["sealed"]))
    except Exception:                   # noqa: BLE001 - any failure is the same answer
        raise OAuthError("The sign-in service's answer could not be opened. Sign in "
                         "again.", "signin")
    if not payload.get("access_token"):
        raise OAuthError("The sign-in service sent no access token.", "signin")
    return {"access": payload["access_token"], "refresh": payload.get("refresh_token") or "",
            "expires": time.time() + int(payload.get("expires_in") or 3600), "via": url}


def unseal(private, state, sealed):
    """What the service sealed to ``private``'s public key: its key (65 bytes),
    the IV (12) and AES-256-GCM ciphertext, keyed by ECDH and HKDF-SHA256 and
    bound to this sign-in's ``state``."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw = _unb64url(sealed)
    theirs = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw[:65])
    shared = private.exchange(ec.ECDH(), theirs)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"\0" * 32,
               info=b"qavector-signin-v1").derive(shared)
    return AESGCM(key).decrypt(raw[65:77], raw[77:], state.encode("utf-8")).decode("utf-8")


def _b64url(raw):
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text):
    import base64
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _listen(port, state, url_for, open_browser, seconds, cancel, say):
    """Wait on 127.0.0.1 for the browser to come back with ``state``; returns
    what it brought (``code``, ``sealed`` or ``error``). Port 0 is any free
    port - the service sends the browser wherever it is told to."""
    answer = {}
    done = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):               # noqa: N802 - the base class's spelling
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parts.query)
            if (query.get("state") or [""])[0] != state:
                # Not ours: another tab, an old link. Ignored, and the wait
                # goes on for the one this sign-in started.
                self._page(400, "This sign-in link is not the one QAVector is "
                                "waiting for. Start again from QAVector.")
                return
            for name in ("code", "sealed"):
                answer[name] = (query.get(name) or [""])[0]
            answer["error"] = (query.get("error_description") or query.get("error")
                               or [""])[0]
            self._page(200, "Signed in. You can close this tab and go back to QAVector."
                       if answer["code"] or answer["sealed"]
                       else "Not signed in: %s" % answer["error"])
            done.set()

        def _page(self, status, text):
            body = ("<!doctype html><meta charset=utf-8><title>QAVector</title>"
                    "<body style='font:16px sans-serif;margin:3em'>%s</body>"
                    % text.replace("<", "&lt;")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = HTTPServer(("127.0.0.1", port), Callback)
    except OSError as exc:
        raise OAuthError("Port %d is in use on this machine (%s). Choose another in the "
                         "OAuth app settings, and the same one in Atlassian's "
                         "callback URL." % (port, exc), "busy")
    server.timeout = 0.25
    url = url_for(server.server_port)
    try:
        if say is not None:
            say(url)
        (open_browser or webbrowser.open)(url)
        deadline = time.monotonic() + seconds
        while not done.is_set():
            if cancel is not None and cancel.is_set():
                raise OAuthError("Sign-in cancelled.", "cancelled")
            if time.monotonic() > deadline:
                raise OAuthError("Nobody came back from the browser in %d minutes."
                                 % (seconds // 60), "cancelled")
            server.handle_request()
    finally:
        server.server_close()
    return answer


def adopt(pending, connection_id, secrets_path=None):
    """Give a pending sign-in's tokens to a connection. Returns whether there were any."""
    store = _store()
    if not str(pending).startswith("pending:"):
        return False
    blob = secret_store.get(store.CREDENTIALS, pending, secrets_path)
    if not blob:
        return False
    secret_store.set_secret(store.CREDENTIALS, connection_id, blob, secrets_path)
    secret_store.set_secret(store.CREDENTIALS, pending, "", secrets_path)
    return True


def _forget_old_pending(secrets_path):
    store = _store()
    try:
        held = secret_store.load(secrets_path).get(store.CREDENTIALS, {})
    except secret_store.SecretsError:
        return
    for name, blob in held.items():
        if name.startswith("pending:"):
            try:
                at = float(json.loads(blob).get("at") or 0)
            except (TypeError, ValueError):
                at = 0
            if time.time() - at > PENDING_SECONDS:
                secret_store.set_secret(store.CREDENTIALS, name, "", secrets_path)


# -- using it -----------------------------------------------------------------
def api_base(cloud_id):
    """Where a Jira Cloud site's REST API answers for an OAuth token."""
    return client.gateway(cloud_id)


def fresh_token(connection_id, path=None, secrets_path=None, now=None):
    """An access token for the connection that is good for a while yet.

    Refreshes under a lock when it is about to run out, and writes the new
    refresh token back before anything else can use the old one. Raises
    :class:`OAuthError` - ``signin`` when Atlassian says the sign-in is gone.
    """
    blob = _read(connection_id, secrets_path)
    if not blob:
        raise OAuthError("Not signed in. Sign in with Atlassian in Connections.", "signin")
    clock = now if now is not None else time.time()
    if float(blob.get("expires") or 0) - EARLY > clock:
        return blob["access"]
    with _Lock(secret_store.resolve_path(secrets_path) + ".oauth.lock"):
        # Somebody may have refreshed while this waited: theirs is the token now.
        blob = _read(connection_id, secrets_path) or blob
        if float(blob.get("expires") or 0) - EARLY > time.time():
            return blob["access"]
        if not blob.get("refresh"):
            raise OAuthError("The sign-in has expired. Sign in with Atlassian again.",
                             "signin")
        if blob.get("via"):
            # Signed in through the service: only it has the secret to refresh with.
            fresh = _refresh_via(blob["via"], blob["refresh"])
        else:
            settings, secret = _configured(path, secrets_path)
            fresh = _tokens({"grant_type": "refresh_token",
                             "client_id": settings["client_id"], "client_secret": secret,
                             "refresh_token": blob["refresh"]})
        if not fresh.get("refresh"):
            fresh["refresh"] = blob["refresh"]
        _write(connection_id, fresh, secrets_path)
        return fresh["access"]


def _read(name, secrets_path):
    raw = secret_store.get(_store().CREDENTIALS, name, secrets_path)
    try:
        blob = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return blob if isinstance(blob, dict) and blob.get("access") else {}


def _write(name, blob, secrets_path):
    secret_store.set_secret(_store().CREDENTIALS, name, json.dumps(blob), secrets_path)


def _tokens(body):
    """POST to the token endpoint; ``{access, refresh, expires}``."""
    request = urllib.request.Request(
        AUTH_BASE + "/oauth/token", data=json.dumps(body).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as answer:
            payload = json.loads(answer.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8", "replace")).get(
                "error_description") or "")
        except Exception:                   # noqa: BLE001 - best effort only
            detail = ""
        if exc.code in (400, 401, 403):
            raise OAuthError("Atlassian refused the sign-in%s. Sign in again; if it keeps "
                             "happening, check the app's client ID and secret."
                             % (": " + detail if detail else ""), "signin")
        raise OAuthError("Atlassian answered %s%s." % (exc.code, ": " + detail if detail
                                                       else ""))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError("Cannot reach Atlassian: %s" % exc)
    if not payload.get("access_token"):
        raise OAuthError("Atlassian sent no access token.")
    return {"access": payload["access_token"], "refresh": payload.get("refresh_token") or "",
            "expires": time.time() + int(payload.get("expires_in") or 3600)}


def _refresh_via(url, refresh):
    """A refresh through the sign-in service; ``{access, refresh, expires, via}``."""
    request = urllib.request.Request(
        url + "/atlassian/refresh", data=json.dumps({"refresh_token": refresh}).encode("utf-8"),
        method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as answer:
            payload = json.loads(answer.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            raise OAuthError("Atlassian refused to renew the sign-in. Sign in again.",
                             "signin")
        raise OAuthError("The sign-in service answered %s." % exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError("Cannot reach the sign-in service: %s" % exc)
    if not payload.get("access_token"):
        raise OAuthError("The sign-in service sent no access token.")
    return {"access": payload["access_token"], "refresh": payload.get("refresh_token") or "",
            "expires": time.time() + int(payload.get("expires_in") or 3600), "via": url}


def _api(access, path):
    request = urllib.request.Request(client.GATEWAY + path)
    request.add_header("Authorization", "Bearer " + access)
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as answer:
            return json.loads(answer.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as exc:
        raise OAuthError("Atlassian answered %s for %s." % (exc.code, path), "signin"
                         if exc.code in (401, 403) else "error")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OAuthError("Cannot reach Atlassian: %s" % exc)


class _Lock(object):
    """A lock file: whoever creates it holds it. Portable, and enough for the
    few processes of one person that might refresh at the same moment."""

    def __init__(self, path, seconds=30):
        self.path, self.seconds = path, seconds

    def __enter__(self):
        deadline = time.monotonic() + self.seconds
        while True:
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                return self
            except FileExistsError:
                try:
                    stale = time.time() - os.path.getmtime(self.path) > self.seconds
                except OSError:
                    stale = False
                if stale:
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise OAuthError("Another QAVector process is refreshing the "
                                     "sign-in; try again.", "busy")
                time.sleep(0.1)

    def __exit__(self, *_exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass
