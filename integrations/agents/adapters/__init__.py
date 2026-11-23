"""One module per agent CLI, each answering the same questions.

An adapter is a module with:

    KIND, NAME            what a connection records, and what a person reads
    DEFAULT_BINARY        the name looked up on PATH when none is given
    EFFORTS               the effort levels the CLI knows, lowest first
    resolve(binary)       the command to run, or "" when it cannot be found
    find(binary)          (path, where) - where: given, PATH or an install place
    INSTALL_HINT          what to tell somebody who does not have the CLI
    status(binary, ...)   who the CLI is signed in as, as plain data
    rate_limits(binary)   optional account usage windows, without a model turn
    describe(status)      one line for a status row
    login_argv(binary)    what to run to sign in
    stages(line, ...)     one line of the CLI's event stream, as rows to show
    MODES                 what a conversation may let the agent do, narrowest first
    turn_argv(binary, ..) the command line for one turn; the message goes to stdin
    turn_env(env)         the environment a turn is started with
    events(line)          one line of the stream, for a conversation's record

Imported by name rather than discovered, so a frozen build carries every one of
them and a kind that is not listed here cannot be loaded by writing it into a
connections file.
"""

from integrations.agents.adapters import claude, codex

_ADAPTERS = {one.KIND: one for one in (claude, codex)}

#: The kinds a connection may be, in the order they are offered.
KINDS = tuple(_ADAPTERS)


class UnknownKind(Exception):
    """A connection names an agent CLI this build has no adapter for."""


def get(kind):
    """The adapter for ``kind``. Raises :class:`UnknownKind`."""
    try:
        return _ADAPTERS[kind]
    except KeyError:
        raise UnknownKind("Unknown agent %r. This build knows: %s."
                          % (kind, ", ".join(KINDS))) from None


def describe():
    """Every kind, and where its CLI is on this machine.

    A look along PATH and where the CLI's installers put it, and nothing more:
    no CLI is started, so this is cheap enough for a form to ask each time it
    opens.
    """
    return [dict(_found(one), kind=one.KIND, name=one.NAME, default_binary=one.DEFAULT_BINARY,
                 install_hint=getattr(one, "INSTALL_HINT", ""), **_rest(one))
            for one in _ADAPTERS.values()]


def _found(adapter):
    """``found`` (the program blank would use, or "") and ``found_where`` -
    PATH, or the place it was found in."""
    finder = getattr(adapter, "find", None)
    path, where = finder("") if finder else (adapter.resolve(""), "PATH")
    return {"found": path, "found_where": where if path else ""}


def _rest(one):
    return dict(efforts=list(one.EFFORTS),
                modes=list(one.MODES), plans=bool(getattr(one, "PLANS", False)),
                mode_description=one.MODE_DESCRIPTION,
                model_default_label=one.MODEL_DEFAULT_LABEL,
                capabilities=one.capabilities().document())
