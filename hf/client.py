import ipaddress
import random
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests

BASE = "https://api.higgsfield.ai"
TERMINAL = {"completed", "failed", "nsfw", "canceled"}


class APIError(RuntimeError):
    def __init__(self, message, status=None, correlation=None):
        super().__init__(message)
        self.status = status
        self.correlation = correlation


def public_url(url, resolve=True):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError("Media URLs must be public HTTPS URLs on port 443.")
    if resolve:
        try:
            addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise ValueError("Cannot resolve media host.") from None
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Private/local media URLs are not supported; use a local reference file instead.")
    return url


def api_url(url):
    p = urlparse(url)
    if p.scheme != "https" or p.netloc != "api.higgsfield.ai" or p.username or p.password or p.fragment:
        raise ValueError("The API returned an unexpected status URL; credentials were not sent.")
    if not re.fullmatch(r"/requests/[A-Za-z0-9_-]+/(status|cancel)", p.path) or p.query:
        raise ValueError("Unexpected Higgsfield request URL.")
    return url


def request_status_url(request_id, returned_url=None):
    """Use the documented production status endpoint when a returned URL is unusable."""
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
        raise ValueError("Higgsfield returned an invalid request ID.")
    expected_path = f"/requests/{request_id}/status"
    canonical = BASE + expected_path
    if not isinstance(returned_url, str) or not returned_url:
        return canonical
    candidate = BASE + returned_url if returned_url.startswith("/requests/") else returned_url
    try:
        approved = api_url(candidate)
    except ValueError:
        # Never send credentials to a host supplied by an unverified response URL.
        return canonical
    if urlparse(approved).path != expected_path:
        raise APIError("Submission request ID and status URL do not match.")
    return approved


def endpoint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)+", value):
        raise ValueError("Use a model ID such as bytedance/seedance-2.5/text-to-video, not a URL.")
    if any(part in (".", "..") for part in value.split("/")) or value.split("/")[0] in {"requests", "files"}:
        raise ValueError("Expected a generation model endpoint.")
    return value


class Client:
    def __init__(self, key, secret, session=None, clock=time.monotonic, sleep=time.sleep):
        self.auth = {"Authorization": f"Key {key}:{secret}", "Content-Type": "application/json"}
        # No automatic retries, including generation POSTs.
        self.session = session or requests.Session()
        self.clock, self.sleep = clock, sleep

    def close(self):
        self.session.close()

    def json(self, method, url, payload=None, timeout=30):
        try:
            response = self.session.request(method, url, headers=self.auth, json=payload,
                                            timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            raise APIError("Higgsfield connection failed or timed out. A generation submission may have been accepted.") from None
        correlation = response.headers.get("X-Correlation-ID")
        with response:
            code = response.status_code
            if not 200 <= code < 300:
                messages = {400: "Invalid parameters or account concurrency limit reached.",
                            401: "Invalid Higgsfield API key or secret.", 403: "Access denied or insufficient credits.",
                            404: "Model/request unavailable to this API account.", 422: "Model input validation failed.",
                            423: "This model is temporarily blocked.", 429: "Rate limit reached; wait before retrying.",
                            503: "This model is disabled or not ready."}
                # Do not echo arbitrary provider bodies, signed URLs, credentials, or prompts into logs.
                raise APIError(f"Higgsfield HTTP {code}: {messages.get(code, 'API request failed.')}", code, correlation)
            try:
                data = response.json()
            except ValueError:
                raise APIError("Higgsfield returned invalid JSON.", correlation=correlation) from None
            if not isinstance(data, dict):
                raise APIError("Unexpected Higgsfield response.", correlation=correlation)
            return data, correlation

    def submit(self, model, payload):
        return self.json("POST", BASE + "/" + endpoint(model), payload)

    def wait(self, job, timeout, update, progress, interrupt):
        deadline = self.clock() + timeout
        delay, failures = 2.0, 0
        url = request_status_url(job["request_id"], job["status_url"])
        while True:
            interrupt()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError(f"Generation is still pending. Request {job['request_id']}. Queue again with the same generation_id to resume.")
            try:
                result, correlation = self.json("GET", url, timeout=min(30, remaining))
            except APIError as error:
                if error.status is not None and error.status < 500 and error.status != 429:
                    raise
                failures += 1
                if failures > 8:
                    raise APIError(f"Status checks failed repeatedly. Request {job['request_id']} is saved; queue again to resume.") from None
                progress("Connection interrupted; retrying status", job["request_id"])
            else:
                failures = 0
                status = result.get("status")
                if result.get("request_id", job["request_id"]) != job["request_id"]:
                    raise APIError("Status response request ID mismatch.")
                if status not in TERMINAL | {"queued", "in_progress"}:
                    raise APIError("Unknown generation status; saved request can be resumed after checking the API docs.")
                update(status, result, correlation)
                progress(status, job["request_id"])
                if status in TERMINAL:
                    return result
            until = min(deadline, self.clock() + delay + random.uniform(0, 0.5))
            while self.clock() < until:
                interrupt()
                self.sleep(min(0.25, until - self.clock()))
            delay = min(delay * 1.5, 10.0)

    def upload(self, source, mime, interrupt=lambda: None):
        interrupt()
        data, _ = self.json("POST", BASE + "/files/generate-upload-url", {"content_type": mime})
        upload = public_url(data.get("upload_url", ""))
        public = public_url(data.get("public_url", ""))
        headers = data.get("upload_headers")
        if not isinstance(headers, dict) or any(k.lower() in {"authorization", "cookie", "host"} for k in headers):
            raise APIError("Invalid upload headers.")
        # A separate request never receives the API authorization header.
        try:
            with requests.put(upload, data=source, headers=headers, timeout=(15, 120), allow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    raise APIError(f"Reference upload failed (HTTP {response.status_code}).")
        except requests.RequestException:
            raise APIError("Reference upload failed. No generation was submitted.") from None
        interrupt()
        return public

    def download(self, url, target, interrupt=lambda: None, limit=1024 * 1024 * 1024):
        target = Path(target)
        part = target.with_suffix(target.suffix + ".part")
        start = self.clock()
        try:
            # Redirects are handled explicitly so every destination is checked.
            for _ in range(6):
                public_url(url)
                with requests.get(url, stream=True, timeout=(15, 30), allow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status_code != 200:
                        raise APIError(f"Output download failed (HTTP {response.status_code}); request is saved.")
                    size = 0
                    with part.open("wb") as out:
                        for chunk in response.iter_content(1024 * 1024):
                            interrupt()
                            size += len(chunk)
                            if size > limit or self.clock() - start > 600:
                                raise APIError("Output exceeds the local download size/time limit.")
                            out.write(chunk)
                    if not size:
                        raise APIError("The generated file was empty.")
                    part.replace(target)
                    return target
            raise APIError("Too many output redirects.")
        except requests.RequestException:
            raise APIError("Output download interrupted; queue again to download the saved result.") from None
        finally:
            part.unlink(missing_ok=True)
