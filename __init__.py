"""Agent companion: dashboard metadata and local operator CLI, without runtime hooks."""

import importlib.util
from pathlib import Path

__version__ = "0.3.7"


def register(ctx):
    """Use Hermes' supported CLI API; register no model tools or agent behavior."""
    spec = importlib.util.spec_from_file_location(
        'agent_companion_operator_cli', Path(__file__).with_name('operator_cli.py'))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    ctx.register_cli_command(name='agent-sharing', help='Manage Agent AI recipient declarations',
                             setup_fn=cli.register_cli, handler_fn=cli.command)
