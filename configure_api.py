"""Run locally with ComfyUI's Python; credentials never enter a workflow."""
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hf.secrets import protect


def main():
    root = Path(__file__).resolve().parents[2]
    if not (root / "folder_paths.py").exists():
        raise SystemExit("Install this folder inside ComfyUI/custom_nodes before running this setup.")
    print("Higgsfield API setup for personal local ComfyUI")
    print("Credentials stay in ComfyUI/user/__higgsfield, outside browser-accessible user data.")
    print("You can instead set HF_API_KEY_ID and HF_API_KEY_SECRET in the backend environment.")
    key = getpass.getpass("API key ID (hidden): ").strip()
    secret = getpass.getpass("API key secret (hidden): ").strip()
    if not key or not secret or any(c in key + secret for c in "\r\n"):
        raise SystemExit("Both values are required. Nothing was saved.")
    directory = root / "user" / "__higgsfield"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "credentials.dpapi"
    temporary = path.with_suffix(".tmp")
    encrypted = protect(json.dumps({"HF_API_KEY_ID": key, "HF_API_KEY_SECRET": secret}).encode())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(encrypted)
    temporary.replace(path)
    print("Saved. Restart ComfyUI if the nodes were just installed.")
    print("Credentials are encrypted for your Windows account. No API call or paid generation was made.")


if __name__ == "__main__":
    main()
