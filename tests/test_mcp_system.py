from __future__ import annotations

import asyncio
from pathlib import Path

from eks_harness.cli.client import HarnessClient
from eks_harness.mcp_server import create_server


def test_mcp_server_lists_all_tool_groups(tmp_path: Path) -> None:
    server, tools = create_server(lambda: HarnessClient("http://127.0.0.1:9"))
    listed = asyncio.run(server.list_tools())
    names = {tool.name for tool in listed}
    for expected in ("list_projects", "list_plugins", "trust_plugin", "list_nodes", "submit_job", "choose_backend",
                     "seed_backend", "list_personas", "score_plan", "score_validate", "video_validate",
                     "video_render", "video_effects", "video_add_segment"):
        assert expected in names, expected
    resources = asyncio.run(server.list_resource_templates()) if hasattr(server, "list_resource_templates") else []
    assert resources is not None
    tools.close()


def test_score_tools_work_on_dsl_files(tmp_path: Path) -> None:
    score_py = tmp_path / "score.py"
    score_py.write_text(
        "from eks_harness.score import Score, beats\n"
        "score = Score(fps=30, bpm=120, duration=8)\n"
        "card = score.web('card.html')\n"
        "score.on(beats('downbeat')[1], card.emit('flip'))\n", encoding="utf-8")
    server, tools = create_server(lambda: HarnessClient("http://127.0.0.1:9"))
    result = asyncio.run(server.call_tool("score_plan", {"path": str(score_py)}))
    text = str(result)
    assert "flip" in text and "60" in text
    exported = asyncio.run(server.call_tool("score_export", {"path": str(score_py)}))
    assert (tmp_path / "score.json").is_file() and exported is not None
    tools.close()
