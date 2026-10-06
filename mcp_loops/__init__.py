"""mcp-loops: define + run structured multi-agent LOOPS on the swarm.

A *loop* is a small team of agents that run one after another, over and over,
until a shared turn budget is spent or the manager calls it done. Three roles:

- ``manager``        — the north-star holder. Exactly one per loop. Keeps a
                       lean freeform status (done / to-do), turns input-provider
                       findings into context + tasks for workers, is the final
                       decision-maker, and is the only agent with a line to the
                       owner (pre-start briefing + mid-run asks, both via Telegram).
- ``worker``         — does the work from the manager's structured tasks.
- ``input_provider`` — tester/critic. Actively probes the product (UI, API, logs,
                       db — free to add its own tools like Playwright) and ends
                       its turn with a satisfaction status.

The config is authored conversationally (owner ↔ coordinator), emitted as a JSON
file + a prettified HTML block-scheme (this package renders both), reviewed, then
run by name. The runner (``mcp_loops.runner``, part (b)) executes each agent as a
headless Claude session via the worker daemon — which gives the 200k/400k context
caps and compact-before-next-turn for free.

This module (part (a)) is engine-agnostic: schema + validator + renderer + CLI.
Nothing here spawns a session; the runner imports :func:`validate_config` /
:func:`summarize` from :mod:`mcp_loops.schema`.
"""

from mcp_loops._version import __version__, STATE_VERSION  # noqa: F401
from mcp_loops.schema import validate_config, summarize, LoopConfigError  # noqa: F401
