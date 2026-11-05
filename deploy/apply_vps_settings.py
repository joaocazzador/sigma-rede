"""Mescla deploy/vps-settings.json nas configurações salvas da máquina do servidor (rodado pelo deploy_vps.sh)."""
import json
import os

PATH = "/opt/sigma/data/host/config.json"
cfg = json.load(open(PATH)) if os.path.exists(PATH) else {"version": 2}
cfg.setdefault("settings", {}).update(json.load(open(os.path.join(os.path.dirname(__file__), "vps-settings.json"))))
os.makedirs(os.path.dirname(PATH), exist_ok=True)
tmp = PATH + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, PATH)
print("  configurações da máquina do servidor:", json.dumps(cfg["settings"], sort_keys=True))
