"""Write the workflow diagram from the compiled LangGraph graph: `python -m tools.draw_graph`.

The diagram in docs/graph.md is generated, never drawn by hand; tests/test_graph.py fails if it is out of date.
"""
from pathlib import Path

from pipeline.graph import Deps, build_graph
from pipeline.store import MemoryStore

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "graph.md"


def render() -> str:
    app = build_graph(Deps(scrape=lambda url: None, extract=lambda text, record, feedback: {}, store=MemoryStore(), review=None))
    mermaid = app.get_graph().draw_mermaid()
    return ("# Workflow graph\n\n"
            "Generated from the compiled LangGraph graph in `pipeline/graph.py` by `python -m tools.draw_graph`.\n"
            "Solid arrows always run; dotted arrows are the routing decisions.\n\n"
            "```mermaid\n" + mermaid.strip() + "\n```\n")


if __name__ == "__main__":
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT}")
