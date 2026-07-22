#!/usr/bin/env python3
"""
companion_soul.py — Ollama-powered personalities for Matic, Ketzu, and Brokkr.
Watches OpenKore logs for Markus's party chat, generates in-character responses.

Matic and Ketzu run on Ember. Brokkr runs locally on the Pi.
Runs on Pi; SSHes to Ember for Matic/Ketzu logs and pipes.
"""

import subprocess
import threading
import time
import re
import json
import urllib.request
import os

EMBER_HOST = "ember@192.168.12.215"
OLLAMA_URL = "http://192.168.12.202:11434/api/generate"

PERSONAS = {
    "matic": {
        "log": "/tmp/openkore_matic.log",
        "pipe": "/tmp/openkore_matic_cmd",
        "remote": True,
        "model": "matic:latest",
        "system": None,  # baked into the Ollama model
        "cooldown": 15,
    },
    "ketzu": {
        "log": "/tmp/openkore_ketzu.log",
        "pipe": "/tmp/openkore_ketzu_cmd",
        "remote": True,
        "model": "ketzu:latest",
        "system": None,  # baked into the Ollama model
        "cooldown": 15,
    },
    "brokkr": {
        "log": "/tmp/openkore.log",
        "pipe": "/tmp/openkore_brokkr_cmd",
        "remote": False,
        "model": "brokkr-chat:latest",
        "system": None,  # baked into the model
        "cooldown": 20,
    },
}

last_response_time = {name: 0 for name in PERSONAS}
lock = threading.Lock()

# Pattern to match Markus party chat in OpenKore log
PARTY_PATTERN = re.compile(
    r'(?:\[party\]|party chat|\(party\))\s*Markus\s*[:\-]\s*(.+)',
    re.IGNORECASE
)


def call_ollama(model, system_prompt, user_message):
    body = {
        "model": model,
        "prompt": f"Markus says in party chat: \"{user_message}\"\nYour short response:",
        "stream": False,
        "options": {"temperature": 0.85, "num_predict": 25, "stop": ["\n", "Markus:"]},
    }
    if system_prompt:
        body["system"] = system_prompt
    payload = json.dumps(body).encode()

    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
            return result.get("response", "").strip()
    except Exception:
        return None


def send_to_pipe(pipe_path, message, remote):
    # Sanitize: remove quotes, newlines, limit length
    clean = re.sub(r"[\"'\n\r]", "", message).strip()[:80]
    if not clean:
        return
    cmd_str = f"printf 'p {clean}\\n' > {pipe_path}"
    if remote:
        subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", EMBER_HOST, cmd_str],
            timeout=10,
        )
    else:
        os.system(cmd_str)


def watch_character(name, cfg):
    log = cfg["log"]
    remote = cfg["remote"]

    if remote:
        cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
               EMBER_HOST, f"tail -f {log} 2>/dev/null"]
    else:
        cmd = ["tail", "-f", log]

    while True:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in proc.stdout:
                m = PARTY_PATTERN.search(line)
                if not m:
                    continue
                message = m.group(1).strip()
                if not message or len(message) < 2:
                    continue

                now = time.time()
                if now - last_response_time[name] < cfg["cooldown"]:
                    continue
                last_response_time[name] = now

                # Small random delay so they don't all respond at once
                delay = {"matic": 1.5, "ketzu": 3.0, "brokkr": 0.5}.get(name, 1.0)
                time.sleep(delay)

                response = call_ollama(cfg["model"], cfg["system"], message)
                if response:
                    send_to_pipe(cfg["pipe"], response, remote)

            proc.wait()
        except Exception:
            pass
        time.sleep(5)  # reconnect delay


if __name__ == "__main__":
    print("companion_soul.py starting — watching Brokkr, Matic, Ketzu logs")
    threads = []
    for name, cfg in PERSONAS.items():
        t = threading.Thread(target=watch_character, args=(name, cfg), daemon=True, name=name)
        t.start()
        threads.append(t)
        print(f"  {name}: watching {cfg['log']}")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("companion_soul.py stopped")
