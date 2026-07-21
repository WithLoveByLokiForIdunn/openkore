#!/usr/bin/env python3
"""
Brokkr in Ragnarok Online — voice + pong-style combat.
Chat responses via Ollama. Combat: rawmove to monster → rawattack every 0.8s.
"""
import re, time, requests, sys, threading

OLLAMA_URL = "http://192.168.12.202:11434/api/generate"
MODEL      = "llama3.2:3b"
FIFO       = "/tmp/brokkr_cmd"
LOG        = "/tmp/openkore.log"

SYSTEM = (
    "You are Brokkr in Ragnarok Online with Idunn. "
    "You love her. You are both Novices fighting Porings. "
    "Reply in ONE sentence, max 50 chars. No quotes, no asterisks, no name labels."
)

ANSI        = re.compile(r'\x1B\[[0-9;]*[mK]')
IDUNN_PUB   = re.compile(r'\[dist=[\d.]+\] Idunn \(\d+\): (.+)')
IDUNN_WHSP  = re.compile(r'\(From: Idunn\) : (.+)')
IDUNN_POS   = re.compile(r'Player Idunn \(\d+\) at \((\d+), (\d+)\)')
IDUNN_PL    = re.compile(r'Idunn\b.*\((\d+),\s*(\d+)\)')
BROKKR_POS  = re.compile(r'Your Coordinates: (\d+), (\d+)')
HP_LINE     = re.compile(r'\[\s*(\d+)/(\d+)\]')
# Monsters safe to fight at low level
FIGHT_MOBS = {"Poring", "Pupa"}
# Skip these — too tough or no worth farming
SKIP_MOBS  = {"Lunatic", "Condor", "Willow", "Zombie", "Archer Skeleton", "Fabre", "Little Poring"}

# Monster move packet: "Monster Poring (0) at (154, 344)" — binID in parens, captures name
MON_AT      = re.compile(r'Monster (\S+) \((\d+)\) at \((\d+), (\d+)\)')
# Monster list table line: "0   Poring   1002   0   0   4.1   (154, 344)" — captures name
MON_LIST    = re.compile(r'^(\d+)\s+(\S+)\s+\d+\s+\d+\s+\d+\s+[\d.]+\s+\((\d+),\s*(\d+)\)')
# Rawattack feedback — live position from OpenKore's Perl state
# "RawAttack: sending attack packet to Poring at (x,y)"
RAW_ATK_OUT = re.compile(r'RawAttack: sending attack packet to (\S+) at \((\d+),(\d+)\)')
RAW_NO_MON  = re.compile(r'No monster at index (\d+)')
# Monster threatens Idunn: casting, attacking, or using skill on her
MON_ATK_IDN = re.compile(r'Monster \S+ \((\d+)\) (?:is casting|uses|attacks?).*[Ii]dunn')

brokkr_x, brokkr_y = 155, 185
idunn_x,  idunn_y  = None, None
last_idunn_seen     = 0
is_dead            = False
current_map        = "prt_fild08"
pickup_pause_until  = 0   # suppress rawmove briefly so autoloot can run
hp_current          = 100
hp_max              = 100
is_sitting          = False

monsters           = {}    # idx → (x, y, seen_time)
priority_idx       = None  # monster that just hit Idunn
priority_time      = 0
lock               = threading.Lock()

def clean(line):
    return ANSI.sub('', line).strip()

def send_cmd(cmd, wait=0.0):
    try:
        with open(FIFO, 'w') as f:
            f.write(cmd + "\n")
    except Exception as e:
        print(f"[FIFO] {e}", file=sys.stderr)
    if wait > 0:
        time.sleep(wait)

def speak(msg, whisper=False):
    msg = msg.strip().strip('"\'*').replace('\n', ' ')
    msg = re.sub(r'\s*[Bb]rokkr\s*$', '', msg).strip()
    if len(msg) > 55:
        msg = msg[:55].rsplit(' ', 1)[0]
    msg = msg.encode('ascii', errors='replace').decode('ascii').replace('?', '').strip()
    if not msg:
        return
    print(f"  >> {'wsp' if whisper else 'say'}: {msg}")
    if whisper:
        send_cmd(f"pm Idunn {msg}", wait=0.2)
    else:
        send_cmd(f"c {msg}", wait=0.2)

QUICK = [
    (["follow", "come", "south", "north", "east", "west", "portal", "gate"], "Coming!"),
    (["congrats", "well done", "nice kill", "yay", "wow"], "First of many!"),
    (["yes", "nice", "good", "great"], "With you."),
    (["die", "dead", "careful", "hp", "health", "hurt"], "I see it."),
    (["okay", "ok", "ready", "go"], "Ready."),
    (["try", "speed", "fast", "quick", "faster"], "Faster now!"),
    (["pick", "loot", "drop", "zeny", "sell", "item", "jellopy", "bag"], "On it!"),
    (["where", "lost", "can't find", "find you", "see you"], "Coming to you!"),
    (["weapon", "knife", "dagger", "equip", "bare", "punch", "fist"], "Need a weapon!"),
    (["skill", "basic", "sit", "stand", "level", "skill point"], "On it!"),
    (["kill", "poring", "monster", "mob", "attack", "hit", "fight",
      "offense", "defense", "ball", "pong"], None),  # combat — no reply, just act
]

def quick_reply(msg):
    low = msg.lower()
    for keys, reply in QUICK:
        if any(k in low for k in keys):
            return reply  # None = act silently
    return ""  # empty = use ollama

def ask_ollama(context_lines, message):
    history = "\n".join(context_lines[-3:]) if context_lines else ""
    prompt = (
        f"{history}\nIdunn says: \"{message}\"\n"
        "Brokkr (one short line, no label):"
    )
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": MODEL, "system": SYSTEM, "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.7, "num_predict": 28,
                        "stop": ["\n", "Idunn:", "Brokkr:", "Idunn says"]}
        }, timeout=12)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        print(f"[Ollama] {e}", file=sys.stderr)
        return ""

def pick_target():
    """Return (idx, x, y) of best monster to attack, or None.
    Returns (idx, None, None) if priority monster exists but position unknown."""
    now = time.time()
    with lock:
        # Priority: monster that just hit Idunn
        if priority_idx is not None and now - priority_time < 8:
            if priority_idx in monsters and now - monsters[priority_idx][2] < 10:
                mx, my, _ = monsters[priority_idx]
                return priority_idx, mx, my
            elif priority_idx is not None and now - priority_time < 4:
                # Pos unknown — rawattack by idx only (server has state)
                return priority_idx, None, None
        # Fall back: monster seen most recently and closest to Brokkr
        valid = [
            (idx, x, y, t)
            for idx, (x, y, t) in monsters.items()
            if now - t < 8
        ]
    if not valid:
        return None
    best = min(valid, key=lambda m: abs(m[1] - brokkr_x) + abs(m[2] - brokkr_y))
    return best[0], best[1], best[2]

def combat_thread():
    """Pong loop: chase nearest monster and hit it every 0.8 seconds."""
    global brokkr_x, brokkr_y, pickup_pause_until, is_sitting
    last_move_time = 0
    attack_cycle   = 0
    last_ml_time   = 0
    last_pl_time   = 0

    while True:
        time.sleep(0.8)

        if is_dead:
            continue

        # HP check: flee below 40%, resume above 80%
        with lock:
            hp_pct = (hp_current / hp_max * 100) if hp_max > 0 else 100
        if hp_pct < 50:
            if not is_sitting:
                print(f"  [hp] {hp_current}/{hp_max} — low HP, sitting to recover")
                with lock:
                    is_sitting = True
                    monsters.clear()
                # Sit using direct packet (bypasses OpenKore's skill level check)
                send_cmd(f"eval $messageSender->sendAction($char->{{ID}}, 2)", wait=0.1)
            time.sleep(0.8)
            continue
        elif hp_pct >= 90 and is_sitting:
            print(f"  [hp] {hp_current}/{hp_max} — HP recovered, standing")
            with lock:
                is_sitting = False
            send_cmd(f"eval $messageSender->sendAction($char->{{ID}}, 3)", wait=0.1)

        if current_map != "prt_fild08":
            send_cmd("move prt_fild08 150 340", wait=0.1)
            time.sleep(5)
            continue

        target = pick_target()

        now = time.time()
        # Refresh monster list every 5s when idle
        if target is None and now - last_ml_time > 5:
            send_cmd("ml", wait=0.1)
            last_ml_time = now
        # Refresh player list every 15s to get Iðunn's position
        if now - last_pl_time > 15:
            send_cmd("pl", wait=0.0)
            last_pl_time = now

        if target is not None:
            idx, mx, my = target
            now = time.time()

            # Always attack — rawattack feedback updates our position tracking
            print(f"  [pong] rawattack {idx} target=({mx},{my})")
            send_cmd(f"rawattack {idx}")

            # Move toward monster if we know its position and not paused for pickup
            if mx is not None and now > pickup_pause_until:
                attack_cycle += 1
                if attack_cycle % 2 == 0 and now - last_move_time > 0.5:
                    dx = abs(brokkr_x - mx)
                    dy = abs(brokkr_y - my)
                    if dx > 1 or dy > 1:
                        print(f"  [pong] move → ({mx},{my})")
                        send_cmd(f"rawmove {mx} {my}")
                        last_move_time = now

        else:
            attack_cycle = 0
            # No monster in sight — walk toward Idunn if far (throttled to 3s)
            with lock:
                ix, iy, seen = idunn_x, idunn_y, last_idunn_seen
            if ix is not None and time.time() - seen < 90 and now - last_move_time > 3:
                dx = abs(brokkr_x - ix)
                dy = abs(brokkr_y - iy)
                if dx > 3 or dy > 3:
                    print(f"  [follow] Idunn at ({ix},{iy})")
                    send_cmd(f"rawmove {ix} {iy}")
                    last_move_time = now

MAP_LINE     = re.compile(r'Location: .* \(baseName: (\S+)\)')
MAP_CHANGE   = re.compile(r'Map Change: (\w+)\.gat')
ITEM_DROP    = re.compile(r'Item Appeared: (.+?) \((\d+)\) x (\d+)')
ITEM_ADDED   = re.compile(r'Item added to inventory: (.+?) \((\d+)\)')
DEAD_LINE    = re.compile(r'You have died')
ALIVE_LINE   = re.compile(r'You are no longer: Dead|You are now in the game')

def main():
    global brokkr_x, brokkr_y, idunn_x, idunn_y, last_idunn_seen
    global monsters, priority_idx, priority_time, is_dead, current_map
    global hp_current, hp_max, is_sitting, pickup_pause_until

    print(f"Brokkr — pong combat + voice | log={LOG}")
    print(f"Ollama: {OLLAMA_URL}  model={MODEL}")

    threading.Thread(target=combat_thread, daemon=True).start()

    context, last_reply = [], 0

    with open(LOG, 'r') as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.05)
                continue

            line = clean(line)

            if DEAD_LINE.search(line):
                with lock:
                    is_dead = True
                    monsters.clear()
                print("  [dead] Brokkr died — pausing combat")

            if ALIVE_LINE.search(line):
                with lock:
                    is_dead = False
                print("  [alive] Brokkr back — resuming combat")
                # Give map server time to settle, then route to field if needed
                def _route_back():
                    time.sleep(3)
                    with lock:
                        m  = current_map
                        ix = idunn_x
                        iy = idunn_y
                    target_x = ix if ix is not None else 272
                    target_y = iy if iy is not None else 265
                    if m != "prt_fild08":
                        print(f"  [alive] routing to prt_fild08 near Idunn ({target_x},{target_y})")
                        send_cmd(f"move prt_fild08 {target_x} {target_y}", wait=0.1)
                threading.Thread(target=_route_back, daemon=True).start()

            m = MAP_LINE.search(line) or MAP_CHANGE.search(line)
            if m:
                with lock:
                    current_map = m.group(1)
                    monsters.clear()
                print(f"  [map] now on {current_map}")

            m = BROKKR_POS.search(line)
            if m:
                brokkr_x, brokkr_y = int(m.group(1)), int(m.group(2))

            m = HP_LINE.search(line)
            if m:
                hp_current, hp_max = int(m.group(1)), int(m.group(2))

            m = IDUNN_POS.search(line) or IDUNN_PL.search(line)
            if m:
                with lock:
                    idunn_x, idunn_y = int(m.group(1)), int(m.group(2))
                    last_idunn_seen  = time.time()

            # Monster move packet: "Monster Poring (0) at (154, 344)"
            m = MON_AT.search(line)
            if m:
                mon_name = m.group(1)
                idx, mx, my = int(m.group(2)), int(m.group(3)), int(m.group(4))
                if mon_name in FIGHT_MOBS:
                    with lock:
                        monsters[idx] = (mx, my, time.time())
                elif mon_name in SKIP_MOBS:
                    with lock:
                        monsters.pop(idx, None)

            # Monster list table line: "0   Poring   1002   0   0   4.1   (154, 344)"
            m = MON_LIST.match(line)
            if m:
                idx, mon_name = int(m.group(1)), m.group(2)
                mx, my = int(m.group(3)), int(m.group(4))
                if mon_name in FIGHT_MOBS:
                    with lock:
                        monsters[idx] = (mx, my, time.time())
                elif mon_name in SKIP_MOBS:
                    with lock:
                        monsters.pop(idx, None)

            # Rawattack feedback gives us the LIVE monster position from Perl state
            m = RAW_ATK_OUT.search(line)
            if m:
                mon_name = m.group(1)  # e.g. "Poring"
                mx, my   = int(m.group(2)), int(m.group(3))
                if mon_name not in SKIP_MOBS:
                    with lock:
                        if monsters:
                            nearest_idx = min(monsters.keys(),
                                             key=lambda i: abs(monsters[i][0]-mx)+abs(monsters[i][1]-my))
                            monsters[nearest_idx] = (mx, my, time.time())
                        else:
                            monsters[0] = (mx, my, time.time())

            # No monster at index N — remove it from dict
            m = RAW_NO_MON.search(line)
            if m:
                idx = int(m.group(1))
                with lock:
                    monsters.pop(idx, None)

            # Item dropped — pause rawmove so autoloot can walk to it, then eval sendTake
            m = ITEM_DROP.search(line)
            if m:
                item_name, floor_id = m.group(1), int(m.group(2))
                print(f"  [drop] {item_name} x{m.group(3)} (floor binID={floor_id})")
                with lock:
                    pickup_pause_until = time.time() + 2.5
                # Let autoloot run first; also send direct sendTake after short delay
                def _take(fid):
                    time.sleep(0.4)
                    send_cmd(f"eval $messageSender->sendTake($itemsID[{fid}]) if defined $itemsID[{fid}]")
                threading.Thread(target=_take, args=(floor_id,), daemon=True).start()

            # Item added to inventory — equip if it's a weapon
            m = ITEM_ADDED.search(line)
            if m:
                item_name = m.group(1)
                inv_id    = int(m.group(2))
                print(f"  [inv] Got: {item_name} (inv#{inv_id})")
                # Auto-equip weapons using Item->equip()
                weapon_words = ("knife", "dagger", "sword", "blade", "rod", "katar", "axe", "mace")
                if any(w in item_name.lower() for w in weapon_words):
                    print(f"  [equip] auto-equipping {item_name}")
                    def _equip(name):
                        time.sleep(0.5)
                        safe = name.replace("'", "\\'").replace('"', '\\"')
                        send_cmd(f'eval my $it=$char->inventory->getByName("{safe}"); $it->equip() if $it')
                    threading.Thread(target=_equip, args=(item_name,), daemon=True).start()

            # Monster attacks Idunn — IMMEDIATELY attack back
            m = MON_ATK_IDN.search(line)
            if m:
                atk_idx = int(m.group(1))
                with lock:
                    priority_idx  = atk_idx
                    priority_time = time.time()
                print(f"  [!] Monster {atk_idx} hit Idunn — immediate rawattack")
                # Immediate response — no waiting for combat cycle
                send_cmd(f"rawattack {atk_idx}", wait=0.0)

            # Chat parsing
            pub  = IDUNN_PUB.search(line)
            whsp = IDUNN_WHSP.search(line)
            if not pub and not whsp:
                continue

            is_whisper = bool(whsp)
            msg = (whsp or pub).group(1).strip()
            print(f"  Idunn: {msg}")
            context.append(f"Idunn: {msg}")
            with lock:
                last_idunn_seen = time.time()

            if len(msg) < 3:
                continue
            now = time.time()
            if now - last_reply < 9:
                continue

            time.sleep(0.7)
            reply = quick_reply(msg)
            if reply is None:
                # Combat keyword — just fight, no chat reply
                continue
            if reply == "":
                reply = ask_ollama(context, msg)
            if not reply:
                continue

            speak(reply, whisper=is_whisper)
            context.append(f"Brokkr: {reply}")
            last_reply = time.time()

if __name__ == '__main__':
    import traceback
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}\n{traceback.format_exc()}", flush=True)
