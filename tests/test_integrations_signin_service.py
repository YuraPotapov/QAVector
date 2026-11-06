"""Sign in with Atlassian through the sign-in service, end to end.

The service is the real Worker (``signin-service/src/worker.js``) under Node, in
front of a loopback Atlassian; the desktop side is QAVector's own client. A
"browser" thread follows the redirects. What matters: the app's secret is used
by the service and never by the desktop, the tokens reach the desktop sealed -
no address the browser visits carries one - and a refresh goes through the
service too. Skipped where Node is not installed.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import secrets as secret_store                              # noqa: E402
from integrations import oauth, store                                   # noqa: E402
from integrations.jira import client                                    # noqa: E402
from fake_jira import FakeJira                                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLOUD = "11111111-2222-3333-4444-555555555555"
SECRET = "service-secret-not-a-real-one"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="the sign-in service runs under Node")


@pytest.fixture
def atlassian(monkeypatch):
    tokens = iter(range(1, 100))

    def token(request):
        n = next(tokens)
        return {"access_token": "access-%d" % n, "refresh_token": "refresh-%d" % n,
                "expires_in": 3600, "scope": "read:jira-work"}

    server = FakeJira({
        "POST /oauth/token": token,
        "/oauth/token/accessible-resources": [{"id": CLOUD, "name": "yourcompany",
                                               "url": "https://yourcompany.atlassian.net"}],
        "/ex/jira/%s/rest/api/3/myself" % CLOUD: {"displayName": "Alex"},
    })
    monkeypatch.setattr(client, "GATEWAY", server.site)
    yield server
    server.close()


@pytest.fixture
def service(atlassian):
    env = dict(os.environ, SIGNIN_ENV=json.dumps({
        "ATLASSIAN_CLIENT_ID": "client-1", "ATLASSIAN_CLIENT_SECRET": SECRET,
        "RELAY_KEY": "relay-key-not-a-real-one", "ATLASSIAN_AUTH": atlassian.site}))
    process = subprocess.Popen(["node", os.path.join(ROOT, "signin-service", "local",
                                                     "serve.mjs")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=env)
    line = process.stdout.readline()
    assert line.startswith("ready "), process.stderr.read()
    yield "http://127.0.0.1:%s" % line.split()[1]
    process.terminate()
    process.wait(timeout=10)


class _NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _location(url):
    opener = urllib.request.build_opener(_NoFollow)
    try:
        opener.open(url, timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307):
            return exc.headers["Location"]
        raise
    raise AssertionError("no redirect from %s" % url)


def _browser(visited, consent="allow"):
    """Start -> Atlassian's consent (simulated) -> the service -> the desktop."""
    def go(start):
        atlassian_page = _location(start)
        visited.append(atlassian_page)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(atlassian_page).query)
        back = {"state": query["state"][0]}
        back.update({"code": "code-1"} if consent == "allow" else {"error": "access_denied"})
        to_desktop = _location(query["redirect_uri"][0] + "?" + urllib.parse.urlencode(back))
        visited.append(to_desktop)
        urllib.request.urlopen(to_desktop, timeout=10).read()

    return lambda url: (visited.append(url),
                        threading.Thread(target=go, args=(url,), daemon=True).start())


@pytest.fixture
def project(tmp_path, monkeypatch, service):
    monkeypatch.setenv("QAVECTOR_SIGNIN_SERVICE", service)
    return str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json")


def test_signing_in_through_the_service_never_shows_the_desktop_a_secret(
        atlassian, project, service):
    visited = []
    found = oauth.sign_in(*project, open_browser=_browser(visited), seconds=20)
    assert found["account"] == "Alex" and found["sites"][0]["id"] == CLOUD
    exchange = atlassian.seen[0].body
    assert exchange["client_secret"] == SECRET and exchange["code"] == "code-1"
    assert exchange["redirect_uri"] == service + "/atlassian/callback"
    assert visited[0].startswith(service + "/atlassian/start?")
    for url in visited:
        assert "access-1" not in url and "refresh-1" not in url and SECRET not in url
    # Kept like any sign-in, marked as the service's, and refreshed through it.
    entry, problems = store.upsert(
        {"name": "Product team", "auth": "oauth", "site": "https://yourcompany.atlassian.net",
         "cloud_id": CLOUD}, *project, oauth_pending=found["pending"])
    assert problems == []
    blob = json.loads(secret_store.get(store.CREDENTIALS, "product-team", project[1]))
    assert blob["via"] == service and blob["access"] == "access-1"
    assert SECRET not in open(project[0], encoding="utf-8").read()


def test_an_ageing_token_is_refreshed_through_the_service(atlassian, project, service):
    store.upsert({"name": "Product team", "auth": "oauth",
                  "site": "https://yourcompany.atlassian.net", "cloud_id": CLOUD}, *project)
    oauth._write("product-team", {"access": "old", "refresh": "refresh-0",
                                  "expires": time.time() - 5, "via": service}, project[1])
    assert oauth.fresh_token("product-team", *project) == "access-1"
    refresh = atlassian.seen[-1].body
    assert refresh == {"client_id": "client-1", "client_secret": SECRET,
                       "grant_type": "refresh_token", "refresh_token": "refresh-0"}
    blob = json.loads(secret_store.get(store.CREDENTIALS, "product-team", project[1]))
    assert blob["refresh"] == "refresh-1" and blob["via"] == service


def test_a_refusal_comes_back_from_the_service_and_is_said(atlassian, project):
    visited = []
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.sign_in(*project, open_browser=_browser(visited, consent="deny"), seconds=20)
    assert caught.value.kind == "signin" and "access_denied" in str(caught.value)
    assert atlassian.seen == []


def test_a_refused_refresh_through_the_service_asks_to_sign_in_again(atlassian, project,
                                                                     service):
    atlassian.answers["POST /oauth/token"] = (403, {"error": "invalid_grant"})
    store.upsert({"name": "Product team", "auth": "oauth",
                  "site": "https://yourcompany.atlassian.net", "cloud_id": CLOUD}, *project)
    oauth._write("product-team", {"access": "old", "refresh": "refresh-0",
                                  "expires": time.time() - 5, "via": service}, project[1])
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.fresh_token("product-team", *project)
    assert caught.value.kind == "signin"
