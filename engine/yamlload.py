"""Reading YAML data, with libyaml when it is there.

``yaml.safe_load`` is the pure-Python parser even when PyYAML was built with
libyaml, and on this application's files that is most of the time any read
takes: ``--describe`` spent four fifths of its time inside it, and every cycle
save parses the file twice. The C loader gives the same data several times
faster, so every data read goes through here.

:func:`compose` is the same for the node tree ``cycle/cyclepatch.py`` edits a
file by. The two parsers put every node at the same index, line and column -
checked across every cycle and scenario file, Cyrillic included - with one
difference worth knowing: libyaml calls a plain scalar's style ``''`` where the
pure parser says ``None``, so a caller asking "is this plain?" asks
``not node.style``.
"""

import yaml

#: libyaml's loader when PyYAML was built with it, the pure one otherwise.
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(stream):
    """What ``yaml.safe_load(stream)`` returns, read by :data:`LOADER`."""
    return yaml.load(stream, Loader=LOADER)


def compose(stream):
    """What ``yaml.compose(stream)`` returns, read by :data:`LOADER`."""
    return yaml.compose(stream, Loader=LOADER)
