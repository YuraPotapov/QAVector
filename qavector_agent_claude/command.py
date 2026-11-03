"""Claude command construction shared by legacy calls and SDK workers."""

def command(binary, prompt, tools, mode="", model="", max_turns=None, add_dir="",
          effort="", disallowed=()):
    """The command line for one call. Built in one place, because the prompt
    with and without a person's messages must differ in nothing else.

    A rule with a space in it - ``Bash(git merge:*)`` - is its own argument:
    joined into one string with the others, the CLI splits it at the space.
    Tools without one are still joined, so the command line a step recorded
    before rules existed is the one it is asked about on Resume.
    """
    argv = [binary, "-p", prompt,
            # stream-json rather than json: the same answer arrives at the end,
            # but every turn is announced as it happens, so the step can show
            # what it is doing instead of going quiet and then printing a wall
            # of JSON. --verbose is what makes the CLI emit the turns at all.
            "--output-format", "stream-json", "--verbose"]
    argv += tool_list("--allowedTools", tools)
    if disallowed:
        argv += tool_list("--disallowedTools", disallowed)
    if max_turns not in (None, ""):
        argv += ["--max-turns", str(int(max_turns))]
    if mode:
        argv += ["--permission-mode", mode]
    model = str(model or "").strip()
    if model:
        argv += ["--model", model]
    effort = str(effort or "").strip()
    if effort:
        argv += ["--effort", effort]
    if add_dir:
        argv += ["--add-dir", add_dir]
    return argv


def tool_list(flag, tools):
    tools = list(tools)
    if any(" " in one for one in tools):
        return [flag] + tools
    return [flag, " ".join(tools)]


def gateway_command(binary, model):
    """Deployment-owned read-only invocation; the prompt is written to stdin."""
    return [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--restricted", "--safe-mode", "--setting-sources", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}',
            "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob", "--no-session-persistence"]


