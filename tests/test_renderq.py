from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from eks_harness import renderq


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(renderq, "POLL_SECONDS", 0.02)


def test_one_render_at_a_time_in_order(paths, fast):
    order: list[str] = []
    started = threading.Event()

    def worker(name: str, hold: float) -> None:
        with renderq.render_slot("render", name):
            order.append(f"start {name}")
            started.set()
            time.sleep(hold)
            order.append(f"end {name}")

    first = threading.Thread(target=worker, args=("a", 0.3))
    first.start()
    started.wait(5)
    threads = []
    for name in ("b", "c"):
        thread = threading.Thread(target=worker, args=(name, 0.05))
        thread.start()
        threads.append(thread)
        time.sleep(0.1)
    status = renderq.status()
    assert status["running"] == 1 and status["waiting"] == 2
    assert [t["label"] for t in status["items"]] == ["a", "b", "c"]
    assert [t.get("position") for t in status["items"]] == [None, 1, 2]
    for thread in [first, *threads]:
        thread.join(10)
    assert order == ["start a", "end a", "start b", "end b", "start c", "end c"]
    assert renderq.status()["items"] == []


def test_nested_render_in_the_same_thread_does_not_wait(paths, fast):
    with renderq.render_slot("score", "outer") as outer:
        assert outer is not None
        with renderq.render_slot("render", "inner") as inner:
            assert inner is None
        assert renderq.status()["running"] == 1


def test_child_process_of_the_holder_passes(paths, fast, tmp_path):
    script = textwrap.dedent("""
        import json, sys
        from eks_harness import renderq
        with renderq.render_slot("render", "child") as ticket:
            print(json.dumps({"queued": ticket is not None}))
    """)
    with renderq.render_slot("score", "parent"):
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"queued": False}


def test_other_process_waits_for_the_slot(paths, fast, tmp_path):
    marker = tmp_path / "started"
    script = textwrap.dedent(f"""
        import pathlib, time
        from eks_harness import renderq
        renderq.POLL_SECONDS = 0.02
        with renderq.render_slot("encode", "other"):
            pathlib.Path({str(marker)!r}).write_text(str(time.time()))
    """)
    with renderq.render_slot("render", "first"):
        env = {k: v for k, v in os.environ.items() if k != renderq.HOLDER_ENV}
        proc = subprocess.Popen([sys.executable, "-c", script], env=env)
        deadline = time.time() + 20
        while time.time() < deadline and renderq.status()["waiting"] == 0:
            time.sleep(0.05)
        assert renderq.status()["waiting"] == 1
        time.sleep(0.3)
        assert not marker.exists()
        released = time.time()
    assert proc.wait(30) == 0
    assert float(marker.read_text()) >= released


def test_stale_tickets_are_pruned(paths, fast):
    folder = renderq.queue_dir()
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    ticket = renderq.Ticket(id="1-dead", kind="render", label="crashed", session=None, pid=int(dead.stdout),
                            host=renderq.socket.gethostname(), queued_at=time.time(), started_at=time.time())
    (folder / "1-dead.json").write_text(json.dumps(ticket.as_dict()))
    with renderq.render_slot("render", "next") as mine:
        assert mine is not None
    assert not (folder / "1-dead.json").exists()


def test_concurrency_setting(paths, fast):
    from eks_harness.config import Config

    Config(paths).set("render.concurrency", 2)
    assert renderq.concurrency() == 2
    inside = []
    barrier = threading.Barrier(2, timeout=5)

    def worker(name):
        with renderq.render_slot("render", name):
            inside.append(name)
            barrier.wait()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("x", "y")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(inside) == ["x", "y"]


def test_encode_recording_takes_a_slot(paths, fast, monkeypatch, tmp_path):
    from eks_harness.store import encode

    seen = []
    monkeypatch.setattr(encode, "_encode_recording", lambda *a, **k: seen.append(renderq.status()["running"]) or "done")
    assert encode.encode_recording(tmp_path / "in.mov", tmp_path / "out.mp4") == "done"
    assert seen == [1]


def test_queue_api_and_cli(client, paths, fast, capsys):
    from eks_harness.cli import main

    with renderq.render_slot("render", "visible", session="feature-x"):
        data = client.get("/api/render-queue").json()
        assert data["running"] == 1 and data["items"][0]["session"] == "feature-x"
        capsys.readouterr()
        assert main(["queue", "--local", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["items"][0]["label"] == "visible"
