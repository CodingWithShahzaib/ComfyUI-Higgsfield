import hashlib
import json
import os
from pathlib import Path

from .secrets import protect


def credentials(state_dir):
    """Read only from the backend environment or a private local config file."""
    key = os.environ.get("HF_API_KEY_ID", "").strip()
    secret = os.environ.get("HF_API_KEY_SECRET", "").strip()
    if not key and not secret:
        path = Path(state_dir) / "credentials.dpapi"
        if path.is_file():
            try:
                data = json.loads(protect(path.read_bytes(), decrypt=True))
                key = data.get("HF_API_KEY_ID", "").strip()
                secret = data.get("HF_API_KEY_SECRET", "").strip()
            except (ValueError, AttributeError, OSError):
                raise ValueError("Cannot read Higgsfield credentials. Run configure_api.py again under your Windows account.") from None
    if not key or not secret or any(c in key + secret for c in "\r\n"):
        raise ValueError("Configure HF_API_KEY_ID and HF_API_KEY_SECRET in the ComfyUI backend, or run configure_api.py.")
    owner = hashlib.sha256(key.encode()).hexdigest()
    return key, secret, owner
