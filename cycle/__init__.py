"""The Cycle engine: orchestration over the things this application already does.

A cycle is a graph of steps. Each step names a *plugin* and hands it a mapping;
the engine knows nothing about what any plugin does. Starting a service, running
a scenario, running a shell command and writing a report are all plugins, and so
is anything added later - the engine's job is deciding what may run, when, and
what to do when something fails.

Layering, the same shape ``engine/`` uses:

    model      YAML mapping -> Cycle; validation; ring detection; graph payload
    loader     finding cycle files across the search path
    cyclefile  reading, rendering, writing and deleting them
    registry   the plugin interface, the metadata that describes one, the table
    plugins/   the built-in plugins
    variables  resolving ${...} against a run's scope
    conditions the `if:` grammar
    context    what a plugin is handed: the run, the workspace, the cancel token
    executor   the parallel DAG scheduler
    workspace  where a run's files go
    run        the record of a run, and its persistence
    bus        the observer protocol, mirrored onto the JSONL event stream

Three rules this package holds to, because each of them is load-bearing:

* **Nothing here knows about Odoo, browsers, agents or Qt.** Anything specific
  is a plugin, and plugins reach the rest of the application through adapters
  rather than reimplementing it.
* **It imports with the standard library alone.** pyyaml is imported lazily
  inside the two modules that parse files, so ``--describe`` and a plain launch
  do not pay for it, exactly as ``engine/loader.py`` already arranges.
* **The core must be usable without the GUI.** A cycle runs from a terminal.
  Steps that genuinely need the GUI - the service adapters, which ask it to
  start something - say so immediately rather than waiting out a timeout.
"""
