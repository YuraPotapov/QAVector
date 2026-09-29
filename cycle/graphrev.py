"""Graph revisions: what a run's graph may become, checked before it does.

A cycle file is a template. The design in ``docs/cycle-adaptive-design.md``
lets a run grow past it - an agent proposes new steps at a revision point, the
core checks the proposal, a person agrees, and the run's *effective graph*
becomes the template plus every accepted revision, in order.

This module is the checking and the replaying, and nothing else. It is pure -
documents in, documents and messages out - so the rules can be tested without
a run, and **the executor does not call it yet**: no run changes shape today.
It exists first because the rules are the part that must be right before any
of it is wired in, and because a design whose rules have never been executed
is a guess.

Everything is expressed in the shapes that already exist. A proposed step is a
step exactly as ``model.to_document`` writes one; the effective graph is a
cycle document, validated by ``model.problems`` like any file. There is no
second step format and no second validator.
"""

import copy
import hashlib
import json

from cycle import model, variables

#: What a proposal says it is. Versioned so a later shape can be told apart.
SCHEMA = "qavector.cycle.proposal/1"

#: The plugin a revision point is a step of.
POINT = "plan.revise"

#: Plugins that read and judge and write nothing outside the run. Only these
#: may be allowed at a point whose changes are approved by policy rather than
#: by a person - an unattended run must not be able to add work that changes
#: a repository, a board or a service.
READ_ONLY = ("agent.review", "check.gate", "report.json", "report.html")

#: The most steps any run may reach through revisions, whatever a point says.
MAX_TOTAL = 80


def digest(proposal):
    """A proposal's identity: the same proposal twice is one proposal."""
    raw = json.dumps(proposal, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# -- the point's own rules ------------------------------------------------------
def policy_of(step):
    """What a revision point allows, from its ``with:`` settings.

    ``step`` is a step as a document writes it. Everything is closed by
    default: no plugin is allowed unless named, nothing may wait on anything
    but the point itself, a person decides.
    """
    settings = dict((step or {}).get("with") or {})

    def names(key):
        value = settings.get(key) or []
        return tuple(str(one) for one in (value if isinstance(value, list) else [value]))

    def number(key, default):
        try:
            return int(settings.get(key, default))
        except (TypeError, ValueError):
            return default

    return {"point": (step or {}).get("id", ""), "proposer": str(settings.get("proposer") or ""),
            "allow": names("allow"), "attach_after": names("attach_after"),
            "before": names("before"), "allow_secrets": names("allow_secrets"),
            "max_steps": number("max_steps", 3), "max_revisions": number("max_revisions", 2),
            "approve": str(settings.get("approve") or "person")}


def policy_problems(policy):
    """What is wrong with a point's rules themselves, before any proposal."""
    found = []
    where = "%s:" % policy["point"]
    if not policy["allow"]:
        found.append("%s allow names no plugin, so nothing could ever be added." % where)
    if policy["approve"] not in ("person", "policy"):
        found.append("%s approve is %r; it has to be person or policy."
                     % (where, policy["approve"]))
    elif policy["approve"] == "policy":
        writers = [one for one in policy["allow"] if one not in READ_ONLY]
        if writers:
            found.append("%s approve: policy lets steps be added with nobody asked, "
                         "so it may allow only plugins that change nothing outside "
                         "the run; %s %s." % (where, ", ".join(writers),
                                              "does" if len(writers) == 1 else "do"))
    if not 1 <= policy["max_steps"] <= 20:
        found.append("%s max_steps has to be between 1 and 20." % where)
    if not 1 <= policy["max_revisions"] <= 10:
        found.append("%s max_revisions has to be between 1 and 10." % where)
    return found


# -- replaying ------------------------------------------------------------------
def effective(template, revisions):
    """The run's graph: the template with every revision applied in order.

    A pure function of what is on disk, so Resume, a replay of an old run and a
    report all arrive at the same graph without asking anybody anything again.
    """
    document = copy.deepcopy(template or {})
    for revision in revisions or ():
        document = _apply(document, revision)
    return document


def _apply(document, revision):
    steps = list(document.get("steps") or [])
    removed = set(revision.get("remove") or ())
    replaced = {one["id"]: one for one in revision.get("replace") or []}
    steps = [copy.deepcopy(replaced.get(one.get("id"), one))
             for one in steps if one.get("id") not in removed]
    for one in steps:
        needs = one.get("needs") or []
        if isinstance(needs, list) and removed & set(needs):
            one["needs"] = [need for need in needs if need not in removed]
            if not one["needs"]:
                del one["needs"]
    # New steps go straight after the point, in the order proposed, so the
    # file order - which decides layout ties - reads the way the run grew.
    added = [copy.deepcopy(one) for one in revision.get("add") or []]
    point = revision.get("point")
    where = next((index for index, one in enumerate(steps) if one.get("id") == point),
                 len(steps) - 1)
    steps[where + 1:where + 1] = added
    # A point's `before` steps wait for what it added.
    for one in steps:
        if one.get("id") in (revision.get("before") or ()) and added:
            needs = list(one.get("needs") or [])
            one["needs"] = needs + [step["id"] for step in added if step["id"] not in needs]
    document["steps"] = steps
    return document


def applied(revisions, proposal):
    """The revision this exact proposal already made, or None.

    What stops a crash from adding the same steps twice: the point is run
    again after a restart, hands over the same proposal, and finds it done.
    """
    wanted = digest(proposal)
    for one in revisions or ():
        if one.get("point") == proposal.get("point") and one.get("proposal_digest") == wanted:
            return one
    return None


def tombstones(revisions):
    """Every step id a revision took out. None may be used again in the run:
    receipts, artifacts and operation keys are all filed under a step's id."""
    found = set()
    for one in revisions or ():
        found.update(one.get("remove") or ())
    return found


# -- checking -------------------------------------------------------------------
def validate(template, revisions, proposal, statuses, registry=None):
    """``(problems, effective document)`` for applying ``proposal`` now.

    ``statuses`` is ``{step id: status}`` for the run as it stands. Nothing
    changes whatever the answer; an empty list of problems is what permits the
    caller to record the revision. Every problem is named, not only the first.
    """
    current = effective(template, revisions)
    found = []
    if not isinstance(proposal, dict):
        return ["the proposal is not a mapping"], current
    if proposal.get("schema") != SCHEMA:
        found.append("the proposal says schema %r; this takes %r."
                     % (proposal.get("schema"), SCHEMA))
    base = proposal.get("base_revision")
    if base != len(revisions or ()):
        found.append("the proposal was made against revision %r, and the run is at "
                     "revision %d; it is stale." % (base, len(revisions or ())))

    point_id = str(proposal.get("point") or "")
    point = next((one for one in current.get("steps") or []
                  if one.get("id") == point_id), None)
    if point is None or point.get("plugin") != POINT:
        found.append("%r is not a revision point of this cycle." % point_id)
        return found, current
    policy = policy_of(point)
    found.extend(policy_problems(policy))

    earlier = [one for one in revisions or () if one.get("point") == point_id]
    if len(earlier) >= policy["max_revisions"]:
        found.append("%s has made %d revision(s), its max_revisions."
                     % (point_id, len(earlier)))

    add = [one for one in proposal.get("add") or [] if isinstance(one, dict)]
    remove = [str(one) for one in proposal.get("remove") or []]
    replace = [one for one in proposal.get("replace") or [] if isinstance(one, dict)]
    if len(add) + len(replace) > policy["max_steps"]:
        found.append("%d step(s) proposed; %s allows %d at a time."
                     % (len(add) + len(replace), point_id, policy["max_steps"]))
    if not (add or remove or replace):
        found.append("the proposal changes nothing.")

    existing = {one.get("id") for one in current.get("steps") or []}
    dead = tombstones(revisions)
    added_ids = [str(one.get("id") or "") for one in add]
    # Which steps this point added before - the only ones it may take back.
    owned = {str(step.get("id") if isinstance(step, dict) else step)
             for one in earlier for step in one.get("add") or ()}

    for step in add + replace:
        step_id = str(step.get("id") or "")
        plugin = str(step.get("plugin") or "")
        if plugin not in policy["allow"]:
            found.append("%s: %s is not a plugin %s allows (%s)."
                         % (step_id, plugin or "no plugin", point_id,
                            ", ".join(policy["allow"]) or "none"))
        found.extend("%s: %s" % (step_id, one) for one in _reads(step, current, policy,
                                                                 registry))
    for step_id in added_ids:
        if step_id in existing or step_id in dead:
            found.append("%s: that id is taken in this run%s." % (
                step_id, " by a step an earlier revision removed" if step_id in dead else ""))
        if added_ids.count(step_id) > 1:
            found.append("%s: proposed twice." % step_id)
    for step_id in remove + [str(one.get("id") or "") for one in replace]:
        if step_id not in owned:
            found.append("%s: only steps %s itself added may be removed or replaced."
                         % (step_id, point_id))
        elif (statuses or {}).get(step_id, "pending") != "pending":
            found.append("%s: it has already started (%s); what a step did stays "
                         "done - add a step after it instead."
                         % (step_id, statuses.get(step_id)))
    for step_id in policy["before"]:
        if (statuses or {}).get(step_id, "pending") != "pending":
            found.append("%s: %s waits for what is added, but it has already "
                         "started." % (point_id, step_id))

    # Future only: a new step waits for the point, for the steps the point
    # names, or for another new step - and through them, for the point.
    allowed_needs = {point_id} | set(policy["attach_after"]) | set(added_ids)
    for step in add + replace:
        needs = step.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        outside = sorted(set(needs) - allowed_needs)
        if outside:
            found.append("%s: may wait only for %s, not %s."
                         % (step.get("id"), ", ".join(sorted(allowed_needs)),
                            ", ".join(outside)))
    by_id = {str(one.get("id") or ""): one for one in add}
    for step_id in added_ids:
        if not _reaches(step_id, point_id, by_id):
            found.append("%s: does not wait for %s, so it could start before the "
                         "change was agreed." % (step_id, point_id))

    revision = {"point": point_id, "add": add, "remove": remove, "replace": replace,
                "before": list(policy["before"])}
    after = _apply(current, revision) if not found else current
    if len(after.get("steps") or []) > MAX_TOTAL:
        found.append("the run would have %d steps; the most is %d."
                     % (len(after["steps"]), MAX_TOTAL))
    if not found:
        try:
            parsed = model.parse_cycle(after, None)
        except model.CycleError as exc:
            found.append("after this change the cycle would not read: %s" % exc)
        else:
            found.extend("after this change: %s" % one
                         for one in model.problems(parsed, registry=registry, raw=after))
    return found, (after if not found else current)


def record(template, revisions, proposal, decision, registry=None):
    """The revision to store once ``proposal`` is accepted - see the design.

    Raises ValueError when it is not acceptable: recording is only ever done
    after :func:`validate` said nothing, and doing it anyway must not be quiet.
    """
    problems, after = validate(template, revisions, proposal, {}, registry)
    if problems:
        raise ValueError("; ".join(problems))
    from cycle import checkpoints
    parsed = model.parse_cycle(after, None)
    added = [str(one.get("id")) for one in proposal.get("add") or []]
    signatures = checkpoints.signatures(parsed, {}, registry) if registry else {}
    return {"revision": len(revisions or ()) + 1, "base": len(revisions or ()),
            "point": proposal.get("point"), "proposal_digest": digest(proposal),
            "decision": dict(decision or {}),
            "add": copy.deepcopy(proposal.get("add") or []),
            "remove": list(proposal.get("remove") or []),
            "replace": copy.deepcopy(proposal.get("replace") or []),
            "before": list(policy_of(next(one for one in after["steps"]
                                           if one.get("id") == proposal.get("point")))
                           ["before"]),
            "definitions": {one: signatures.get(one, "") for one in added}}


# -- running part of a grown run --------------------------------------------------
def partial(template, revisions, step_id, mode, source_run=""):
    """What a partial run of a grown run runs, and on which graph.

    ``mode`` is ``only`` - this step alone, the page's single chevron - or
    ``from`` - this step and everything after it, the double one. Decided on
    2026-09-28: both work on **any** step of the run on screen, including one a
    revision added, exactly as they do on a step the file has. So the partial
    run takes the source run's effective graph, and its revisions come with it.

    One exception, and it is the point of running a revision point again: a
    point that runs again decides its changes again. The revisions it made -
    and any revision made by a point one of those added - are not inherited,
    so the steps they added are not on the graph to be borrowed, and the point
    proposes against the graph as it stands without them.

    Returns ``{"document", "chosen", "revisions", "problems"}``: the graph to
    run, the steps to run (the rest are borrowed from the source run as any
    partial run borrows), and the revisions the new run starts with, each
    marked with the run it came from.
    """
    answer = {"document": effective(template, revisions), "chosen": [],
              "revisions": [], "problems": []}
    if mode not in ("only", "from"):
        answer["problems"].append("a partial run is only or from, not %r." % mode)
        return answer
    if step_id not in _ids(answer["document"]):
        answer["problems"].append("%s is not a step of this run." % step_id)
        return answer
    rerun = _chosen(answer["document"], step_id, mode)
    kept, document = [], copy.deepcopy(template or {})
    for revision in revisions or ():
        if revision.get("point") in rerun or revision.get("point") not in _ids(document):
            continue
        kept.append(dict(copy.deepcopy(revision), revision=len(kept) + 1,
                         base=len(kept), source_run=revision.get("source_run") or source_run))
        document = _apply(document, revision)
    answer.update(document=document, revisions=kept,
                  chosen=sorted(_chosen(document, step_id, mode)))
    return answer


def _ids(document):
    return {one.get("id") for one in document.get("steps") or []}


def _chosen(document, step_id, mode):
    """``step_id``, and for ``from`` every step that waits on it, however far."""
    found = {step_id}
    if mode == "only":
        return found
    changed = True
    while changed:
        changed = False
        for one in document.get("steps") or []:
            needs = one.get("needs") or []
            needs = [needs] if isinstance(needs, str) else needs
            if one.get("id") not in found and found & set(needs):
                found.add(one.get("id"))
                changed = True
    return found


def _reads(step, current, policy, registry):
    """What a proposed step reads that it may not: a secret, an undeclared output."""
    found = []
    secrets = {name for name, value in (current.get("variables") or {}).items()
               if isinstance(value, dict) and value.get("secret")}
    for path in variables.references(step.get("with") or {}):
        parts = path.split(".")
        if parts[0] == "vars" and len(parts) > 1 and parts[1] in secrets \
                and parts[1] not in policy["allow_secrets"]:
            found.append("reads the secret %r, which the point does not allow; "
                         "what a proposal reads ends up in prompts and reports."
                         % parts[1])
        if (parts[0] == "steps" and len(parts) > 3 and parts[2] == "outputs"
                and registry is not None):
            producer = next((one for one in current.get("steps") or []
                             if one.get("id") == parts[1]), None)
            plugin = registry.get((producer or {}).get("plugin", "")) if producer else None
            declared = {one.key for one in getattr(getattr(plugin, "metadata", None),
                                                   "outputs", ()) or ()}
            if declared and parts[3] not in declared:
                found.append("reads %s, which %s does not produce (it has %s)."
                             % (path, parts[1], ", ".join(sorted(declared))))
    return found


def _reaches(step_id, point_id, by_id, seen=()):
    step = by_id.get(step_id)
    if step is None:
        return False
    needs = step.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    if point_id in needs:
        return True
    return any(_reaches(need, point_id, by_id, seen + (step_id,))
               for need in needs if need in by_id and need not in seen)
