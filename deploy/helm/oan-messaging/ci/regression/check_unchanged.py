# ruff: noqa: S603, S607
# Subprocess argv is built only from fixed local chart files; no shell or user input.
"""Existing values render the same (Telegram added next to SMS, never through it).

    python3 ci/regression/check_unchanged.py            check (exit 1 on any difference)
    python3 ci/regression/check_unchanged.py --update   rewrite the golden files

Also checks the Telegram worker (``workerTelegram``, off by default): it holds the bot
token and no 8028 secret; the SMS worker holds no bot token; and its guards refuse unsafe
values.
"""

from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "golden"
CASES = {
    "values.yaml": [],
    "values-pilot.yaml": ["-f", str(CHART / "values-pilot.yaml")],
    **{
        p.name: ["-f", str(p)]
        for p in sorted((CHART / "ci").glob("0[1-4]-*-values.yaml"))
    },
}
TG = CHART / "ci" / "05-telegram-worker-values.yaml"

failures: list[str] = []
passed = 0


def check(cond: bool, label: str) -> None:
    global passed
    if cond:
        passed += 1
    else:
        failures.append(label)
        print(f"FAIL  {label}")


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "oan-msg", str(CHART), "-n", "oan-messaging-dev", *args],
        capture_output=True,
        text=True,
    )


def unchanged(update: bool) -> None:
    for name, args in CASES.items():
        r = helm(*args)
        check(r.returncode == 0, f"{name}: renders")
        golden = GOLDEN / name
        if update:
            golden.write_text(r.stdout)
            continue
        same = golden.exists() and golden.read_text() == r.stdout
        if not same and golden.exists():
            diff = difflib.unified_diff(
                golden.read_text().splitlines(), r.stdout.splitlines(), "golden", "now",
                lineterm="", n=1,
            )
            print("\n".join(list(diff)[:40]))
        check(same, f"{name}: renders exactly as the golden file")


def env_names(d: dict[str, Any]) -> set[str]:
    spec = d["spec"]["template"]["spec"]
    return {e["name"] for c in spec["containers"] for e in c.get("env", [])}


def telegram_worker() -> None:
    r = helm("-f", str(TG))
    check(r.returncode == 0, f"telegram worker values render: {r.stderr.strip()[:200]}")
    if r.returncode != 0:
        return
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    deps = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    tg = deps.get("oan-msg-oan-messaging-worker-telegram")
    sms = deps.get("oan-msg-oan-messaging-worker")
    check(tg is not None and sms is not None, "both worker Deployments render")
    if tg is None or sms is None:
        return
    tg_env = env_names(tg)
    sms_env = env_names(sms)
    check("OAN_MSG_TELEGRAM_BOT_TOKEN__OAN" in tg_env, "telegram worker has the bot token")
    check(not any(n.startswith("OAN_MSG_SMS8028") for n in tg_env), "telegram worker: no 8028")
    check(
        not any(n.startswith("OAN_MSG_TELEGRAM_BOT_TOKEN__") for n in sms_env),
        "SMS worker has no bot token",
    )
    envs = {
        e["name"]: e.get("value")
        for c in tg["spec"]["template"]["spec"]["containers"] for e in c["env"]
    }
    check(envs.get("OAN_MSG_WORKER_CHANNELS") == "telegram", "telegram worker channels")
    check(envs.get("OAN_MSG_TELEGRAM_RATE_PER_SECOND") == "20", "pace default 20 (TG-08)")
    check(envs.get("OAN_MSG_TELEGRAM_OTP_RESERVE") == "0", "OTP reserve default 0")
    check("OAN_MSG_TELEGRAM_API_BASE_URL" not in envs, "API address: code default")
    vols = tg["spec"]["template"]["spec"].get("volumes", [])
    check(all("secret" not in v for v in vols), "telegram worker mounts no Secret volume")
    check(not tg["spec"]["template"]["spec"].get("initContainers"), "no TLS init container")
    sms_envs = {
        e["name"]: e.get("value")
        for c in sms["spec"]["template"]["spec"]["containers"] for e in c["env"]
    }
    check(sms_envs.get("OAN_MSG_WORKER_CHANNELS") == "sms", "SMS worker stays [sms]")
    nps = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    check("oan-msg-oan-messaging-worker-telegram-ingress" in nps, "telegram worker: no ingress")
    eg = nps.get("oan-msg-oan-messaging-worker-telegram-egress")
    check(eg is not None and len(eg["spec"]["egress"]) == 1, "telegram worker: Telegram egress")

    base = ("-f", str(TG))
    guards = [
        (("--set", "workerTelegram.ratePerSecond=31"), "at most 30"),
        (("--set", "workerTelegram.otpReserve=20"), "otpReserve must be below"),
        (("--set", "worker.channels={sms,telegram}"), "keep worker.channels at [sms]"),
        (("--set", "secrets.telegram.bots=null"), "needs secrets.telegram.bots"),
        (("--set", "workerTelegram.apiBaseUrl=http://api.telegram.org"), "https origin"),
        (("--set", "workerTelegram.terminationGracePeriodSeconds=10"), "worker lease"),
        (("--set", "workerTelegram.command=null"), "workerTelegram"),
    ]
    for extra, needle in guards:
        err = helm(*base, *extra)
        check(err.returncode != 0 and needle in err.stderr, f"guard: {needle}")
    ok = helm(*base, "--set", "workerTelegram.apiBaseUrl=https://api.telegram.org")
    check(ok.returncode == 0, "https API address renders")


def main() -> int:
    update = "--update" in sys.argv[1:]
    unchanged(update)
    if not update:
        telegram_worker()
    print(f"\n{passed} checks passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
