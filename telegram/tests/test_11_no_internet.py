"""11. No container reaches the internet: the stack network is internal, only `edge` has a
second network, and its outbound traffic is firewalled to the stack subnet. Probes from
inside show that public addresses cannot be reached and that `api.telegram.org` is the
fake."""

from __future__ import annotations

import json
import subprocess

from conftest import STACK, compose

PROBE = """
import socket
for host, port in (("1.1.1.1", 443), ("149.154.167.220", 443), ("8.8.8.8", 53)):
    try:
        socket.create_connection((host, port), timeout=3).close()
        print("REACHED", host)
    except OSError:
        print("blocked", host)
print("api.telegram.org ->", socket.gethostbyname("api.telegram.org"))
"""


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, text=True, capture_output=True, cwd=STACK
    ).stdout


def test_no_container_reaches_the_internet():
    net = json.loads(docker("network", "inspect", "oan-tg-stack"))[0]
    assert net["Internal"] is True
    fake_tg = compose("ps", "-q", "fake-telegram").strip()
    fake_ip = json.loads(docker("inspect", fake_tg))[0]["NetworkSettings"]["Networks"][
        "oan-tg-stack"
    ]["IPAddress"]
    report = []
    for cid in compose("ps", "-q").split():
        info = json.loads(docker("inspect", cid))[0]
        name = info["Config"]["Labels"]["com.docker.compose.service"]
        networks = set(info["NetworkSettings"]["Networks"])
        if name == "edge":
            assert networks == {"oan-tg-stack", "oan-tg-edge"}
        else:
            assert networks == {"oan-tg-stack"}, (name, networks)
        has_python = (
            subprocess.run(
                ["docker", "exec", cid, "sh", "-c", "command -v python"], capture_output=True
            ).returncode
            == 0
        )
        if not has_python:
            report.append(f"{name}: no python; only on the internal network")
            continue
        out = subprocess.run(
            ["docker", "exec", cid, "python", "-c", PROBE],
            text=True,
            capture_output=True,
            timeout=60,
        ).stdout
        assert "REACHED" not in out, (name, out)
        assert f"api.telegram.org -> {fake_ip}" in out, (name, out)
        report.append(f"{name}: " + "; ".join(out.split("\n")[:4]))
    print("\n".join(report))
