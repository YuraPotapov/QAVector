"""Talking to the Claude Code CLI: where it is, and whether it is signed in.

The reason this exists is authentication. CrewAI and AutoGen resolve
credentials themselves - through litellm and autogen-ext - and both look for an
API key in the environment, so using either means somebody first creates a key
in a web console, works out where to export it, and keeps it somewhere. That is
a developer's errand, and it is the single thing standing between "I opened
QAVector" and "an agent reviewed my failing tests".

The Claude Code CLI has already solved it: ``claude auth login`` is a browser
sign-in against a Claude subscription or a Console account, and the credentials
it stores are the CLI's own business afterwards. Nothing here ever sees a token,
stores one, or passes one - the binary is invoked and it knows who it is.

So this module is deliberately small. It answers three questions:

* where is the binary,
* is it signed in, and as whom,
* what argv signs in.

``claude auth status --json`` answers the second in about a second, which is
cheap enough for a settings page to ask on demand and far too slow to ask on
every start - so nothing here is called from ``--describe``.

Nothing in this module runs the agent itself; ``agent.py`` does that.
"""

import json
import os
import shutil
import subprocess

import runtime_paths

#: What the binary is called. Overridable per step, because somebody may have it
#: installed somewhere PATH does not reach - a nodenv shim, a per-project copy.
DEFAULT_BINARY = "claude"

#: How long to wait for `auth status`. It is a local read of a credentials file
#: plus, sometimes, a token refresh - a second is normal, ten means something is
#: wrong and a settings page should say so rather than hang.
STATUS_TIMEOUT = 20.0


def resolve(binary=None):
    """The command to run. Returns "" when it cannot be found.

    A bare name is looked up on PATH; anything with a separator in it is taken
    as a path and checked. Returning "" rather than raising keeps "it is not
    installed" a thing the caller reports, which is the useful answer.
    """
    name = (binary or "").strip() or DEFAULT_BINARY
    if os.path.dirname(os.path.expanduser(name)):
        path = os.path.abspath(os.path.expanduser(name))
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else ""
    return shutil.which(name) or ""


def auth_status(binary=None, timeout=STATUS_TIMEOUT):
    """Who the CLI is signed in as, as plain data. Never raises.

    Always returns the same shape, so a caller renders one thing::

        {"installed": bool, "logged_in": bool, "binary": str,
         "email": str, "organisation": str, "plan": str, "method": str,
         "problem": str}

    ``problem`` is empty when all is well and a sentence otherwise - a missing
    binary, a CLI too old to have the subcommand, a timeout. It is what a
    settings page shows and what an agent step quotes when it refuses to start.
    """
    found = {"installed": False, "logged_in": False, "binary": "", "email": "",
             "organisation": "", "plan": "", "method": "", "problem": ""}

    path = resolve(binary)
    if not path:
        found["problem"] = ("Claude Code is not installed, or %r is not on PATH."
                            % ((binary or "").strip() or DEFAULT_BINARY))
        return found
    found["installed"], found["binary"] = True, path

    try:
        finished = subprocess.run(
            [path, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
            env=runtime_paths.clean_subprocess_env(),
            # Somebody else's home directory is not ours to guess at; the CLI
            # reads its own configuration from the environment it is given.
            cwd=os.path.expanduser("~"))
    except (OSError, subprocess.SubprocessError) as exc:
        found["problem"] = "Cannot ask %s who it is signed in as: %s" % (path, exc)
        return found

    text = (finished.stdout or b"").decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        # An older CLI without `auth status`, or one that printed a prompt.
        detail = ((finished.stderr or b"").decode("utf-8", "replace").strip()
                  or text)
        found["problem"] = ("%s did not answer with the sign-in status%s"
                            % (path, ": %s" % detail[:200] if detail else "."))
        return found
    if not isinstance(payload, dict):
        found["problem"] = "%s answered with something unexpected." % path
        return found

    found["logged_in"] = bool(payload.get("loggedIn"))
    found["email"] = str(payload.get("email") or "")
    found["organisation"] = str(payload.get("orgName") or "")
    found["plan"] = str(payload.get("subscriptionType") or "")
    found["method"] = str(payload.get("authMethod") or "")
    if not found["logged_in"]:
        found["problem"] = "Not signed in. Run `%s auth login` once." % path
    return found


def login_argv(binary=None, console=False, email=""):
    """What to run to sign in. The CLI opens a browser and waits.

    Handed back rather than run, because who runs it differs: the settings page
    starts it and watches, and somebody at a terminal just types it.
    """
    path = resolve(binary) or ((binary or "").strip() or DEFAULT_BINARY)
    argv = [path, "auth", "login", "--console" if console else "--claudeai"]
    if email:
        argv.extend(["--email", email])
    return argv


def describe(status):
    """One line for a status row, from what :func:`auth_status` returned."""
    if not status.get("installed"):
        return "Claude Code is not installed"
    if not status.get("logged_in"):
        return "Not signed in"
    who = status.get("email") or status.get("organisation") or "signed in"
    plan = status.get("plan")
    return "Signed in as %s%s" % (who, " (%s)" % plan if plan else "")
