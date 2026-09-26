import hashlib
import json
import sqlite3
from pathlib import Path

from .client import APIError, TERMINAL, request_status_url


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class Jobs:
    def __init__(self, directory, owner):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.owner = owner
        self.db = sqlite3.connect(str(Path(directory) / "requests.sqlite3"), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            owner TEXT NOT NULL, token TEXT NOT NULL, model TEXT NOT NULL,
            status TEXT NOT NULL, request_id TEXT, status_url TEXT, result TEXT,
            correlation TEXT, PRIMARY KEY(owner, token))""")
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, token):
        row = self.db.execute("SELECT * FROM jobs WHERE owner=? AND token=?", (self.owner, token)).fetchone()
        return dict(row) if row else None

    def by_request(self, request_id):
        row = self.db.execute("SELECT * FROM jobs WHERE owner=? AND request_id=?", (self.owner, request_id)).fetchone()
        if not row:
            raise ValueError("This request is not recorded for the configured local API account.")
        return dict(row)

    def claim(self, token, model):
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO jobs(owner,token,model,status) VALUES(?,?,?,'preparing')",
                                     (self.owner, token, model))
        return cursor.rowcount == 1

    def accepted(self, token, data, correlation):
        request_id, status_url = data.get("request_id"), data.get("status_url")
        # Persist the ID even if the rest of the response is malformed.
        if isinstance(request_id, str) and request_id:
            with self.db:
                self.db.execute("UPDATE jobs SET request_id=?,correlation=? WHERE owner=? AND token=?",
                                (request_id, correlation, self.owner, token))
        if not request_id:
            raise APIError("Submission response is incomplete. Check your Higgsfield request history before starting another generation.")
        status_url = request_status_url(request_id, status_url)
        with self.db:
            self.db.execute("UPDATE jobs SET status_url=?,status='queued' WHERE owner=? AND token=?",
                            (status_url, self.owner, token))

    def recover_status_url(self, job):
        # Older versions saved the accepted ID before rejecting the response URL.
        # Recover that same job; never issue another generation POST.
        if job["request_id"] and not job["status_url"] and job["status"] == "submitting":
            with self.db:
                self.db.execute("UPDATE jobs SET status_url=?,status='queued' WHERE owner=? AND token=? AND status='submitting' AND status_url IS NULL",
                                (request_status_url(job["request_id"]), self.owner, job["token"]))
            return self.get(job["token"])
        return job

    def update(self, token, status, result=None, correlation=None):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,result=?,correlation=COALESCE(?,correlation) WHERE owner=? AND token=?",
                            (status, json.dumps(result) if result else None, correlation, self.owner, token))

    def release(self, token):
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE owner=? AND token=?", (self.owner, token))


def run(client, jobs, model, identity, payload_factory, timeout, progress, interrupt):
    token = fingerprint({"model": model, "input": identity})
    if jobs.claim(token, model):
        try:
            interrupt()
            payload = payload_factory()
            interrupt()
        except BaseException:
            # Nothing was submitted; a retry cannot duplicate a generation.
            jobs.release(token)
            raise
        jobs.update(token, "submitting")
        progress("submitting", "")
        try:
            data, correlation = client.submit(model, payload)
        except APIError as error:
            if error.status in {400, 401, 403, 404, 422, 423, 429}:
                jobs.release(token)
            # Network failures and server errors are ambiguous: never replay the POST.
            raise
        jobs.accepted(token, data, correlation)
    return resume(client, jobs, jobs.get(token), timeout, progress, interrupt)


def resume(client, jobs, job, timeout, progress, interrupt):
    job = jobs.recover_status_url(job)
    if job["status"] in {"preparing", "submitting"} or not job["status_url"]:
        raise APIError("This generation is being submitted, or a previous submission was interrupted. "
                       "It will not be submitted again automatically. Check the Higgsfield console. "
                       "Only change generation_id if you intend a new paid generation.")
    if job["status"] in TERMINAL:
        result = json.loads(job["result"])
    else:
        result = client.wait(job, timeout,
                             lambda status, data, corr: jobs.update(job["token"], status, data, corr),
                             progress, interrupt)
    if result["status"] != "completed":
        raise APIError(f"Higgsfield generation ended: {result['status']}. Request {job['request_id']}. "
                       "Change generation_id only to request a new generation.")
    return job["request_id"], result
