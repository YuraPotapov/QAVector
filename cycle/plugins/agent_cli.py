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

**The knowledge of the CLI is the Agents integration's**
(``integrations/agents/adapters/claude.py``): where the binary is, how it is
asked who it is, what argv signs in. This module is the cycle's side of it and
adds the one thing only a cycle knows - that a step's processes start inside
that step's restrictions, the sign-in question included.

Nothing in this module runs the agent itself; ``agent.py`` does that.
"""

from cycle import restrict
from integrations.agents.adapters import claude as _claude

DEFAULT_BINARY = _claude.DEFAULT_BINARY
STATUS_TIMEOUT = _claude.STATUS_TIMEOUT

resolve = _claude.resolve
login_argv = _claude.login_argv
describe = _claude.describe


def auth_status(binary=None, timeout=STATUS_TIMEOUT):
    """Who the CLI is signed in as, as plain data. Never raises.

    The adapter's answer (see ``claude.status``), asked from wherever this
    step's processes run: inside its sandbox when it has one.
    """
    return _claude.status(binary, timeout, run=restrict.run)
