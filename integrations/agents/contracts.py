"""Provider-neutral contracts. Missing features are refused, never approximated."""

from dataclasses import asdict, dataclass
from typing import Protocol


PATCH_CHARS = 256 * 1024


class Unsupported(ValueError):
    code = "unsupported"


def rate_limit_window(label, used_percent, resets_at=None, *, duration_minutes=None, account_wide=False):
    """Normalize a provider's percentage and optional Unix reset time."""
    if type(used_percent) not in (float, int) or not 0 <= used_percent < float("inf"):
        return None
    if type(resets_at) not in (float, int) or not 0 < resets_at < float("inf"):
        resets_at = None
    window = {"label": label, "used_percent": used_percent, "resets_at": resets_at}
    if type(duration_minutes) is int and duration_minutes > 0:
        window.update(duration_minutes=duration_minutes, account_wide=account_wide)
    return window


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    modes: tuple
    resume: bool = False
    selected_model: bool = False
    effort: bool = False
    cancel: bool = False
    usage: bool = False
    qavector: bool = False
    planning: bool = False
    skills: bool = False
    image_input: bool = False
    fork_session: bool = False
    interactive_approvals: bool = False
    assigns_session_id: bool = False
    compact: bool = False
    compact_hint: bool = False
    cost_scope: str = "unknown"

    def document(self):
        result = asdict(self)
        result["modes"] = list(self.modes)
        return result

    def require(self, feature):
        if not getattr(self, feature, False):
            raise Unsupported("%s does not support %s." % (self.provider, feature))


class AgentProviderAdapter(Protocol):
    """A CLI provider; process lifetime belongs to the shared runner.

    The public Engine supplies async handles/stream/cancel. Providers only own
    command construction and event interpretation, not another execution loop.
    """
    KIND: str
    NAME: str
    MODES: tuple
    EFFORTS: tuple

    def capabilities(self) -> ProviderCapabilities: ...
    def resolve(self, binary=None) -> str: ...
    def turn_argv(self, binary, mode="read", model="", effort="", session="", resume=False,
                  skills_dir="", attachments_dir="", plan=False, qavector=None) -> list: ...
    def turn_env(self, env) -> dict: ...
    def events(self, line) -> list: ...
    def stage_skills(self, where, skills) -> str: ...


@dataclass(frozen=True)
class ModelRef:
    provider: str
    id: str = ""                 # empty means the provider's default, not a guessed model


@dataclass(frozen=True)
class SessionRef:
    id: str
    connection_id: str
    provider: str
    provider_session_id: str = ""


@dataclass(frozen=True)
class ConnectionRef:
    id: str
    provider: str


@dataclass(frozen=True)
class AgentTask:
    id: str
    agent_id: str
    project_id: str
    execution_id: str
    state: str
    session_id: str = ""


@dataclass(frozen=True)
class AgentProfile:
    """A logical agent bound by its owner to a connection and a workspace.

    A task may select a session of that connection, but cannot change the
    workspace, mode or restrictions. Broader settings need a new owner profile.
    """
    id: str
    name: str
    connection_id: str
    project_id: str
    folder: str
    mode: str = "read"
    model: str = ""
    effort: str = ""
    restrictions: object = None


def normalized_event(event, provider, session_id, task_id):
    """Add the shared envelope without altering the legacy chat record."""
    names = {"started": "agent.started", "text": "message.delta",
             "thinking": "agent.thinking", "completed": "agent.completed",
             "failed": "agent.failed", "stopped": "agent.canceled"}
    kind = event.get("type", "unknown")
    return {"type": names.get(kind, kind), "provider": provider,
            "session_id": session_id, "task_id": task_id,
            "data": {key: value for key, value in event.items() if key != "type"}}
