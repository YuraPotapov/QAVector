"""The built-in plugins, as one table.

This tuple is the whole list. Adding a plugin is writing the class and adding
its name here - nothing scans a directory, nothing registers itself at import
time, and ``grep BUILTIN`` answers "what can a step do?" completely. See
``cycle/registry.py`` for why that is the shape, and for the seam an external
plugin uses instead.

Imports are at module scope here rather than inside the tuple because this
module is itself imported lazily, by ``registry._builtins`` - so the cost of
pulling in a plugin's dependencies is paid the first time a plugin is looked up,
and not by ``--describe`` merely listing cycle files.
"""

from cycle.plugins.approval import ApprovalGate
from cycle.plugins.check import CheckGate
from cycle.plugins.command import ShellCommand
from cycle.plugins.git import GitCheckout
from cycle.plugins.git_commit import GitCommit
from cycle.plugins.git_prepare_branch import GitPrepareBranch
from cycle.plugins.jira import JiraIssues
from cycle.plugins.jira_transition import JiraTransition
from cycle.plugins.memory import MemoryClaim, MemoryRecall, MemoryRemember
from cycle.plugins.agent import AgentReview
from cycle.plugins.agent_edit import AgentEdit
from cycle.plugins.agent_implement import AgentImplement
from cycle.plugins.report import ReportHtml, ReportJson
from cycle.plugins.scenario import ScenarioRun
from cycle.plugins.wait import Wait
from cycle.plugins.service import (ServiceRestart, ServiceStart, ServiceStop,
                                   ServiceWait)

#: Every plugin that ships with the application, in the order a menu should
#: offer them: the general ones first, the ones that reach into the rest of the
#: application after, and the ones that write something out last.
BUILTIN = (
    ShellCommand(),
    Wait(),
    CheckGate(),
    GitCheckout(),
    GitPrepareBranch(),
    GitCommit(),
    ServiceStart(),
    ServiceStop(),
    ServiceRestart(),
    ServiceWait(),
    ScenarioRun(),
    JiraIssues(),
    JiraTransition(),
    MemoryRecall(),
    MemoryRemember(),
    MemoryClaim(),
    ApprovalGate(),
    AgentReview(),
    AgentEdit(),
    AgentImplement(),
    ReportJson(),
    ReportHtml(),
)
