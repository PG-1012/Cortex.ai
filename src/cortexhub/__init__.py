"""CortexHub: a local-first shared memory engine for Claude and Muse on an Obsidian vault."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as _main

    _main()
