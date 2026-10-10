from __future__ import annotations

import platform
import time

import httpx
from test_nodes import live_hub  # noqa: F401

from eks_harness.cli.client import HarnessClient
from eks_harness.nodes.host_node import HostNode, host_node_id
from eks_harness.video.farm.nodes import node_hosts


def _wait_online(client: httpx.Client, node_id: str) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        for item in client.get("/api/nodes").json()["items"]:
            if item["id"] == node_id and item["online"]:
                return item
        time.sleep(0.1)
    raise AssertionError(f"{node_id} never came online")


def test_host_node_id_comes_from_the_host_name() -> None:
    assert host_node_id("studio") == "studio"
    derived = host_node_id()
    assert derived and derived == derived.lower() and " " not in derived


def test_the_hub_machine_runs_as_its_own_node(live_hub, monkeypatch) -> None:  # noqa: F811
    ctx, base = live_hub
    monkeypatch.setattr("eks_harness.nodes.agent.probe",
                        lambda blender=None: {"os": "darwin", "hostname": platform.node(), "cpus": 4})
    ctx.config.set("nodes.hostNodeId", "hub-mac")
    first = HostNode(ctx)
    first.start()
    try:
        with httpx.Client(base_url=base, timeout=30) as client:
            item = _wait_online(client, "hub-mac")
            assert item["host"] is True and item["slots"]
            removed = client.delete("/api/nodes/hub-mac")
            assert removed.status_code == 400 and removed.json()["error"] == "host_node"
            assert client.post("/api/nodes/hub-mac/token").status_code == 400
            hub = HarnessClient(base)
            try:
                assert [h.node for h in node_hosts({"enabled": True}, client=hub)] == ["hub-mac"]
                assert node_hosts({"enabled": True}, client=hub, skip_this_machine=True) == []
            finally:
                hub.close()
    finally:
        first.stop()
    second = HostNode(ctx)
    second.start()
    try:
        with httpx.Client(base_url=base, timeout=30) as client:
            assert _wait_online(client, "hub-mac")["host"] is True
    finally:
        second.stop()
