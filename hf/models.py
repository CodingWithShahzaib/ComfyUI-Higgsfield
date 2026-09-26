IMAGE_MODELS = {
    "GPT Image 2.5 Sunburst (Higgsfield)": "marketing-studio/image/sunburst",
    "Marketing Studio Image 2.0 Alpha": "marketing-studio/image",
}
VIDEO_MODELS = {
    "Seedance 2.5 - Reference to Video": "bytedance/seedance-2.5/reference-to-video",
    "Seedance 2.5 - Image to Video": "bytedance/seedance-2.5/image-to-video",
    "Seedance 2.5 - Text to Video": "bytedance/seedance-2.5/text-to-video",
    "Seedance 2.0 - Text to Video": "bytedance/seedance-2.0/text-to-video",
}
MINIMAX_H3_MODELS = {
    "MiniMax H3 - Text to Video": "minimax/h3/text-to-video",
    "MiniMax H3 - Image to Video": "minimax/h3/image-to-video",
    "MiniMax H3 - Reference to Video": "minimax/h3/reference-to-video",
}
WAN30_MODELS = {
    "Wan 3.0 - Text to Video": "alibaba/wan-3.0/text-to-video",
    "Wan 3.0 - Image to Video": "alibaba/wan-3.0/image-to-video",
    "Wan 3.0 - Reference to Video": "alibaba/wan-3.0/reference-to-video",
}
RATIOS = ["16:9", "4:3", "1:1", "3:4", "9:16", "21:9"]
IMAGE_RATIOS = ["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"]
H3_RATIOS = ["auto", "adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
WAN_RATIOS = ["adaptive", "16:9", "4:3", "1:1", "3:4", "9:16"]


def choice(value, options, name):
    if value not in options:
        raise ValueError(f"{name} must be one of: {', '.join(str(x) for x in options)}")
    return value


def controls(generation_id, timeout):
    if not isinstance(generation_id, str) or not generation_id.strip() or len(generation_id) > 200:
        raise ValueError("generation_id must contain 1 to 200 characters.")
    if type(timeout) is not int or not 30 <= timeout <= 7200:
        raise ValueError("timeout_seconds must be between 30 and 7200.")


def require_prompt(prompt, required=True):
    if not isinstance(prompt, str):
        raise ValueError("prompt must be text.")
    text = prompt.strip()
    if required and not text:
        raise ValueError("Enter a video prompt.")
    return text


def image_input(model, prompt, resolution, aspect_ratio, quality, references):
    choice(model, IMAGE_MODELS, "model")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Enter an image prompt.")
    choice(resolution, ["1k", "2k", "4k"], "resolution")
    choice(aspect_ratio, IMAGE_RATIOS, "aspect_ratio")
    if any(r["kind"] != "image" for r in references):
        raise ValueError("Image generation accepts image references only.")
    if model.startswith("GPT") and len(references) > 16:
        raise ValueError("Sunburst accepts at most 16 reference images.")
    payload = dict(prompt=prompt, resolution=resolution, aspect_ratio=aspect_ratio, enhance_prompt=False)
    if model.startswith("GPT"):
        payload["quality"] = choice(quality, ["low", "medium", "high", "xhigh", "max"], "quality")
    elif quality != "high":
        raise ValueError("Alpha uses its API default quality. Choose high, or use Sunburst for adjustable quality.")
    return payload


def video_input(model, prompt, duration, resolution, aspect_ratio, generate_audio, output_format, references):
    choice(model, VIDEO_MODELS, "model")
    route = VIDEO_MODELS[model]
    is20 = "2.0" in route
    if type(duration) is not int or not 4 <= duration <= (15 if is20 else 30):
        raise ValueError(f"duration must be 4 to {15 if is20 else 30} whole seconds.")
    choice(resolution, ["480p", "720p", "1080p", "4k"] if is20 else ["480p", "720p"], "resolution")
    choice(aspect_ratio, RATIOS, "aspect_ratio")
    choice(output_format, ["mp4"] if is20 else ["mp4", "mov"], "output_format")
    if type(generate_audio) is not bool:
        raise ValueError("generate_audio must be true or false.")
    payload = dict(duration=duration, resolution=resolution, generate_audio=generate_audio)
    if not isinstance(prompt, str):
        raise ValueError("prompt must be text.")
    if prompt.strip():
        payload["prompt"] = prompt
    if route.endswith("text-to-video"):
        if not prompt.strip():
            raise ValueError("Text to Video requires a prompt.")
        if references:
            raise ValueError("Select Image to Video or Reference to Video to use references.")
    elif route.endswith("image-to-video"):
        if not 1 <= len(references) <= 2 or any(r["kind"] != "image" for r in references):
            raise ValueError("Image to Video needs one start image and optionally one end image, in that order.")
        if aspect_ratio != "16:9":
            raise ValueError("Image to Video derives its ratio from the image. Leave aspect_ratio at its default 16:9 (not sent).")
    elif not references:
        raise ValueError("Add image, video, or audio references, or choose Text to Video.")
    if not route.endswith("image-to-video"):
        payload["aspect_ratio"] = aspect_ratio
    if not is20:
        payload["output_format"] = output_format
    return payload


def minimax_h3_input(model, prompt, duration, aspect_ratio, aigc_watermark, references):
    """Build payload for minimax/h3/{text,image,reference}-to-video (docs.higgsfield.ai)."""
    choice(model, MINIMAX_H3_MODELS, "model")
    route = MINIMAX_H3_MODELS[model]
    text = require_prompt(prompt, required=True)
    if type(duration) is not int or not 5 <= duration <= 15:
        raise ValueError("MiniMax H3 duration must be 5 to 15 whole seconds.")
    choice(aspect_ratio, H3_RATIOS, "aspect_ratio")
    if type(aigc_watermark) is not bool:
        raise ValueError("aigc_watermark must be true or false.")
    payload = dict(
        prompt=text,
        duration=duration,
        resolution="2K",
        aspect_ratio=aspect_ratio,
        aigc_watermark=aigc_watermark,
    )
    if route.endswith("text-to-video"):
        if references:
            raise ValueError("Select Image to Video or Reference to Video to use references.")
    elif route.endswith("image-to-video"):
        if not 1 <= len(references) <= 2 or any(r["kind"] != "image" for r in references):
            raise ValueError("Image to Video needs one start image and optionally one end image, in that order.")
    elif route.endswith("reference-to-video"):
        images = [r for r in references if r["kind"] == "image"]
        videos = [r for r in references if r["kind"] == "video"]
        audios = [r for r in references if r["kind"] == "audio"]
        if not images and not videos:
            raise ValueError("Reference to Video needs at least one image or video reference.")
        if audios and not images and not videos:
            raise ValueError("Audio references require an image or video reference.")
        if len(images) > 9:
            raise ValueError("MiniMax H3 accepts at most 9 reference images.")
        if len(videos) > 3:
            raise ValueError("MiniMax H3 accepts at most 3 reference videos.")
        if len(audios) > 3:
            raise ValueError("MiniMax H3 accepts at most 3 reference audio files.")
    else:
        raise ValueError(f"Unsupported MiniMax H3 route: {route}")
    return payload


def wan30_input(model, prompt, duration, resolution, aspect_ratio, generate_audio, enable_thinking, seed, references):
    """Build payload for alibaba/wan-3.0/{text,image,reference}-to-video (docs.higgsfield.ai)."""
    choice(model, WAN30_MODELS, "model")
    route = WAN30_MODELS[model]
    text = require_prompt(prompt, required=True)
    if type(duration) is not int or not 2 <= duration <= 30:
        raise ValueError("Wan 3.0 duration must be 2 to 30 whole seconds.")
    choice(resolution, ["480p", "720p", "1080p"], "resolution")
    choice(aspect_ratio, WAN_RATIOS, "aspect_ratio")
    if type(generate_audio) is not bool:
        raise ValueError("generate_audio must be true or false.")
    if type(enable_thinking) is not bool:
        raise ValueError("enable_thinking must be true or false.")
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("seed must be an integer from 0 to 2147483647.")
    payload = dict(
        prompt=text,
        duration=duration,
        resolution=resolution,
        aspect_ratio=aspect_ratio,
        generate_audio=generate_audio,
        enable_thinking=enable_thinking,
    )
    # Docs: schema accepts seed=0, but implementation only forwards a nonzero seed.
    if seed:
        payload["seed"] = seed
    if route.endswith("text-to-video"):
        if references:
            raise ValueError("Select Image to Video or Reference to Video to use references.")
    elif route.endswith("image-to-video"):
        if not 1 <= len(references) <= 2 or any(r["kind"] != "image" for r in references):
            raise ValueError("Image to Video needs one start image and optionally one end image, in that order.")
    elif route.endswith("reference-to-video"):
        images = [r for r in references if r["kind"] == "image"]
        videos = [r for r in references if r["kind"] == "video"]
        audios = [r for r in references if r["kind"] == "audio"]
        if len(images) > 10:
            raise ValueError("Wan 3.0 accepts at most 10 reference images.")
        if len(videos) > 5:
            raise ValueError("Wan 3.0 accepts at most 5 reference videos.")
        if len(audios) > 5:
            raise ValueError("Wan 3.0 accepts at most 5 reference audio files.")
    else:
        raise ValueError(f"Unsupported Wan 3.0 route: {route}")
    return payload


def attach_references(route, payload, references, upload):
    result = dict(payload)
    urls = [(ref["kind"], upload(ref)) for ref in references]
    if route.endswith("/image-to-video"):
        result["image_url"] = urls[0][1]
        if len(urls) == 2:
            result["end_image_url"] = urls[1][1]
    else:
        for kind in ("image", "video", "audio"):
            values = [url for k, url in urls if k == kind]
            if values:
                result[kind + "_urls"] = values
    return result
