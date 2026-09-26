import hashlib
import io
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import torch
from PIL import Image, ImageOps

import folder_paths
from comfy.cli_args import args
from comfy.model_management import throw_exception_if_processing_interrupted
from comfy_api.latest import InputImpl, ui
from server import PromptServer

from .hf.client import Client, endpoint
from .hf.config import credentials
from .hf.jobs import Jobs, run, resume
from .hf.models import (IMAGE_MODELS, VIDEO_MODELS, MINIMAX_H3_MODELS, WAN30_MODELS,
                        IMAGE_RATIOS, RATIOS, H3_RATIOS, WAN_RATIOS,
                        image_input, video_input, minimax_h3_input, wan30_input,
                        attach_references, controls)

log = logging.getLogger(__name__)
interrupt = throw_exception_if_processing_interrupted
CATEGORY = "Higgsfield"
CONTROL_INPUTS = {
    "generation_id": ("STRING", {"default": "take-1", "tooltip": "Change for a NEW paid generation. Keep unchanged to resume/reuse a result."}),
    "timeout_seconds": ("INT", {"default": 1800, "min": 30, "max": 7200}),
}


def state_directory():
    if args.multi_user:
        raise RuntimeError("Higgsfield nodes are configured for personal local ComfyUI. Multi-user mode is not supported.")
    return Path(folder_paths.get_system_user_directory("higgsfield"))


def progress_callback(node_id):
    last = [None]

    def report(status, request_id):
        if last[0] == (status, request_id):
            return
        last[0] = (status, request_id)
        log.info("Higgsfield: %s%s", status, f" (request {request_id})" if request_id else "")
        server = PromptServer.instance
        if server is not None:
            server.send_sync("higgsfield.status", {"node_id": str(node_id), "status": status, "request_id": request_id}, server.client_id)
    return report


def file_reference(path, kind):
    path = Path(path).resolve()
    allowed = {"image": {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"},
               "video": {".mp4": "video/mp4"}, "audio": {".wav": "audio/wav", ".mp3": "audio/mpeg"}}
    mime = allowed.get(kind, {}).get(path.suffix.lower())
    if not mime or not path.is_file():
        raise ValueError("Select an existing PNG/JPEG/WebP image, MP4 video, or WAV/MP3 audio reference.")
    if path.stat().st_size > 1024 ** 3:
        raise ValueError("The local reference upload limit is 1 GiB.")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            interrupt()
            digest.update(block)
    return dict(kind=kind, mime=mime, path=str(path), digest=digest.hexdigest())


def tensor_references(images):
    references = []
    for image in images:
        interrupt()
        array = (image.detach().cpu().numpy().clip(0, 1) * 255).round().astype(np.uint8)
        data = io.BytesIO()
        Image.fromarray(array).save(data, format="PNG")
        value = data.getvalue()
        references.append(dict(kind="image", mime="image/png", data=value, digest=hashlib.sha256(value).hexdigest()))
    return references


def upload_reference(client, ref):
    if "data" in ref:
        return client.upload(io.BytesIO(ref["data"]), ref["mime"], interrupt)
    # Check content again before upload so a modified file cannot reuse a different job's hash.
    current = file_reference(ref["path"], ref["kind"])
    if current["digest"] != ref["digest"]:
        raise ValueError("Reference file changed during execution. Queue the workflow again.")
    with open(ref["path"], "rb") as source:
        return client.upload(source, ref["mime"], interrupt)


def save_outputs(client, owner, request_id, result, progress):
    output = Path(folder_paths.get_output_directory()) / "higgsfield" / owner[:16]
    output.mkdir(parents=True, exist_ok=True)
    base = hashlib.sha256(request_id.encode()).hexdigest()[:24]
    saved = {"images": [], "video": None, "request_id": request_id}
    media = [("images", item) for item in result.get("images", [])]
    if result.get("video"):
        media.append(("video", result["video"]))
    if not media:
        raise ValueError("Completed response has no image/video outputs. Request is saved for inspection in the Higgsfield console.")
    for index, (kind, item) in enumerate(media):
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str):
            raise ValueError("Invalid media output URL.")
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in ({".png", ".jpg", ".jpeg", ".webp"} if kind == "images" else {".mp4", ".mov"}):
            ext = ".png" if kind == "images" else ".mp4"
        path = output / f"{base}_{index}{ext}"
        if not path.exists():
            progress("downloading output", request_id)
            client.download(url, path, interrupt)
        if kind == "images":
            saved[kind].append(str(path))
        else:
            saved[kind] = str(path)
    progress("completed", request_id)
    return saved


def generate(model, payload, references, generation_id, timeout_seconds, unique_id, custom_factory=None):
    controls(generation_id, timeout_seconds)
    directory = state_directory()
    key, secret, owner = credentials(directory)
    client, jobs = Client(key, secret), Jobs(directory, owner)
    progress = progress_callback(unique_id)
    identity = {"parameters": payload, "generation_id": generation_id,
                "references": [{k: ref[k] for k in ("kind", "mime", "digest")} for ref in references]}
    try:
        def make_payload():
            progress("uploading references" if references else "preparing", "")
            if custom_factory:
                return custom_factory(client)
            return attach_references(model, payload, references, lambda ref: upload_reference(client, ref))
        request_id, result = run(client, jobs, model, identity, make_payload, timeout_seconds, progress, interrupt)
        return save_outputs(client, owner, request_id, result, progress)
    except Exception:
        progress("stopped - see error; request retained when accepted", "")
        raise
    finally:
        jobs.close()
        client.close()


def image_result(saved):
    if not saved["images"]:
        raise ValueError("This request did not produce images.")
    tensors, previews = [], []
    for filename in saved["images"]:
        with Image.open(filename) as image:
            rgb = ImageOps.exif_transpose(image).convert("RGB")
            tensors.append(torch.from_numpy(np.array(rgb).astype(np.float32) / 255.0).unsqueeze(0))
        relative = Path(filename).relative_to(folder_paths.get_output_directory())
        previews.append(dict(filename=relative.name, subfolder=relative.parent.as_posix(), type="output"))
    return {"ui": {"images": previews}, "result": (tensors, json.dumps(saved["images"]), saved["request_id"])}


def video_result(saved):
    if not saved["video"]:
        raise ValueError("This request did not produce a video.")
    relative = Path(saved["video"]).relative_to(folder_paths.get_output_directory())
    preview = ui.PreviewVideo([dict(filename=relative.name, subfolder=relative.parent.as_posix(), type="output")])
    return {"ui": preview.as_dict(), "result": (InputImpl.VideoFromFile(saved["video"]), saved["video"], saved["request_id"])}


class ReferenceImages:
    CATEGORY = CATEGORY + "/References"
    FUNCTION = "build"
    RETURN_TYPES = ("HF_REFERENCES",)
    RETURN_NAMES = ("references",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": ("IMAGE",)}, "optional": {"previous": ("HF_REFERENCES",)}}

    def build(self, images, previous=None):
        return (list(previous or []) + tensor_references(images),)


class ReferenceFile:
    CATEGORY = CATEGORY + "/References"
    FUNCTION = "build"
    RETURN_TYPES = ("HF_REFERENCES",)
    RETURN_NAMES = ("references",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"file_path": ("STRING", {"default": "", "tooltip": "Local PNG, JPEG, WebP, MP4, WAV, or MP3 path. Uploaded only when generation runs."})},
                "optional": {"previous": ("HF_REFERENCES",)}}

    @classmethod
    def IS_CHANGED(cls, file_path, **kwargs):
        path = Path(file_path.strip().strip('"'))
        if path.is_file():
            stat = path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        return "missing"

    def build(self, file_path, previous=None):
        path = Path(file_path.strip().strip('"'))
        suffix = path.suffix.lower()
        kind = "video" if suffix == ".mp4" else "audio" if suffix in {".wav", ".mp3"} else "image"
        return (list(previous or []) + [file_reference(path, kind)],)


class ImageGenerate:
    CATEGORY = CATEGORY
    FUNCTION = "execute"
    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("images", "saved_paths", "request_id")
    OUTPUT_IS_LIST = (True, False, False)
    OUTPUT_NODE = True
    DESCRIPTION = "Generate or edit images with your local Higgsfield API credentials. References are optional."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (list(IMAGE_MODELS),), "prompt": ("STRING", {"multiline": True}),
            "resolution": (["1k", "2k", "4k"], {"default": "2k"}),
            "aspect_ratio": (IMAGE_RATIOS,), "quality": (["high", "low", "medium", "xhigh", "max"],),
            **CONTROL_INPUTS}, "optional": {"references": ("HF_REFERENCES",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Run the durable job lookup even when Comfy's in-memory cache contains this node.
        return float("nan")

    def execute(self, model, prompt, resolution, aspect_ratio, quality, generation_id, timeout_seconds, references=None, unique_id=None):
        refs = references or []
        payload = image_input(model, prompt, resolution, aspect_ratio, quality, refs)
        saved = generate(IMAGE_MODELS[model], payload, refs, generation_id, timeout_seconds, unique_id)
        return image_result(saved)


class VideoGenerate:
    CATEGORY = CATEGORY
    FUNCTION = "execute"
    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "saved_path", "request_id")
    OUTPUT_NODE = True
    DESCRIPTION = "Seedance video generation. Image to Video uses start/end images; Reference to Video accepts image/video/audio references."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (list(VIDEO_MODELS),), "prompt": ("STRING", {"multiline": True}),
            "duration": ("INT", {"default": 5, "min": 4, "max": 30}),
            "resolution": (["720p", "480p", "1080p", "4k"],), "aspect_ratio": (RATIOS,),
            "generate_audio": ("BOOLEAN", {"default": True}), "output_format": (["mp4", "mov"],),
            **CONTROL_INPUTS}, "optional": {"references": ("HF_REFERENCES",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, model, prompt, duration, resolution, aspect_ratio, generate_audio, output_format,
                generation_id, timeout_seconds, references=None, unique_id=None):
        refs = references or []
        payload = video_input(model, prompt, duration, resolution, aspect_ratio, generate_audio, output_format, refs)
        saved = generate(VIDEO_MODELS[model], payload, refs, generation_id, timeout_seconds, unique_id)
        return video_result(saved)


class VideoMinimaxH3:
    CATEGORY = CATEGORY
    FUNCTION = "execute"
    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "saved_path", "request_id")
    OUTPUT_NODE = True
    DESCRIPTION = (
        "MiniMax H3 via Higgsfield API (minimax/h3/*). Resolution is fixed to 2K per docs. "
        "I2V: start (+ optional end) image. Ref2V: image and/or video refs; audio needs image or video."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (list(MINIMAX_H3_MODELS),),
            "prompt": ("STRING", {"multiline": True}),
            "duration": ("INT", {"default": 5, "min": 5, "max": 15}),
            "aspect_ratio": (H3_RATIOS, {"default": "auto"}),
            "aigc_watermark": ("BOOLEAN", {"default": False}),
            **CONTROL_INPUTS}, "optional": {"references": ("HF_REFERENCES",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, model, prompt, duration, aspect_ratio, aigc_watermark,
                generation_id, timeout_seconds, references=None, unique_id=None):
        refs = references or []
        payload = minimax_h3_input(model, prompt, duration, aspect_ratio, aigc_watermark, refs)
        saved = generate(MINIMAX_H3_MODELS[model], payload, refs, generation_id, timeout_seconds, unique_id)
        return video_result(saved)


class VideoWan30:
    CATEGORY = CATEGORY
    FUNCTION = "execute"
    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "saved_path", "request_id")
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Alibaba Wan 3.0 via Higgsfield API (alibaba/wan-3.0/*). "
        "I2V: first frame (+ optional last). Ref2V: up to 10 images / 5 videos / 5 audio. Seed 0 is omitted."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": (list(WAN30_MODELS),),
            "prompt": ("STRING", {"multiline": True}),
            "duration": ("INT", {"default": 5, "min": 2, "max": 30}),
            "resolution": (["1080p", "720p", "480p"],),
            "aspect_ratio": (WAN_RATIOS, {"default": "adaptive"}),
            "generate_audio": ("BOOLEAN", {"default": True}),
            "enable_thinking": ("BOOLEAN", {"default": False}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 2147483647}),
            **CONTROL_INPUTS}, "optional": {"references": ("HF_REFERENCES",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, model, prompt, duration, resolution, aspect_ratio, generate_audio, enable_thinking, seed,
                generation_id, timeout_seconds, references=None, unique_id=None):
        refs = references or []
        payload = wan30_input(model, prompt, duration, resolution, aspect_ratio, generate_audio, enable_thinking, seed, refs)
        saved = generate(WAN30_MODELS[model], payload, refs, generation_id, timeout_seconds, unique_id)
        return video_result(saved)


class AdvancedGenerate:
    CATEGORY = CATEGORY + "/Advanced"
    FUNCTION = "execute"
    RETURN_TYPES = ("HF_RESULT", "STRING")
    RETURN_NAMES = ("result", "request_id")
    OUTPUT_NODE = True
    DESCRIPTION = "Use another documented Higgsfield image/video endpoint. Supply its exact JSON schema. Reference placeholders: $ref1, $ref2, etc."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model_id": ("STRING", {"default": "bytedance/seedance-2.0/text-to-video"}),
            "parameters_json": ("STRING", {"multiline": True, "default": '{"prompt":"A cinematic coastal road","resolution":"720p","duration":5,"aspect_ratio":"16:9","generate_audio":true}'}),
            **CONTROL_INPUTS}, "optional": {"references": ("HF_REFERENCES",)}, "hidden": {"unique_id": "UNIQUE_ID"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, model_id, parameters_json, generation_id, timeout_seconds, references=None, unique_id=None):
        endpoint(model_id)
        try:
            payload = json.loads(parameters_json, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except ValueError:
            raise ValueError("parameters_json must be valid JSON without NaN or Infinity.") from None
        if not isinstance(payload, dict) or not payload:
            raise ValueError("parameters_json must be a non-empty JSON object matching this model's documentation.")
        refs = references or []
        used = set()

        def replace(value, urls):
            if isinstance(value, str) and value.startswith("$ref"):
                try:
                    index = int(value[4:]) - 1
                except ValueError:
                    raise ValueError("Use reference placeholders $ref1, $ref2, etc.") from None
                if index < 0 or index >= len(refs):
                    raise ValueError(f"Missing reference for {value}.")
                used.add(index)
                return urls[index] if urls else value
            if isinstance(value, list):
                return [replace(x, urls) for x in value]
            if isinstance(value, dict):
                return {k: replace(v, urls) for k, v in value.items()}
            return value

        replace(payload, [])
        if len(used) != len(refs):
            raise ValueError("Every connected reference must be used in parameters_json (for example image_urls: [\"$ref1\"]).")

        def factory(client):
            urls = [upload_reference(client, ref) for ref in refs]
            return replace(payload, urls)
        saved = generate(model_id, payload, refs, generation_id, timeout_seconds, unique_id, factory)
        return (saved, saved["request_id"])


class ResumeRequest:
    CATEGORY = CATEGORY + "/Advanced"
    FUNCTION = "execute"
    RETURN_TYPES = ("HF_RESULT",)
    RETURN_NAMES = ("result",)
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"request_id": ("STRING",), "timeout_seconds": CONTROL_INPUTS["timeout_seconds"]},
                "hidden": {"unique_id": "UNIQUE_ID"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, request_id, timeout_seconds, unique_id=None):
        controls("resume", timeout_seconds)
        directory = state_directory()
        key, secret, owner = credentials(directory)
        client, jobs = Client(key, secret), Jobs(directory, owner)
        progress = progress_callback(unique_id)
        try:
            job = jobs.by_request(request_id)
            request_id, result = resume(client, jobs, job, timeout_seconds, progress, interrupt)
            return (save_outputs(client, owner, request_id, result, progress),)
        finally:
            jobs.close()
            client.close()


class ResultImages:
    CATEGORY = CATEGORY + "/Advanced"
    FUNCTION = "execute"
    RETURN_TYPES, RETURN_NAMES = ImageGenerate.RETURN_TYPES, ImageGenerate.RETURN_NAMES
    OUTPUT_IS_LIST = (True, False, False)
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"result": ("HF_RESULT",)}}

    def execute(self, result):
        return image_result(result)


class ResultVideo:
    CATEGORY = CATEGORY + "/Advanced"
    FUNCTION = "execute"
    RETURN_TYPES, RETURN_NAMES = VideoGenerate.RETURN_TYPES, VideoGenerate.RETURN_NAMES
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"result": ("HF_RESULT",)}}

    def execute(self, result):
        return video_result(result)


NODE_CLASS_MAPPINGS = {"HFImageGenerate": ImageGenerate, "HFVideoGenerate": VideoGenerate,
                       "HFVideoMinimaxH3": VideoMinimaxH3, "HFVideoWan30": VideoWan30,
                       "HFReferenceImages": ReferenceImages, "HFReferenceFile": ReferenceFile,
                       "HFAdvancedGenerate": AdvancedGenerate, "HFResumeRequest": ResumeRequest,
                       "HFResultImages": ResultImages, "HFResultVideo": ResultVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"HFImageGenerate": "Higgsfield - Generate / Edit Image",
                               "HFVideoGenerate": "Higgsfield - Generate Video (Seedance)",
                               "HFVideoMinimaxH3": "Higgsfield - MiniMax H3 Video",
                               "HFVideoWan30": "Higgsfield - Wan 3.0 Video",
                               "HFReferenceImages": "Higgsfield - Reference Images",
                               "HFReferenceFile": "Higgsfield - Reference File",
                               "HFAdvancedGenerate": "Higgsfield - Custom Model (Advanced)",
                               "HFResumeRequest": "Higgsfield - Resume Request",
                               "HFResultImages": "Higgsfield - Result Images",
                               "HFResultVideo": "Higgsfield - Result Video"}
