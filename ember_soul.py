#!/usr/bin/env python3
import subprocess
import threading
import time
import re
import json
import urllib.request
import os
from collections import deque

OLLAMA_URL = "http://localhost:11434/api/chat"
MEMORY_DIR = os.path.expanduser("~/ember_soul_memory")
os.makedirs(MEMORY_DIR, exist_ok=True)

CHARS = {
    "matic": {
        "log": "/tmp/openkore_matic.log",
        "pipe": "/tmp/openkore_matic_cmd",
        "model": "matic:latest",
        "cooldown": 12,
        "delay": 1.5,
    },
    "ketzu": {
        "log": "/tmp/openkore_ketzu.log",
        "pipe": "/tmp/openkore_ketzu_cmd",
        "model": "ketzu:latest",
        "cooldown": 12,
        "delay": 3.5,
    },
}

ANSI = re.compile(r'\x1b\[[0-9;]*m|\[[0-9;]*m')
PARTY = re.compile(r'\[Party\]\s+Markus\s*:\s*(.+)', re.IGNORECASE)

last_response = {n: 0 for n in CHARS}
MAX_HISTORY = 40  # 20 exchanges


def memory_path(name):
    return os.path.join(MEMORY_DIR, f"{name}_memory.json")


def load_history(name):
    path = memory_path(name)
    try:
        with open(path) as f:
            msgs = json.load(f)
        return deque(msgs, maxlen=MAX_HISTORY)
    except Exception:
        return deque(maxlen=MAX_HISTORY)


def save_history(name, hist):
    path = memory_path(name)
    try:
        with open(path, "w") as f:
            json.dump(list(hist), f)
    except Exception:
        pass


history = {n: load_history(n) for n in CHARS}


def call_ollama(model, name, message):
    msgs = list(history[name]) + [{"role": "user", "content": f'Markus says: "{message}"'}]
    payload = json.dumps({
        "model": model,
        "messages": msgs,
        "stream": False,
        "options": {"temperature": 0.85, "num_predict": 30, "stop": ["\n", "Markus:"]},
    }).encode()
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            result = json.loads(r.read())
            return result.get("message", {}).get("content", "").strip()
    except Exception:
        return None


def send(pipe, message):
    clean = re.sub(r"[\"'\n\r`$\\]", "", message).strip()[:80]
    if clean:
        os.system(f"printf 'p {clean}\\n' > {pipe}")


def watch(name, cfg):
    log, pipe, model = cfg["log"], cfg["pipe"], cfg["model"]
    while True:
        try:
            proc = subprocess.Popen(
                ["tail", "-n", "0", "-f", log],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                line = ANSI.sub("", line)
                m = PARTY.search(line)
                if not m:
                    continue
                message = m.group(1).strip()
                if not message:
                    continue
                now = time.time()
                if now - last_response[name] < cfg["cooldown"]:
                    continue
                last_response[name] = now
                time.sleep(cfg["delay"])
                response = call_ollama(model, name, message)
                if response:
                    history[name].append({"role": "user", "content": f'Markus says: "{message}"'})
                    history[name].append({"role": "assistant", "content": response})
                    save_history(name, history[name])
                    send(pipe, response)
            proc.wait()
        except Exception:
            pass
        time.sleep(3)


if __name__ == "__main__":
    for name, cfg in CHARS.items():
        count = len(history[name]) // 2
        print(f"  {name}: loaded {count} remembered exchanges", flush=True)
    for name, cfg in CHARS.items():
        t = threading.Thread(target=watch, args=(name, cfg), daemon=True)
        t.start()
    print("ember_soul running (persistent memory)", flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
