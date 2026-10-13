"""Execution-plan tree model.

The compiler flattens a scenario's ``use:`` nesting into a flat list of
:class:`~domain.flow.Step` for the runner. For the execution overlay we also
want the *hierarchy* back - the scenario, each reusable block it pulls in, and
the individual actions/assertions - so the HUD can draw it as a tree.

A :class:`PlanNode` is one node of that tree. Group nodes mirror a scenario or a
``use:``-d block; leaf nodes are the individual steps. Each leaf carries
``step_index``, its position in the flat list, so the runner's ``enumerate``
index maps 1:1 onto a tree node with no bookkeeping on the runner side.
"""

from dataclasses import dataclass, field

GROUP = "group"
STEP = "step"


@dataclass
class PlanNode:
    """One node of the execution tree.

    ``id`` is a stable path-based id (``"0"``, ``"0/1"``, ``"0/1/0"``) so the
    injected renderer can diff by key across pushes. Group nodes have
    ``children``; leaf (``STEP``) nodes carry ``step_index`` / ``action`` /
    ``target`` (the last is a human-friendly label, not the resolved CSS).
    """

    id: str
    label: str
    kind: str = GROUP                       # GROUP | STEP
    children: list = field(default_factory=list)   # list[PlanNode]
    step_index: int = None                  # leaf only: index into the flat step list
    action: str = None                      # leaf only
    target: str = None                      # leaf only: friendly label (named selector / value)
    description: str = None                 # group only: the flow's own description

    def to_dict(self):
        """Plain nested dict for JSON serialization to the overlay renderer."""
        node = {"id": self.id, "label": self.label, "kind": self.kind}
        if self.kind == STEP:
            node["step_index"] = self.step_index
        else:
            node["children"] = [child.to_dict() for child in self.children]
        return node

    def leaves(self):
        """Yield the leaf (STEP) nodes in tree (== flat step) order."""
        if self.kind == STEP:
            yield self
            return
        for child in self.children:
            yield from child.leaves()
