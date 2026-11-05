"""Capability manifest: what the grading agent may and may not do."""
from __future__ import annotations

from collections.abc import Callable

PERMISSIONS = [
    {"id": "sandbox:exec", "granted": True, "label": "compile and run submissions inside the offline Docker sandbox"},
    {"id": "fs:read", "granted": True, "label": "read problems, rules, submissions and the similarity corpus"},
    {"id": "fs:write", "granted": True, "label": "write reports/ and the LLM response cache only"},
    {"id": "llm:local", "granted": True, "label": "call the local model (Ollama); no code leaves the machine"},
    {"id": "llm:remote", "granted": True, "label": "call a remote model (Groq), only if a key is configured"},

    {"id": "exec:host", "granted": False, "label": "run student code directly on the host machine"},
    {"id": "net:sandbox", "granted": False, "label": "give the sandbox network access"},
    {"id": "grade:llm", "granted": False,
     "label": "let the language model set the final grade or any metric other than its own readability score"},
]

GRANT = {p["id"]: p["granted"] for p in PERMISSIONS}


class PermissionError(Exception):
    def __init__(self, capability: str):
        super().__init__(f'DENIED by manifest: capability "{capability}" is not granted.')
        self.capability = capability


def check_permission(capability: str, log: Callable[[dict], None] | None = None) -> bool:
    granted = GRANT.get(capability) is True
    if log:
        log({
            "kind": "perm-allow" if granted else "perm-deny",
            "msg": f'permission "{capability}" -> {"ALLOW" if granted else "DENY (not in manifest)"}',
        })
    if not granted:
        raise PermissionError(capability)
    return True
