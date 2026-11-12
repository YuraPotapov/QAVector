"""Agents: the command-line agents QAVector can talk to, set up once.

A *connection* is one agent CLI on this machine - Claude Code, found on PATH or
at a path somebody chose, with the model it is asked for by default. Unlike a
Jira connection it belongs to QAVector rather than to a project: the CLI signs
in once for the whole machine, so every project sees the same connections.

Layering:

    adapters/  one module per CLI: where it is, who it is signed in as, and
               how its event stream reads
    store      the connections file, beside the rest of the user's data
    cli        ``qavector-core --integrations agents ...``, the GUI's way in

Three rules, the same ones the rest of ``integrations`` holds to:

* **It knows nothing of what uses it.** A cycle step, or anything else, depends
  on this package and calls it; nothing here names a cycle, a step or a plugin.
* **No credential passes through.** An agent CLI signs in by itself and keeps
  what it was given; nothing here reads a token, stores one or hands one on.
* **Nothing is asked without somebody asking.** Whether a CLI is signed in is
  found out when a person presses Test, never on a timer and never at start-up.
"""

#: What the Integrations page calls this provider.
PROVIDER = "agents"
