"""Integrations: outside services a project is connected to, defined once.

A *connection* is one account on one service - "Product team" on a Jira site -
and it lives in the project. The Integrations page reads and changes things
through it, and so may anything else in QAVector, by its id.

Layering:

    store      the project's connections file, each one's token, and
               ``credentials()`` - a connection ready to use
    activity   what was changed through a connection, and what failed
    oauth      signing in through the browser, and keeping that sign-in fresh
    cli        ``qavector-core --integrations ...``, the GUI's only way in
    jira/      the first provider: client, document format, operations

Four rules this package holds to:

* **It knows nothing of what uses it.** Cycles, or anything else, depend on
  this package and call its neutral API; nothing here names them, carries a
  setting for them or reads their files. ``cycle/connections.py`` is the cycle's
  side of it.
* **One client for every caller.** The page and anything else ask Jira through
  the same session code, so the half that explains why a 401 happened cannot
  drift between them.
* **A token never leaves the core.** It is kept encrypted in the project's
  secrets store (``cycle/secrets.py``) and arrives through a ``--from`` file,
  never argv; no answer, record or log line carries it back.
* **Nothing is asked without somebody asking.** Every operation here is one
  request that a person, or whatever uses the connection, made; nothing in this
  package polls.
"""

#: The providers this build knows, by the id a connection records. A provider
#: is a module with ``operations.call(name, connection, token, request)``.
PROVIDERS = ("jira",)
