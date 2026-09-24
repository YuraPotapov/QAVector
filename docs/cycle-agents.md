# Git and agent plugins

`git.checkout` prepares source code for a run. `agent.review` reviews that code,
structured inputs and selected text artifacts. Both are ordinary registry
plugins; the executor has no Git, framework or model-provider branches.

`agent.review` has three backends, and **the default one needs no API key**:

| `framework` | how it authenticates | what it needs installed |
|---|---|---|
| `claude_cli` (default) | the Claude Code CLI's own browser sign-in | Claude Code |
| `crewai` | an API key in an environment variable | a Python environment with CrewAI |
| `autogen` | an API key in an environment variable | a Python environment with AutoGen |

## Signing in without an API key

Making an API key means opening a web console, creating a key, deciding where
to keep it and exporting it where the right process will see it. That is a
developer's errand, and it used to be the only way to get an agent to read a
failing test.

`framework: claude_cli` removes it. The plugin runs the Claude Code CLI, which
signs in through a browser against a Claude subscription or a Console account
and keeps the credentials to itself:

```bash
claude auth login          # opens the browser; nothing to copy or paste
claude auth status         # who it is signed in as
```

**No token passes through this application.** Nothing here reads one, stores
one or writes one down - the binary is invoked and it knows who it is. A cycle
file that uses it carries no secret and is safe to commit.

On the **Cycles** page, select an `agent.review` step and the Inspector shows a
**Setup** section with *Check* and *Sign in…*. Those are not built into the
page: a plugin declares what it can be asked outside a run
(`metadata.actions`), `--describe` publishes it, and the Inspector offers
whatever it finds. That is deliberate - a plugin is meant to be replaceable
without touching the application, and a plugin whose setup lived in the
application's Settings would not be. The same mechanism is available from a
terminal:

```bash
python3 session_launcher.py --cycle-plugin-action=agent.review:sign_in_status
```

A step whose backend is not signed in **fails before anything starts**, naming
the one command that fixes it, rather than failing somewhere inside the binary
with a message written for a terminal.

### What the CLI backend is allowed to do

A review reads. The step runs the CLI with `--allowedTools Read Glob Grep` and
nothing else, so a step asked to review something cannot change it, run a
command, or reach the network on its own account. `--max-turns` is passed only
when the step sets `max_iterations`; left out, the agent has no turn limit and
the step's `timeout` bounds it instead. A turn count is a poor sign of an agent
being stuck, and an attempt cut off by one stops halfway with its edits applied
and unchecked.

```yaml
- id: review
  plugin: agent.review
  needs: [checkout, tests]
  with:
    framework: claude_cli
    model: sonnet              # an alias: opus, sonnet, fable
    # effort: high             # low, medium, high, xhigh, max
    # claude: /path/to/claude  # blank finds it on PATH
    repository: "${steps.checkout.outputs.workspace}"
    task: >
      Review the changes for security problems. Be specific about file and line.
    inputs:
      tests: "${steps.tests.outputs.stdout_tail}"
```

It produces the same review document as the other two backends - `summary`,
`risk`, `issues`, `recommendations`, `report_path` - plus `cost_usd` when the
CLI reports it, so a cycle can change backend without anything downstream
noticing.

### How hard it is asked to think

`effort` becomes `--effort` and is one of `low`, `medium`, `high`, `xhigh` and
`max`. Left blank - which is the default, and what every cycle written before
the field existed does - the CLI uses whatever it is configured to use.

The level is checked before the step starts. The CLI itself warns about a level
it does not know and carries on at its default, which would leave a cycle file
claiming an effort the run never used, so `agent.review`, `agent.edit` and
`agent.implement` refuse it instead. It belongs to the `claude_cli` backend
only; CrewAI and AutoGen have no equivalent and say so rather than ignoring it.

In `agent.implement` the same level covers every attempt **and** the review
that signs them off: a reviewer asked to think less than the writer did is not
a check on it.

## Git checkout

Git must be installed and available on the core process's PATH.

```yaml
- id: checkout
  plugin: git.checkout
  timeout: 180
  retry: {attempts: 2, delay: 3}
  with:
    repository: "${vars.repository}"
    branch: main
    # commit: <commit SHA>     # optional; produces a detached HEAD
    # path: source/project    # default: source/<step id>
    # depth: 1                # default: 0, full history
    # submodules: true        # default: false
```

The destination must be a new directory inside the run workspace. The plugin
clones into a temporary sibling and moves it to the destination after all Git
operations succeed. A failed checkout removes only its temporary tree, allowing
the engine to retry. Existing directories are refused. Local clones copy Git
objects rather than hard-linking them to the source; they include committed
content, not uncommitted changes. See the [Git clone options](https://git-scm.com/docs/git-clone).

Authentication uses the normal Git credential helper or SSH setup. Interactive
Git prompts are disabled; set a step timeout for unavailable remotes or SSH
authentication. A shallow clone may not include an older requested commit.

Outputs are `workspace` (relative to the run directory), `commit` (full SHA) and
`branch` (the branch name, or `HEAD` when detached). Each Git invocation retains
its own stdout/stderr logs, including across retries.

## Agent environment

Create a separate Python 3.11 or 3.12 environment and install the chosen
framework. These commands are setup instructions, not actions the plugin runs:

```bash
python3.12 -m venv /path/to/agent-env
/path/to/agent-env/bin/python -m pip install -r requirements-agent-crewai.txt
# Or use requirements-agent-autogen.txt for AutoGen.
```

On Windows, use the environment's `Scripts/python.exe`. Set the `python` field
to that interpreter's absolute path. The core's own Python environment and GUI
do not need framework packages. Packaged builds ship the worker source and use
the same external interpreter.
The worker uses Python isolated mode, so provider packages must be installed in
that environment rather than injected through `PYTHONPATH`.

Choose the model and provider in the cycle:

- **CrewAI:** `model` is the provider/model identifier; `model_options` are
  forwarded to `crewai.LLM`. The optional requirements include LiteLLM for
  providers such as Ollama. See [CrewAI model configuration](https://docs.crewai.com/en/concepts/llms).
- **AutoGen:** also set `model_client` to a `ChatCompletionClient` component
  class. `model` and `model_options` configure that component. The provided
  requirements include the Ollama client; install the matching `autogen-ext`
  extra for another provider. See [AutoGen model clients](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/models.html).

If the provider requires a key, set `api_key_env` to the name of an environment
variable inherited by QAVector. The worker reads the value from its environment;
the request file stores only the variable name. Do not put keys into the YAML or
`model_options`. Provider-specific authentication can also use its normal
environment configuration.

## A Git → review → report cycle

The bundled [demo_git_review.yaml](../cycles/demo_git_review.yaml) contains this
workflow. Set its variables to your repository, interpreter and installed model
before running it. It does not launch tests or modify the repository.

```yaml
id: demo_git_review
name: Git and agent review
variables:
  repository: /path/to/your/repository
  agent_python: /path/to/agent-env/bin/python
  model: ollama/your-installed-model

steps:
  - id: checkout
    plugin: git.checkout
    timeout: 180
    with:
      repository: "${vars.repository}"

  - id: review
    plugin: agent.review
    needs: [checkout]
    timeout: 300
    with:
      framework: crewai
      python: "${vars.agent_python}"
      model: "${vars.model}"
      model_options:
        base_url: http://localhost:11434
      repository: "${steps.checkout.outputs.workspace}"
      task: Review the repository for correctness and maintainability. Cite files and lines.
      inputs:
        commit: "${steps.checkout.outputs.commit}"

  - id: report
    plugin: report.html
    needs: [review]
```

For AutoGen, replace the review's model settings with:

```yaml
framework: autogen
model_client: autogen_ext.models.ollama.OllamaChatCompletionClient
model: your-installed-model
model_options:
  host: http://localhost:11434
```

The selected model must support tools and structured output. Model availability
and quality depend on the configured provider. The adapters do not install or
download models.

## Letting an agent change files

`agent.review` reads and only reads: its tools are `Read`, `Glob` and `Grep`,
so a step asked to review something cannot alter it. When you want the changes
made rather than described, that is a different step — **`agent.edit`**.

It is a separate plugin rather than a switch on the review on purpose. A setting
would mean every `agent.review` step in every cycle changes meaning depending on
one line further down, and two steps doing very different things would read the
same at a glance. The cycle file says which it is:

```yaml
  - id: fix
    plugin: agent.edit
    needs: [checkout]
    timeout: 900
    with:
      model: sonnet
      # effort: high
      directory: "${steps.checkout.outputs.workspace}"
      task: >-
        Replace the deprecated logging calls in src/ with the new API.
        Change nothing else.
```

### What it may and may not do

| | |
|---|---|
| Tools | `Read`, `Glob`, `Grep`, `Edit`, `Write`, `MultiEdit` |
| Permission mode | `acceptEdits` — edits are applied as they are made |
| Shell | **never** — `Bash` is not among its tools |

**Edits are applied, not proposed.** Nobody is at a terminal during a run, so a
step that waited to be asked would hang. That is the bargain, and the reason the
plugin is called `edit`. Point it at a checkout you can throw away or one you
can diff afterwards.

**It cannot run commands.** A step that needs to run something has
`command.shell`, which says so in the cycle file and is covered by the same
timeout and Stop. An editing agent with a shell would make "what did this step
do" unanswerable from the file.

`permissions` in a plugin's metadata does **not** restrict anything — it records
what a plugin says it needs, against the day a sandbox exists. The tool list is
what actually limits an agent today.

### What it reports

`files_changed` is every path it wrote, once each, relative to the directory —
read off the CLI's own event stream as the writes happen, not by diffing
afterwards. A step that fails halfway still reports what it changed by then,
because it really did change it.

```yaml
  - id: show
    plugin: command.shell
    needs: [fix]
    with:
      dir: "${steps.checkout.outputs.workspace}"
      command: git --no-pager diff
```

That pairing — checkout, edit, diff — is the arrangement this is built for: the
work is done in a tree you control and the diff is there to read before anything
of it is kept.

### A review-and-fix cycle

`cycles/review_and_fix.yaml` is the whole idea in one file, and it needs no API
key and no setting up:

```
prepare ──► review ──► fix ──┬──► diff ──┐
                             └──► verify ─┴──► report
```

`prepare` writes a small module with one real defect and commits it, so the diff
at the end is against something. `review` finds the defect. `fix` is handed the
review's own `issues` and `recommendations` as inputs — that is the reason the
two are separate steps rather than one prompt: what one concluded is an input to
the other rather than something written out twice. `diff` and `verify` need only
the fix, so they run at the same time.

`verify` is the step that matters. An agent that "fixed" something by changing
what already worked has not fixed it, so the check asserts the ordinary case is
untouched and that the edge case no longer crashes — accepting either of the two
fair answers, a value or a raised `ValueError`. It carries `on_failure:
continue` so that a failed verification still leaves a report behind, which is
the run you most want to read.

Two agent steps cost real money — a few cents at sonnet. A bigger model and a
higher `effort` both spend more: raise them on the steps whose answer the rest
of the run is built on, not on every step at once.

## Watching a review happen

A review takes as long as it takes, and the `claude_cli` backend says what it is
doing while it does it. It runs the CLI with `--output-format stream-json`, so
every turn arrives as it happens and becomes a row in the **Stages** tab beside
the output:

```
o  Started - claude-sonnet-4-6
|
o  Thinking          what it is weighing
|
o  Read main.py      file_path: /repo/src/main.py
|
o  Returned          the first lines of what came back
|
o  Answered          what it concluded
|
o  Finished          2 turns, $0.0428
```

With nothing selected on the canvas the tab shows every step's stages, labelled
by step; selecting a node narrows it to that one. The Output tab beside it works
the same way, and the same rule holds: nothing selected means the whole run.

The raw event stream is **not** printed into the output - it is JSON, and one
line of it can be tens of kilobytes. All of it is still written to the step's
`stdout.log` in the run directory, which is where to look when a row is not
enough. Stages are a general mechanism (`cycle.step.stage`), not an agent one:
any plugin that emits them gets these rows with nothing added to the interface.

## Inputs, tools and results

`task` is the review instruction. `inputs` accepts structured values, including
`${steps.<id>.outputs.<key>}` references. `files` accepts a YAML list of text
artifact paths within the run workspace, such as a previous command's
`stdout_path`. File contents are limited to 128 KiB each and a 512 KiB total
budget; included truncation is marked in the request. A repository is optional.

When one is supplied, the agent has `list_files` and `read_file` tools limited
to that repository. Reads reject traversal and symlinks escaping it. No shell
or file-writing tools are supplied. These are tool-level restrictions, not an
OS sandbox for the framework or provider packages.

For these two, `max_iterations` defaults to 12 when left out (CrewAI agent
iterations / AutoGen tool iterations). The engine's `timeout`, retry and Stop controls apply to the worker
process. A retry starts a fresh agent; it does not resume an earlier conversation.

Both adapters return the same validated outputs:

```json
{
  "summary": "What was reviewed and the conclusion",
  "issues": [
    {"severity": "high", "description": "Concrete finding", "file": "src/app.py", "line": 42}
  ],
  "recommendations": ["Suggested next action"],
  "risk": "high"
}
```

Risk is `low`, `medium`, `high` or `unknown`; issue severity excludes `unknown`.
`file` may be empty and `line` may be null when no source location applies.
The plugin adds `report_path`, pointing to the `review.json` artifact. A provider
failure, missing dependency or invalid structured response fails the step. A
successfully produced high-risk review is still a successful step: use its
outputs in a following condition if the workflow should react to risk.

The existing `report.html`/`report.json` plugins still capture the run at their
execution time; the final-report limitation in the implementation review remains.

## Framework assessment

These are integration choices based on source and documentation review, not
benchmarks or a completed live-provider proof of concept.

| Option | Fit for a QAVector plugin | What this implementation does |
| --- | --- | --- |
| CrewAI | Agent/Task/Crew primitives, typed task output and callbacks fit a blocking worker. Framework dependencies and its Python version are isolated from Qt. | One reviewer with bounded read tools and Pydantic output; optional install. |
| Microsoft AutoGen | Async agents, typed messages and `run_stream` map to an isolated worker and step events. Team orchestration can be added behind this boundary. | One AssistantAgent, a configurable model-client component and an event loop confined to the worker. |
| Direct provider SDK/API | Smaller dependency set; tools, retries and conversation state would need a separate implementation for the selected API. | No direct provider implementation; model configuration stays in the framework adapters. |
| MCP integration | Useful as a tool/server boundary, but does not itself supply the review model and agent loop. | No MCP servers are connected in this first version. |
| External CLI agent | Naturally isolated and cancellable; capabilities and result formats depend on the CLI. | Not selected: the requested first implementation uses frameworks. |
| Local runtime/model | Can keep inference local, subject to model support for tools and structured output. | Model clients can target a local provider such as Ollama. |
| Custom lightweight runtime | Maximum control over dependencies, but QAVector would own the agent loop and provider compatibility. | Deferred; the core continues to orchestrate ordinary plugins. |

CrewAI's structured task outputs and callbacks are documented in
[Tasks](https://docs.crewai.com/en/concepts/tasks) and
[Crews](https://docs.crewai.com/en/concepts/crews). AutoGen documents typed
responses, tool iterations and streaming in
[Agents](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/agents.html).
Its maintainers currently describe AutoGen as being in maintenance mode;
the adapter remains optional. See the [official AutoGen repository](https://github.com/microsoft/autogen).

The CrewAI source uses the MIT license, and AutoGen's Python package declares
MIT for code. Their notices need to accompany redistributed copies; model,
provider and transitive dependency terms are separate. See
[CrewAI's license](https://raw.githubusercontent.com/crewAIInc/crewAI/main/LICENSE)
and [AutoGen's package metadata](https://github.com/microsoft/autogen/blob/main/python/packages/autogen-agentchat/pyproject.toml).

Multi-agent teams, MCP tools, framework checkpoint/resume, token accounting and
OS permission enforcement are not exposed by this first plugin. Only progress
event types and tool actions are logged; complete framework traces and private
model reasoning are not added to the event bus. Neither framework becomes a
dependency of the executor. Packaging weight stays in the user-managed agent
environment. Framework/provider licensing and redistribution need to be reviewed
if those dependencies are later bundled with the application.
