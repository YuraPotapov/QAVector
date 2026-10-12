"""Changing a cycle file where it changed, and nowhere else.

A form edits a *document* - the plain mapping ``model.to_document`` makes - and
hands it back. Writing that document out whole is correct and loses everything
the document does not carry: every comment somebody wrote beside a step, the
order they put things in, a block of text laid out to be read. The development
cycles are a hundred lines of such comments, and one tick in a step's dialog
used to take every one of them with it, silently.

So a save from a form is applied to the file as **edits**: the document the
form started from is compared with the one it returns, and only the entries
that differ are rewritten in the text. An entry is replaced in place when it
fits on its line, so a comment after it stays; a whole entry, a step, or a
mapping is rewritten only when that is what changed.

This module only finds the places. What a value looks like as YAML is
``cycle/cyclefile.py``'s business and is handed in as ``render``, so there is
still one writer of cycle YAML, not two.

Anything this cannot do exactly - a shape it does not recognise, steps put in
a different order, a file whose parsed form does not line up with its text -
raises :class:`PatchError`, and the caller falls back to writing the whole
document. Whether *that* loses anything is measured separately, by
:func:`lost_comments`, and a loss is put to the person rather than accepted
for them.
"""

import collections

#: Stands for "this key is not there", which None cannot: None is a value.
MISSING = object()


class PatchError(Exception):
    """This change cannot be made as edits; write the whole document instead."""


# -- comments -----------------------------------------------------------------
def comments(text):
    """Every comment in ``text`` as written, in order, or None if unreadable.

    A ``#`` starts a comment only outside a value - inside a quoted string or a
    block of text it is content - so the values' own spans are taken from the
    parser rather than guessed at with a regular expression.
    """
    import yaml

    from engine import yamlload
    try:
        root = yamlload.compose(text)
    except Exception:  # noqa: BLE001 - an unreadable file has no answer here
        return None
    spans = []
    if root is not None:
        _scalar_spans(root, spans)
    spans.sort()
    found, position = [], 0
    while True:
        at = text.find("#", position)
        if at < 0:
            return found
        position = at + 1
        if at > 0 and text[at - 1] not in " \t\n":
            continue
        if any(start <= at < end for start, end in spans):
            continue
        line_end = text.find("\n", at)
        found.append(text[at:line_end if line_end >= 0 else len(text)].rstrip())
        position = line_end if line_end >= 0 else len(text)


def lost_comments(before, after):
    """The comments in ``before`` that ``after`` no longer has, in order."""
    old, new = comments(before or ""), comments(after or "")
    if not old or new is None:
        return []
    remaining = collections.Counter(new)
    lost = []
    for one in old:
        if remaining[one]:
            remaining[one] -= 1
        else:
            lost.append(one)
    return lost


def _scalar_spans(node, spans):
    import yaml
    if isinstance(node, yaml.ScalarNode):
        spans.append((node.start_mark.index, node.end_mark.index))
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            _scalar_spans(key, spans)
            _scalar_spans(value, spans)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _scalar_spans(item, spans)


# -- the patch ----------------------------------------------------------------
def patch(text, base, new, render):
    """``text``, edited so that where it said ``base`` it now says ``new``.

    ``base`` is the document the editor was given for ``text`` and ``new`` the
    one it handed back; only their differences touch the text. ``render`` is
    the writer's own vocabulary - see ``cyclefile._Renderer``. Raises
    :class:`PatchError` when the change cannot be made as edits.
    """
    import yaml

    from engine import yamlload
    try:
        root = yamlload.compose(text)
    except Exception as exc:  # noqa: BLE001
        raise PatchError("the file does not parse: %s" % exc)
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise PatchError("a cycle file is a block mapping")
    edits = []
    _mapping(text, root, base, new, "cycle", render, edits)
    return _apply(text, edits)


def _entries(node):
    import yaml
    if node.flow_style:
        raise PatchError("a flow mapping is rewritten whole")
    entries = []
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            raise PatchError("a key that is not a plain name")
        entries.append((key.value, key, value))
    if len({name for name, _k, _v in entries}) != len(entries):
        raise PatchError("a key written twice")
    return entries


def _mapping(text, node, base, new, context, render, edits):
    import yaml
    entries = _entries(node)
    index = {name: (key, value) for name, key, value in entries}
    names = list(dict.fromkeys(list(base) + list(new)))
    for name in names:
        old, now = base.get(name, MISSING), new.get(name, MISSING)
        if old is not MISSING and now is not MISSING and old == now:
            continue
        if old is MISSING and now is MISSING:
            continue
        if name not in index:
            if now is not MISSING:
                _insert(text, node, entries, context, name, now, render, edits)
            continue
        key, value = index[name]
        if now is MISSING:
            _delete(text, node, key, value, edits)
        elif (isinstance(old, dict) and isinstance(now, dict)
              and isinstance(value, yaml.MappingNode) and not value.flow_style
              and value.value):
            _mapping(text, value, old, now, render.child(context, name), render,
                     edits)
        elif (context == "cycle" and name == "steps" and isinstance(old, list)
              and isinstance(now, list) and isinstance(value, yaml.SequenceNode)
              and not value.flow_style and value.value):
            _steps(text, value, old, now, render, edits)
        else:
            _replace(text, key, value, context, name, now, render, edits)


def _replace(text, key, value, context, name, now, render, edits):
    """One entry's value, in place when it fits on its line, else the entry."""
    import yaml
    inline = render.inline(now)
    one_line = value.start_mark.line == value.end_mark.line
    # A plain scalar's style is None to the pure parser and '' to libyaml.
    in_place = (isinstance(value, yaml.ScalarNode)
                and (value.style or None) in (None, '"', "'")
                and value.start_mark.index < value.end_mark.index) or (
        isinstance(value, (yaml.MappingNode, yaml.SequenceNode)) and value.flow_style)
    if inline is not None and one_line and in_place:
        edits.append((value.start_mark.index, value.end_mark.index, inline))
        return
    column = key.start_mark.column
    lines = render.entry(context, name, now, column)
    replacement = "\n".join([lines[0][column:]] + lines[1:]) + "\n"
    edits.append((key.start_mark.index,
                  _line_end(text, _entry_end(text, key, value)), replacement))


def _delete(text, node, key, value, edits):
    start = _line_start(text, key.start_mark.index)
    if text[start:key.start_mark.index].strip():
        # The first entry of a list item shares its line with the dash.
        raise PatchError("cannot take the first entry off a list item")
    edits.append((start, _line_end(text, _entry_end(text, key, value)), ""))


def _insert(text, node, entries, context, name, now, render, edits):
    """A new entry, after the one it follows in the writer's own order."""
    order = render.order(context)
    if order is not None and name in order:
        before = set(order[:order.index(name)])
    else:
        before = {one for one, _k, _v in entries if one < name}
    anchor = None
    for one, key, value in entries:
        if one in before:
            anchor = (key, value)
    column = entries[0][1].start_mark.column
    lines = render.entry(context, name, now, column)
    block = "\n".join(lines) + "\n"
    if anchor is None:
        first = entries[0][1]
        start = _line_start(text, first.start_mark.index)
        if not text[start:first.start_mark.index].strip():
            edits.append((start, start, block))
            return
        anchor = (first, entries[0][2])     # a list item: go after its first entry
    at = _line_end(text, _entry_end(text, *anchor))
    edits.append((at, at, block))


def _steps(text, sequence, base, new, render, edits):
    """Steps by id: the changed ones edited, removed ones cut, new ones added.

    Steps put in a different order are not attempted - the order is the
    file's layout, and moving blocks of commented text about is where a
    patch would start guessing.
    """
    import yaml
    items = sequence.value
    raw_ids = []
    for item in items:
        if not isinstance(item, yaml.MappingNode):
            raise PatchError("a step that is not a mapping")
        found = [value.value for key, value in item.value
                 if isinstance(key, yaml.ScalarNode) and key.value == "id"
                 and isinstance(value, yaml.ScalarNode)]
        raw_ids.append(str(found[0]).strip() if found else "")
    old_ids = [str(one.get("id") or "") for one in base]
    new_ids = [str(one.get("id") or "") for one in new]
    if raw_ids != old_ids or len(set(new_ids)) != len(new_ids):
        raise PatchError("the steps do not line up with the file")

    if len(old_ids) == len(new_ids) and all(
            one == two or (one not in new_ids and two not in old_ids)
            for one, two in zip(old_ids, new_ids)):
        # Same places; an id may have been changed where it stands.
        pairs = list(zip(range(len(items)), new))
        added = []
    else:
        kept = [one for one in old_ids if one in new_ids]
        if kept != [one for one in new_ids if one in old_ids]:
            raise PatchError("the steps were put in another order")
        for position, one in enumerate(old_ids):
            if one not in new_ids:
                _cut_item(text, items[position], edits)
        pairs = [(old_ids.index(step["id"]), step) for step in new
                 if step.get("id") in old_ids]
        added = [step for step in new if step.get("id") not in old_ids]

    for position, step in pairs:
        if base[position] == step:
            continue
        item = items[position]
        if item.flow_style:
            _replace_item(text, item, step, render, edits)
        else:
            _mapping(text, item, base[position], step, "step", render, edits)

    if not added:
        return
    dash = _dash_column(text, items[0])
    # Each new step goes after the step it follows in the new list, so a step
    # added in the middle lands in the middle. Several in a row share a place
    # and are written in their own order.
    places = collections.OrderedDict()
    previous = None
    for step in new:
        if step.get("id") in old_ids:
            previous = old_ids.index(step["id"])
            continue
        places.setdefault(previous, []).append(step)
    for after, steps in places.items():
        block = "".join("\n".join(render.item(step, dash)) + "\n" for step in steps)
        if after is None:
            at = _line_start(text, items[0].start_mark.index)
        else:
            at = _line_end(text, _content_end(text, items[after]))
        edits.append((at, at, block))


def _cut_item(text, item, edits):
    start = _line_start(text, item.start_mark.index)
    if text[start:item.start_mark.index].strip() != "-":
        raise PatchError("a step that does not start its own line")
    edits.append((start, _line_end(text, _content_end(text, item)), ""))


def _replace_item(text, item, step, render, edits):
    dash = _dash_column(text, item)
    lines = render.item(step, dash)
    # Everything up to the dash stays; the rest is the new step.
    lines[0] = lines[0][dash + 2:]
    edits.append((item.start_mark.index,
                  _line_end(text, _content_end(text, item)),
                  "\n".join(lines) + "\n"))


def _dash_column(text, item):
    start = _line_start(text, item.start_mark.index)
    prefix = text[start:item.start_mark.index]
    if prefix.strip() != "-":
        raise PatchError("a step that does not start its own line")
    return prefix.index("-")


# -- positions ----------------------------------------------------------------
def _content_end(text, node):
    """Where what ``node`` says ends - before the blank lines and comments a
    block collection's own end mark runs on into."""
    import yaml
    if isinstance(node, yaml.MappingNode) and not node.flow_style and node.value:
        key, value = node.value[-1]
        return _entry_end(text, key, value)
    if isinstance(node, yaml.SequenceNode) and not node.flow_style and node.value:
        return _content_end(text, node.value[-1])
    if isinstance(node, yaml.ScalarNode) and node.style in ("|", ">"):
        start, end = node.start_mark.index, node.end_mark.index
        return start + len(text[start:end].rstrip())
    return node.end_mark.index


def _entry_end(text, key, value):
    """The end of one ``key: value``. An empty value ends with its key."""
    import yaml
    if (isinstance(value, yaml.ScalarNode)
            and value.start_mark.index == value.end_mark.index):
        return key.end_mark.index
    return max(key.end_mark.index, _content_end(text, value))


def _line_start(text, index):
    return text.rfind("\n", 0, index) + 1


def _line_end(text, index):
    """Just past the newline that ends the line ``index`` is on."""
    found = text.find("\n", index)
    return len(text) if found < 0 else found + 1


def _apply(text, edits):
    ordered = sorted(edits, key=lambda one: (one[0], one[1]))
    for (start, end, _new), (after, _e, _n) in zip(ordered, ordered[1:]):
        if end > after:
            raise PatchError("two changes overlap")
    for start, end, replacement in reversed(ordered):
        text = text[:start] + replacement + text[end:]
    return text
