import json
import os

CONFIG_FILE = "config.json"


def load_config():
    cfg = {
        "image_output": "timelapse",
        "video_output": "videos",
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    cfg.update({k: v for k, v in data.items() if k in cfg})
        except Exception as e:
            print(f"Failed to read config: {e}")
    os.makedirs(cfg["image_output"], exist_ok=True)
    os.makedirs(cfg["video_output"], exist_ok=True)
    return cfg


def persist_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Failed to save config: {e}")
