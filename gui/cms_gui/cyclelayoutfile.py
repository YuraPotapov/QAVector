"""Where somebody put the nodes, so they are still there tomorrow.

The computed layout is a starting point rather than an answer. On a real cycle
there is always one node somebody wants a little out of the way to read the
rest, and until now that arrangement lasted exactly as long as the window did -
reopen the cycle, or restart the application, and the work of arranging it was
gone. A canvas you can rearrange but not keep is a canvas nobody bothers to
rearrange.

**Not in the cycle file.** A cycle is committed, shipped and read by the core,
and where a box sits on somebody's screen is none of those things. Two people
working on one cycle would otherwise be trading pixel coordinates through a
diff. So it lives here, beside the GUI's other files, and a cycle copied to
another machine simply arrives with the computed layout - which is the right
answer, not a lost one.

**Anything missing falls back to the computed place.** A step added since the
arranging, a renamed one, a file that was never written - all the same thing to
a reader: that node has no remembered place, so it goes where the layout says.
That is what makes this safe to keep stale and safe to delete.

**Where they were looking, as well as where the nodes are.** A zoom and a pan
are the same kind of fact as a dragged node - work somebody did to be able to
read the thing - and they used to last exactly as long as the scene did. They
sit beside the places, per cycle, under ``view``.
"""

import json
import os

#: Bumped when the shape changes in a way an older build could not read.
#: 2 keeps each cycle's places under ``places`` and adds ``view`` beside them;
#: a version-1 file - a bare step-to-place map - still reads, because throwing
#: somebody's arrangement away to add a zoom to it would be a poor trade.
VERSION = 2

FILE_NAME = "cyclelayout.json"

#: How far off the origin a remembered place may be. A coordinate outside this
#: is not a place somebody dragged a node to - it is a corrupt file or a bug,
#: and honouring it would put the node somewhere the view cannot scroll to,
#: which reads as the node having vanished.
LIMIT = 1000000.0


class CycleLayoutFileError(Exception):
    """The layout file could not be read or written."""


def default_path():
    """Beside the GUI's other files, in the user's own data root."""
    from . import servicesfile
    return os.path.join(os.path.dirname(servicesfile.default_path()), FILE_NAME)


def load(path=None):
    """``{cycle id: {"places": {step id: (x, y)}, "view": {...}}}``.

    Never raises; a bad file reads empty. Deliberately forgiving. This is
    cosmetic state, and refusing to open a cycle because the file remembering
    where its boxes sat is malformed would be letting the least important thing
    in the application stop the work.
    """
    path = path or default_path()
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}

    found = {}
    for cycle_id, entry in (document.get("cycles") or {}).items():
        if not isinstance(entry, dict):
            continue
        # Version 1 wrote the places as the entry itself. Told apart by what is
        # in it rather than by the version the file claims, because a file
        # half-written by two builds should still give up what it can.
        places = entry.get("places") if "places" in entry or "view" in entry \
            else entry
        kept = {}
        for step_id, place in (places or {}).items():
            spot = _place(place)
            if spot is not None:
                kept[str(step_id)] = spot
        view = _view(entry.get("view"))
        if kept or view:
            found[str(cycle_id)] = {"places": kept, "view": view}
    return found


def _place(value):
    """One ``(x, y)``, or None when it is not one worth honouring."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if any(one != one or abs(one) > LIMIT for one in (x, y)):
        return None                       # NaN, or off the edge of the world
    return (x, y)


def _view(value):
    """One remembered view, or None when it is not one worth honouring."""
    if not isinstance(value, dict):
        return None
    centre = value.get("center")
    if not isinstance(centre, (list, tuple)) or len(centre) != 2:
        return None
    try:
        zoom = float(value.get("zoom"))
        x, y = float(centre[0]), float(centre[1])
    except (TypeError, ValueError):
        return None
    if any(one != one for one in (zoom, x, y)):
        return None                       # NaN
    if zoom <= 0 or abs(x) > LIMIT or abs(y) > LIMIT:
        return None
    return {"zoom": zoom, "center": [x, y]}


def places_for(cycle_id, path=None):
    """Where this cycle's nodes were last put, or ``{}``."""
    return (load(path).get(str(cycle_id or "")) or {}).get("places") or {}


def view_for(cycle_id, path=None):
    """Where this cycle was last looked at from, or None."""
    return (load(path).get(str(cycle_id or "")) or {}).get("view")


def remember(cycle_id, places, path=None):
    """Write where this cycle's nodes sit now, leaving other cycles alone.

    Read-modify-write, like the other small files here. Two windows arranging
    two cycles at once could lose one of them, and that is the right trade: a
    lock and a temp file for the memory of where a box sat would cost more
    than the thing is worth.
    """
    _keep(cycle_id, path, places=places)


def remember_view(cycle_id, view, path=None):
    """Write where this cycle is being looked at from. None forgets it."""
    _keep(cycle_id, path, view=view)


def _keep(cycle_id, path=None, **changed):
    """Change one half of a cycle's entry, leaving the other half alone."""
    cycle_id = str(cycle_id or "").strip()
    if not cycle_id:
        return
    path = path or default_path()
    document = load(path)
    entry = dict(document.get(cycle_id) or {})
    if "places" in changed:
        entry["places"] = {step_id: [float(x), float(y)]
                           for step_id, (x, y) in (changed["places"] or {}).items()}
    if "view" in changed:
        entry["view"] = _view(changed["view"])
    if entry.get("places") or entry.get("view"):
        document[cycle_id] = entry
    else:
        # Arranging a cycle back to its computed layout and pressing Fit forget
        # it rather than writing down that it has neither: an empty entry is a
        # row that says nothing and has to be skipped by every reader.
        document.pop(cycle_id, None)
    _write(path, document)


def forget(cycle_id, path=None):
    """Drop what was remembered about one cycle. True when there was some."""
    path = path or default_path()
    document = load(path)
    if str(cycle_id) not in document:
        return False
    document.pop(str(cycle_id))
    _write(path, document)
    return True


def _entry(entry):
    """One cycle as it goes on disk. A half it has nothing for is left out."""
    written = {}
    places = entry.get("places") or {}
    if places:
        written["places"] = {step: list(place) for step, place in places.items()}
    if entry.get("view"):
        written["view"] = {"zoom": entry["view"]["zoom"],
                           "center": list(entry["view"]["center"])}
    return written


def _write(path, document):
    """Atomically, and never fatally: a lost arrangement is not a lost cycle."""
    payload = {"version": VERSION,
               "cycles": {cycle_id: _entry(entry)
                          for cycle_id, entry in document.items()}}
    try:
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError as exc:
        raise CycleLayoutFileError("Could not write %s: %s" % (path, exc))
