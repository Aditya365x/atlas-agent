"""Isolated Python execution: separate process, rlimits, wall-clock timeout, scrubbed env,
throwaway working dir. Produced charts (png/jpg/svg) are returned as base64.

Used directly in `local` mode and inside the network-isolated `sandbox` container in `http` mode.
Dependency-free on purpose so the sandbox image stays tiny.
"""

from __future__ import annotations

import asyncio
import base64
import os
import resource
import signal
import sys
import tempfile
from pathlib import Path

MAX_OUTPUT = 8_000
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 2_000_000
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}


def _limits(cpu_s: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)  # 1.5 GB address space
    resource.setrlimit(resource.RLIMIT_FSIZE, (25 * 1024 * 1024,) * 2)  # max 25 MB per written file
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _collect_images(tmp: str) -> list[dict]:
    images = []
    for p in sorted(Path(tmp).rglob("*")):
        if p.suffix.lower() in _MIME and p.is_file() and p.stat().st_size <= MAX_IMAGE_BYTES:
            images.append({"name": p.name, "mime": _MIME[p.suffix.lower()], "b64": base64.b64encode(p.read_bytes()).decode()})
            if len(images) >= MAX_IMAGES:
                break
    return images


def _clip(s: bytes) -> str:
    text = s.decode("utf-8", errors="replace")
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n...[truncated {len(text) - MAX_OUTPUT} chars]"


async def run_python(code: str, timeout_s: int = 20) -> dict:
    with tempfile.TemporaryDirectory(prefix="atlas-sbx-") as tmp:
        script = Path(tmp) / "main.py"
        script.write_text(code, encoding="utf-8")
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": tmp,
            "MPLBACKEND": "Agg",
            "PYTHONUNBUFFERED": "1",
            "MPLCONFIGDIR": tmp,
            "OPENBLAS_NUM_THREADS": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(script),
            cwd=tmp,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=lambda: _limits(timeout_s),
            start_new_session=True,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 2)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
        images = await asyncio.to_thread(_collect_images, tmp)
        return {
            "stdout": _clip(out),
            "stderr": _clip(err),
            "exit_code": proc.returncode,
            "timed_out": timed_out or proc.returncode == -signal.SIGXCPU,
            "images": images,
        }
