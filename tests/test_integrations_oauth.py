"""Signing in to Jira Cloud through the browser, against a loopback Atlassian.

The browser is a thread that follows the authorize URL's redirect the way
Atlassian would. What matters: the code is exchanged with the project's own app,
the tokens never reach an answer or the connections file, a refresh replaces the
rotating refresh token, and requests go through the API gateway with links still
pointing at the site.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cycle import registry, secrets as secret_store                    # noqa: E402
from domain.cycle import Cycle, CycleStep                               # noqa: E402
from cycle import connections as binding                                # noqa: E402
from integrations import cli, oauth, store                              # noqa: E402
from integrations.jira import operations                                # noqa: E402
from integrations.jira import client                                    # noqa: E402
from integrations.jira.client import Session                            # noqa: E402
from fake_jira import FakeJira                                          # noqa: E402

SECRET = "app-secret-not-a-real-one"
SITE = "https://yourcompany.atlassian.net"
CLOUD = "11111111-2222-3333-4444-555555555555"


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def atlassian(monkeypatch):
    tokens = iter(range(1, 100))

    def token(request):
        n = next(tokens)
        return {"access_token": "access-%d" % n, "refresh_token": "refresh-%d" % n,
                "expires_in": 3600}

    server = FakeJira({
        "POST /oauth/token": token,
        "/oauth/token/accessible-resources": [{"id": CLOUD, "name": "yourcompany",
                                               "url": SITE + "/", "scopes": []}],
        "/ex/jira/%s/rest/api/3/myself" % CLOUD: {"displayName": "Alex"},
    })
    monkeypatch.setattr(oauth, "AUTH_BASE", server.site)
    monkeypatch.setattr(client, "GATEWAY", server.site)
    yield server
    server.close()


@pytest.fixture
def project(tmp_path):
    paths = (str(tmp_path / "integrations.json"), str(tmp_path / "cyclesecrets.json"))
    assert oauth.save_app("client-1", SECRET, _free_port(), *paths) == []
    return paths


def _browser(code="code-1", state=None, error=""):
    """A browser that goes where the authorize URL says to come back to."""
    seen = []

    def open_browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        seen.append(query)
        back = {"state": state if state is not None else query["state"][0]}
        back.update({"error": error} if error else {"code": code})
        target = query["redirect_uri"][0].replace("localhost", "127.0.0.1")
        threading.Thread(target=_visit, args=(target + "?" + urllib.parse.urlencode(back),),
                         daemon=True).start()

    open_browser.seen = seen
    return open_browser


def _visit(url):
    time.sleep(0.05)
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except Exception:                   # noqa: BLE001 - a 400 page is still a visit
        pass


# -- the app ------------------------------------------------------------------
def test_the_app_s_secret_is_kept_encrypted_and_never_shown(project):
    shown = oauth.app(*project)
    assert shown["client_id"] == "client-1" and shown["secret_set"] is True
    assert shown["callback"] == "http://localhost:%d/callback" % shown["port"]
    assert SECRET not in json.dumps(shown)
    assert SECRET not in open(project[0], encoding="utf-8").read()
    assert secret_store.get(store.CREDENTIALS, "app:atlassian", project[1]) == SECRET


def test_the_app_needs_an_id_a_secret_and_a_sane_port(tmp_path):
    paths = (str(tmp_path / "i.json"), str(tmp_path / "s.json"))
    assert oauth.save_app("client-1", "", 80, *paths) == [
        "The callback port must be a number from 1024 to 65535.",
        "Secret is required with a client ID."]
    assert oauth.save_app("", "", 50000, *paths, service="ftp://x") == [
        "The sign-in service must be an https:// address."]


def test_saving_the_app_keeps_the_connections(project, tmp_path):
    store.upsert({"name": "Ops", "site": "https://x.example.invalid",
                  "email": "you@yourcompany.example"}, *project, new_token="t")
    oauth.save_app("client-2", "", 50000, *project)
    assert [one["name"] for one in store.load(project[0])] == ["Ops"]
    assert oauth.app(*project)["client_id"] == "client-2"


# -- signing in ---------------------------------------------------------------
def test_a_sign_in_exchanges_the_code_with_the_project_s_app(atlassian, project):
    browser = _browser()
    found = oauth.sign_in(*project, open_browser=browser, seconds=10)
    query = browser.seen[0]
    assert query["client_id"] == ["client-1"] and query["prompt"] == ["consent"]
    assert set(query["scope"][0].split()) == set(oauth.SCOPES)
    exchange = atlassian.seen[0].body
    assert exchange == {"grant_type": "authorization_code", "client_id": "client-1",
                        "client_secret": SECRET, "code": "code-1",
                        "redirect_uri": query["redirect_uri"][0]}
    assert atlassian.seen[1].authorization == "Bearer access-1"
    assert found["sites"] == [{"id": CLOUD, "name": "yourcompany", "url": SITE}]
    assert found["account"] == "Alex"
    assert "access-1" not in json.dumps(found) and found["pending"].startswith("pending:")


def test_the_pending_sign_in_becomes_a_connection_s(atlassian, project):
    found = oauth.sign_in(*project, open_browser=_browser(), seconds=10)
    entry, problems = store.upsert(
        {"name": "Product team", "auth": "oauth", "site": SITE, "cloud_id": CLOUD},
        *project, oauth_pending=found["pending"])
    assert problems == [] and entry["email"] == ""
    assert store.describe(*project)[0]["token_set"] is True
    assert secret_store.get(store.CREDENTIALS, found["pending"], project[1]) == ""
    assert "access-1" not in open(project[0], encoding="utf-8").read()
    assert oauth.fresh_token("product-team", *project) == "access-1"


def test_a_return_that_is_not_this_sign_in_s_is_ignored(atlassian, project):
    def browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        back = query["redirect_uri"][0].replace("localhost", "127.0.0.1")
        threading.Thread(target=lambda: (
            _visit(back + "?code=stolen&state=forged"),
            _visit(back + "?" + urllib.parse.urlencode(
                {"code": "code-9", "state": query["state"][0]}))), daemon=True).start()

    oauth.sign_in(*project, open_browser=browser, seconds=10)
    assert atlassian.seen[0].body["code"] == "code-9"


def test_a_refusal_in_the_browser_is_said_plainly(atlassian, project):
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.sign_in(*project, open_browser=_browser(error="access_denied"), seconds=10)
    assert caught.value.kind == "signin" and "access_denied" in str(caught.value)
    assert atlassian.seen == []


def test_a_sign_in_can_be_cancelled(project):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.sign_in(*project, open_browser=lambda url: None, cancel=cancel, seconds=10)
    assert caught.value.kind == "cancelled"


def test_a_taken_port_is_said_with_what_to_do(project):
    holder = socket.socket()
    holder.bind(("127.0.0.1", oauth.app(*project)["port"]))
    holder.listen(1)
    try:
        with pytest.raises(oauth.OAuthError) as caught:
            oauth.sign_in(*project, open_browser=lambda url: None, seconds=1)
        assert caught.value.kind == "busy" and "Choose another" in str(caught.value)
    finally:
        holder.close()


def test_without_an_app_or_a_service_signing_in_says_there_is_no_way(tmp_path, monkeypatch):
    monkeypatch.delenv("QAVECTOR_SIGNIN_SERVICE", raising=False)
    monkeypatch.setattr(oauth, "_built_in", lambda: "")
    shown = oauth.app(str(tmp_path / "i.json"), str(tmp_path / "s.json"))
    assert shown["mode"] == "none" and shown["service"] == ""


def test_the_service_comes_from_the_project_the_environment_or_the_build(tmp_path,
                                                                         monkeypatch):
    paths = (str(tmp_path / "i.json"), str(tmp_path / "s.json"))
    monkeypatch.setattr(oauth, "_built_in", lambda: "https://built.example.invalid")
    monkeypatch.delenv("QAVECTOR_SIGNIN_SERVICE", raising=False)
    assert oauth.service(paths[0]) == "https://built.example.invalid"
    monkeypatch.setenv("QAVECTOR_SIGNIN_SERVICE", "https://env.example.invalid/")
    assert oauth.service(paths[0]) == "https://env.example.invalid"
    assert oauth.save_app("", "", 50000, *paths, service="https://own.example.invalid") == []
    assert oauth.service(paths[0]) == "https://own.example.invalid"
    assert oauth.app(*paths)["mode"] == "service"


def test_without_the_app_signing_in_asks_for_it(tmp_path, monkeypatch):
    monkeypatch.delenv("QAVECTOR_SIGNIN_SERVICE", raising=False)
    monkeypatch.setattr(oauth, "_built_in", lambda: "")
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.sign_in(str(tmp_path / "i.json"), str(tmp_path / "s.json"),
                      open_browser=lambda url: pytest.fail("opened the browser"))
    assert caught.value.kind == "app"


# -- refreshing ---------------------------------------------------------------
def _signed(project, expires):
    store.upsert({"name": "Product team", "auth": "oauth", "site": SITE,
                  "cloud_id": CLOUD}, *project)
    oauth._write("product-team", {"access": "old-access", "refresh": "old-refresh",
                                  "expires": expires}, project[1])


def test_a_good_token_is_used_as_it_is(atlassian, project):
    _signed(project, time.time() + 3000)
    assert oauth.fresh_token("product-team", *project) == "old-access"
    assert atlassian.seen == []


def test_an_ageing_token_is_refreshed_and_the_new_refresh_token_kept(atlassian, project):
    _signed(project, time.time() + 30)
    assert oauth.fresh_token("product-team", *project) == "access-1"
    assert atlassian.seen[0].body == {"grant_type": "refresh_token", "client_id": "client-1",
                                      "client_secret": SECRET, "refresh_token": "old-refresh"}
    blob = json.loads(secret_store.get(store.CREDENTIALS, "product-team", project[1]))
    assert blob["refresh"] == "refresh-1" and blob["access"] == "access-1"
    assert not os.path.exists(project[1] + ".oauth.lock")


def test_a_refused_refresh_asks_to_sign_in_again(atlassian, project):
    atlassian.answers["POST /oauth/token"] = (403, {"error": "invalid_grant",
                                                    "error_description": "expired"})
    _signed(project, time.time() - 10)
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.fresh_token("product-team", *project)
    assert caught.value.kind == "signin"


# -- using it -----------------------------------------------------------------
def test_requests_go_through_the_gateway_and_links_stay_on_the_site(atlassian, project):
    _signed(project, time.time() + 3000)
    atlassian.answers["/ex/jira/%s/rest/api/3/search/jql" % CLOUD] = {
        "isLast": True, "issues": [{"key": "QA-1", "fields": {"summary": "x"}}]}
    connection = store.get("product-team", project[0])
    answer = operations.call("search", connection, "old-access", {})
    request = atlassian.seen[-1]
    assert request.path == "/ex/jira/%s/rest/api/3/search/jql" % CLOUD
    assert request.authorization == "Bearer old-access"
    assert answer["rows"][0]["url"] == SITE + "/browse/QA-1"


def test_an_attachment_on_the_site_is_fetched_through_the_gateway(atlassian):
    atlassian.answers["/ex/jira/%s/rest/api/3/attachment/content/10" % CLOUD] = b"PNG"
    get = Session(SITE, "bearer", "", "t", api=oauth.api_base(CLOUD))
    assert get.fetch(SITE + "/rest/api/3/attachment/content/10") == b"PNG"
    with pytest.raises(Exception):
        get.fetch("https://elsewhere.example.invalid/x")


def test_the_cli_answers_with_a_fresh_token_s_result(atlassian, project, capsys):
    _signed(project, time.time() - 10)
    atlassian.answers["/ex/jira/%s/rest/api/3/myself" % CLOUD] = {"displayName": "Alex"}
    code = cli.main(["test", "product-team", "--integrations-file=" + project[0],
                     "--cycle-secrets-file=" + project[1]])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["result"]["account"] == "Alex"
    assert atlassian.seen[-1].authorization == "Bearer access-1"


def test_a_cycle_step_gets_the_gateway_and_a_fresh_bearer_token(atlassian, project):
    _signed(project, time.time() + 3000)
    cycle = Cycle(id="c", steps=(CycleStep("q", "jira.issues",
                                           settings={"connection": "product-team"}),))
    found, problems = binding.load(cycle, None, *project)
    assert problems == []
    expanded = binding.expand(registry.get("jira.issues"), {"connection": "product-team"},
                              found)
    assert expanded["auth"] == "bearer" and expanded["token"] == "old-access"
    assert expanded["api"] == oauth.api_base(CLOUD) and expanded["site"] == SITE
    assert expanded["email"] == ""


def test_signing_in_from_the_command_line(atlassian, project, capsys, monkeypatch):
    monkeypatch.setattr(oauth.webbrowser, "open", _browser())
    code = cli.main(["sign-in", "--integrations-file=" + project[0],
                     "--cycle-secrets-file=" + project[1]])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0 and payload["sites"][0]["id"] == CLOUD
    assert "access-1" not in captured.out + captured.err
