# Hello print

A minimal Python project: `main.py` prints one line and exits.
It needs no third-party dependencies.

```bash
python3 main.py
```

Expected output:

```text
Hello, QAVector!
```

The [hello_print_review cycle](../../cycles/hello_print_review.yaml), shown under
the **Demo** project in QAVector, asks CrewAI with OpenAI's `gpt-4o-mini` to review
this directory in Ukrainian and then writes an HTML report. The cycle does not
execute the program or tests. The model supports tools and structured output;
see the [OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-4o-mini).

For the first run, create the separate agent environment from the QAVector
repository root using Python 3.11 or 3.12:

```bash
python3.12 -m venv .venv-agent
.venv-agent/bin/python -m pip install -r requirements-agent-crewai.txt
```

Set `OPENAI_API_KEY` in the environment used to launch QAVector. For example,
in Bash, enter it without echoing it or saving it in shell history:

```bash
read -r -s -p 'OpenAI API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
```

Launch QAVector from that shell so the worker inherits the key. If it is already
running, restart it from that shell. Store the actual key in the environment,
not in the YAML or this project.

The cycle already sets `model: openai/gpt-4o-mini` and
`api_key_env: OPENAI_API_KEY`. Its absolute paths point to this checkout; adjust
`project_dir` and `agent_python` if the checkout or environment is moved. See
[agent environment setup](../../docs/cycle-agents.md#agent-environment) for other
platforms and environments.

In the GUI, open **Cycles → Demo → Hello print — agent review**. Edit variables
in the YAML tab if needed. The agent's structured result is saved as
`steps/review/review.json` in the run directory; the HTML report is under
`reports/run.html`.

This example and cycle are prepared for implementation review. Dependencies
have not been installed, and the program, tests and cloud review have not been
run.
