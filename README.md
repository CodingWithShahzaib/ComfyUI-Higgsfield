# ComfyUI-Higgsfield

**Higgsfield API nodes for ComfyUI** — generate and edit images, run Seedance video, and call other Higgsfield endpoints from your graph.

Maintained for **[The AI Brief](https://www.youtube.com/@theaibriefyt20)** by **Shahzaib Rehman**.

> **Upstream:** node implementation is based on [w0ver/Higgsfield-api-comfyui-nodes](https://github.com/w0ver/Higgsfield-api-comfyui-nodes) by [Sherif Oneway](https://www.youtube.com/sherifoneway). See [CREDITS.md](CREDITS.md).

---

## Features

- **Image generate / edit** — GPT Image 2.5 Sunburst and Marketing Studio Image 2.0 Alpha
- **Video** — Seedance 2.5 (text / image / reference-to-video) and Seedance 2.0 text-to-video
- **References** — IMAGE batches, plus local PNG/JPEG/WebP, MP4, or WAV via file path
- **Native ComfyUI outputs** — IMAGE list + VIDEO, with saved paths and request IDs
- **Resumable jobs** — `generation_id` avoids accidental duplicate spends; resume timed-out requests
- **Credentials** — Windows DPAPI helper (`Configure API.bat`) or `HF_API_KEY_ID` / `HF_API_KEY_SECRET` env vars
- **Advanced node** — any documented Higgsfield `model_id` + JSON body (Soul, Kling, MiniMax H3, etc.)
- **Example workflows** — five ready graphs under `example_workflows/`

## Supported presets

| Model / route | Use case | References |
| --- | --- | --- |
| GPT Image 2.5 Sunburst (Higgsfield) | Generate or edit images | Optional; up to 16 images |
| Marketing Studio Image 2.0 Alpha | Generate or edit images | Optional images |
| Seedance 2.5 — Text to Video | Video from a prompt | None |
| Seedance 2.5 — Image to Video | Animate a start image | Start (+ optional end) image |
| Seedance 2.5 — Reference to Video | Guided video | Images, videos, and/or audio |
| Seedance 2.0 — Text to Video | Video from a prompt | None |

These are **verified presets**, not the full Higgsfield catalog. For Soul / Kling / MiniMax H3 and other routes, use **Higgsfield - Custom Model (Advanced)**.

## Requirements

- Recent ComfyUI with native **VIDEO** support and the system-user directory API
- Higgsfield API account (key ID + secret + credits) — [console](https://open.higgsfield.ai/)
- Internet for generate / upload / download
- Python deps installed into **ComfyUI’s** environment (`requests`)

## Install

### Option A — git clone (recommended)

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/CodingWithShahzaib/ComfyUI-Higgsfield.git
```

### Option B — ZIP

**Code → Download ZIP** on this repo, extract so the folder layout is:

```text
ComfyUI/
└── custom_nodes/
    └── ComfyUI-Higgsfield/
        ├── __init__.py
        ├── nodes.py
        ├── requirements.txt
        ├── hf/
        ├── web/
        └── example_workflows/
```

`__init__.py` must sit directly inside `ComfyUI-Higgsfield` (no extra nested folder).

### Dependencies

```bash
# from your ComfyUI folder, using ComfyUI's Python
python -m pip install -r custom_nodes/ComfyUI-Higgsfield/requirements.txt
```

Windows Easy-Install / portable example:

```powershell
.\python_embeded\python.exe -m pip install -r .\ComfyUI\custom_nodes\ComfyUI-Higgsfield\requirements.txt
```

### Configure API credentials

Create a key in the [Higgsfield API console](https://open.higgsfield.ai/). You get:

- **API key ID**
- **API key secret**

If you receive `KEY_ID:KEY_SECRET`, split on the first colon.

**Windows (easiest):** double-click `Configure API.bat` inside this folder (same Windows account you use to run ComfyUI). Credentials are encrypted to:

```text
ComfyUI/user/__higgsfield/credentials.dpapi
```

**Or environment variables** (override the encrypted file):

```text
HF_API_KEY_ID=your_key_id
HF_API_KEY_SECRET=your_key_secret
```

Do **not** commit keys, paste them into public workflow JSON you share, or leave them visible on stream.

### Restart ComfyUI

Search the node menu for **Higgsfield**, or drag a file from `example_workflows/` onto the canvas.

## How to use

### Text → image

1. Add **Higgsfield - Generate / Edit Image**
2. Pick a model, paste prompt, set resolution / aspect / quality
3. Leave `references` empty → Queue

Sunburst supports quality presets. For Alpha, leave quality at `high`.

### Image edit / references

```text
Load Image → Higgsfield - Reference Images → Higgsfield - Generate / Edit Image
```

Chain more references with the `previous` input on Reference Images.

### Image → video (Seedance 2.5)

```text
Load Image → Higgsfield - Reference Images → Higgsfield - Generate Video
```

Choose **Seedance 2.5 - Image to Video**. First ref = start frame; optional second = end frame. Leave aspect ratio at default `16:9` for this route (ratio comes from the image).

### Text → video

**Higgsfield - Generate Video** → Text to Video route → prompt → no references.

| Route | Duration | Resolutions |
| --- | --- | --- |
| Seedance 2.5 | 4–30 s | 480p, 720p |
| Seedance 2.0 T2V | 4–15 s | 480p, 720p, 1080p, 4k |

### Reference-to-video

Select **Seedance 2.5 - Reference to Video**. Build a chain with **Reference Images** and/or **Reference File**, then connect to Generate Video.

### Other models (Soul, Kling, MiniMax H3, …)

Use **Higgsfield - Custom Model (Advanced)**:

1. Copy `model_id` + JSON body from the [Higgsfield catalog](https://open.higgsfield.ai/explore)
2. Use `$ref1`, `$ref2`, … in URL fields for connected references
3. Unpack with **Result Images** or **Result Video**

### `generation_id` (important)

It is a **local take label**, not a model seed.

| Goal | Action |
| --- | --- |
| New paid take | Change `generation_id` (e.g. `take-1` → `take-2`) |
| Resume / reuse same job | Keep inputs + `generation_id` the same and queue again |

Outputs land under `ComfyUI/output/higgsfield/<account-folder>/`.

## Example workflows

| Workflow | Use case |
| --- | --- |
| [01 — Text to video](example_workflows/01-text-to-video.json) | Seedance 2.5 from a prompt |
| [02 — GPT Image with a reference](example_workflows/02-gpt-image-reference.json) | Image + reference |
| [03 — Animate an image](example_workflows/03-animate-image.json) | Seedance I2V |
| [04 — Multiple references](example_workflows/04-multiple-references.json) | Multi-ref chain |
| [05 — GPT text-to-image](example_workflows/05-gpt-text-to-image.json) | Image, no refs |

## Nodes

| Node | Purpose |
| --- | --- |
| Generate / Edit Image | Sunburst / Alpha |
| Generate Video | Seedance routes |
| Reference Images | IMAGE → reference chain |
| Reference File | Local image / MP4 / WAV path |
| Custom Model (Advanced) | Arbitrary documented endpoint |
| Resume Request | Recover a saved request |
| Result Images / Result Video | Unpack advanced / resumed results |

## Troubleshooting

| Issue | Check |
| --- | --- |
| Nodes missing | Folder nesting, deps, restart ComfyUI |
| HTTP 401 | Both key ID + secret configured |
| HTTP 403 | Credits / API access |
| HTTP 422 | Route schema (duration, refs, resolution) |
| Timeout | Re-queue with same `generation_id`; check Higgsfield console |

## Credits & links

See **[CREDITS.md](CREDITS.md)**.

- **The AI Brief (YouTube):** https://www.youtube.com/@theaibriefyt20
- **Shahzaib:** https://shahzaib.codes · https://github.com/CodingWithShahzaib
- **Higgsfield docs:** https://docs.higgsfield.ai
- **Upstream pack:** https://github.com/w0ver/Higgsfield-api-comfyui-nodes

## License / redistribution

Upstream repository currently declares no SPDX license on GitHub. This fork is published for The AI Brief viewers with attribution to the original author. If you redistribute further, keep [CREDITS.md](CREDITS.md) and link the upstream repo.
