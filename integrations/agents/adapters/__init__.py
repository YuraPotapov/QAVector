"""One module per agent CLI, each answering the same questions.

An adapter is a module with:

    KIND, NAME            what a connection records, and what a person reads
    DEFAULT_BINARY        the name looked up on PATH when none is given
    EFFORTS               the effort levels the CLI knows, lowest first
    resolve(binary)       the command to run, or "" when it cannot be found
    status(binary, ...)   who the CLI is signed in as, as plain data
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

from integrations.agents.adapters import claude

_ADAPTERS = {claude.KIND: claude}

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

    A look along PATH and nothing more: no CLI is started, so this is cheap
    enough for a form to ask each time it opens.
    """
    return [{"kind": one.KIND, "name": one.NAME, "default_binary": one.DEFAULT_BINARY,
             "found": one.resolve(""), "efforts": list(one.EFFORTS),
             "modes": list(one.MODES)}
            for one in _ADAPTERS.values()]
