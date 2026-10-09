from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from eks_harness.score import Score, beats
from eks_harness.score.render import ScoreRenderError, dependency_order, render_score

SIZE = 32
FPS = 10


def _ok() -> bool:
    try:
        from eks_harness.video.plugins.builtin.media.web_scene import runner

        runner.require_playwright()
        runner.runtime_script()
        return runner.browser_version() != "unknown" and shutil.which("ffmpeg") is not None
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not _ok(), reason="needs Playwright Chromium, the scene runtime and ffmpeg")

CARD = """<!doctype html><html><head><style>
html,body{margin:0;width:32px;height:32px;background:rgb(255,0,0)}</style></head><body><script>
let sent = false;
requestAnimationFrame(function tick(t) {
  if (!sent && t >= 500) { sent = true; ehx.emit("half", { at: ehx.frame }); }
  requestAnimationFrame(tick);
});
ehx.on("pulse", () => { document.body.dataset.pulses = String(Number(document.body.dataset.pulses || 0) + 1); });
</script></body></html>"""

BADGE = """<!doctype html><html><head><style>
html,body{margin:0;width:32px;height:32px}
#b{position:absolute;left:0;top:0;width:32px;height:16px;background:rgb(0,255,0)}
#tex{position:absolute;left:0;top:16px;width:32px;height:16px}</style></head><body>
<div id="b"></div><img id="tex"><script>
ehx.texture("card", document.getElementById("tex"));
ehx.on("show", () => { document.getElementById("b").style.background = "rgb(0,0,255)"; });
</script></body></html>"""


def _frames(path: Path, size: int = SIZE) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, size, size, 3)


def build(tmp_path: Path) -> Score:
    (tmp_path / "card.html").write_text(CARD, encoding="utf-8")
    (tmp_path / "badge.html").write_text(BADGE, encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    str(tmp_path / "tone.wav")], check=True)
    s = Score(fps=FPS, bpm=120, duration=1.0, size=(SIZE, SIZE), song="tone.wav", window=(0.5, 1.5))
    card = s.web("card.html", id="card", transparent=False)
    badge = s.web("badge.html", id="badge", textures={"card": card}, transparent=False)
    s.on(card.event("half"), badge.emit("show"), id="show")
    s.on(beats()[0:2], card.emit("pulse"), id="pulse")
    s.edit.layer(card).layer(badge)
    return s


def test_dependency_order_and_cycles(tmp_path: Path) -> None:
    s = Score(fps=FPS, duration=1.0)
    a = s.web("a.html", id="a")
    b = s.web("b.html", id="b", textures={"a": a})
    s.blender("c.py", id="c", textures={"b": b})
    assert [t.id for t in dependency_order(s.build())] == ["a", "b", "c"]
    s.ir.tracks[0].textures = {"c": "c"}
    with pytest.raises(ScoreRenderError, match="cycle"):
        dependency_order(s.build())


def test_device_tracks_need_a_take(tmp_path: Path) -> None:
    s = Score(fps=FPS, duration=1.0)
    s.device("ios", flow="x.py")
    with pytest.raises(ScoreRenderError, match="take runner"):
        render_score(s.build(), tmp_path, None)


@needs_browser
def test_score_renders_scenes_chains_events_and_composites(tmp_path: Path) -> None:
    score = build(tmp_path).build()
    result = render_score(score, tmp_path, tmp_path / "out.mp4", workspace=tmp_path / "ws")
    names = {(e.source, e.name) for e in result.events}
    assert ("card", "half") in names
    show = next(a for a in result.plan.actions if a.rule == "show")
    assert show.target == "badge" and show.frame == 5
    assert result.iterations == 2 and not [w for w in result.warnings if "settle" in w]
    badge = _frames(result.clips["badge"])
    assert badge[3, 4, 4, 1] > 200 and badge[6, 4, 4, 2] > 200
    assert badge[2, 24, 4, 0] > 200
    assert result.output is not None and result.output.exists()
    final = _frames(result.output)
    assert final.shape[0] == FPS
    assert final[8, 4, 4, 2] > 150
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "csv=p=0", str(result.output)],
                           capture_output=True, text=True).stdout
    assert "audio" in probe
