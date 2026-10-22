"""Code-interpreter tool with three interchangeable backends:
local (rlimited subprocess) | http (network-isolated sandbox container) | e2b (managed microVMs)."""

import base64
from typing import Any

import httpx

from app import db
from app.config import get_settings
from app.sandbox_runner import run_python
from app.tools.base import Tool, ToolContext, ToolOutput

MAX_CODE_CHARS = 20_000


async def _execute(code: str, files: dict[str, bytes]) -> dict:
    s = get_settings()
    if s.sandbox_backend == "http":
        async with httpx.AsyncClient(timeout=s.sandbox_timeout_s + 10) as client:
            r = await client.post(
                f"{s.sandbox_url}/run",
                json={"code": code, "timeout_s": s.sandbox_timeout_s, "files": {n: base64.b64encode(d).decode() for n, d in files.items()}},
                headers={"X-Sandbox-Token": s.sandbox_token or ""},
            )
            r.raise_for_status()
            return r.json()
    if s.sandbox_backend == "e2b":
        from e2b_code_interpreter import AsyncSandbox  # optional dependency

        sbx = await AsyncSandbox.create(api_key=s.e2b_api_key, timeout=s.sandbox_timeout_s + 30)
        try:
            for name, data in files.items():
                await sbx.files.write(f"/home/user/{name}", data)
            ex = await sbx.run_code(code, timeout=s.sandbox_timeout_s)
            images = [
                {"name": f"out{i}.png", "mime": "image/png", "b64": r.png} for i, r in enumerate(ex.results) if getattr(r, "png", None)
            ]
            text_results = [r.text for r in ex.results if getattr(r, "text", None)]
            return {
                "stdout": "".join(ex.logs.stdout) + ("\n".join(text_results) if text_results else ""),
                "stderr": "".join(ex.logs.stderr) + (f"{ex.error.name}: {ex.error.value}" if ex.error else ""),
                "exit_code": 1 if ex.error else 0,
                "timed_out": False,
                "images": images,
            }
        finally:
            await sbx.kill()
    return await run_python(code, s.sandbox_timeout_s, files)


async def python_exec(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
    code = str(args.get("code", ""))
    if not code.strip():
        return ToolOutput("Error: empty code", error=True)
    if len(code) > MAX_CODE_CHARS:
        return ToolOutput(f"Error: code exceeds {MAX_CODE_CHARS} chars", error=True)
    from app.rag import data_files

    async with db.sessionmaker()() as session:
        files = await data_files(session, ctx.user_id)
    res = await _execute(code, files)
    parts = []
    if res.get("stdout"):
        parts.append(f"stdout:\n{res['stdout']}")
    if res.get("stderr"):
        parts.append(f"stderr:\n{res['stderr']}")
    if res.get("timed_out"):
        parts.append("Execution timed out (CPU or wall-clock limit).")
    images = [{"mime": i["mime"], "b64": i["b64"], "name": i.get("name", "")} for i in res.get("images", [])]
    if images:
        parts.append(f"Generated {len(images)} image(s): {', '.join(i['name'] for i in images)} (shown to the user).")
    parts.append(f"exit_code={res.get('exit_code')}")
    failed = res.get("exit_code") not in (0, None) or res.get("timed_out")
    return ToolOutput("\n".join(parts), images=images, error=bool(failed))


TOOL = Tool(
    name="python_exec",
    description=(
        "Execute Python 3 code in an isolated sandbox and return stdout/stderr. Use for math, data analysis, "
        "parsing, simulations and charts. numpy, pandas and matplotlib are available. Save charts with "
        "plt.savefig('name.png') - saved images are shown to the user automatically. Always print() results. "
        "No internet access inside the sandbox. State does not persist between calls. "
        "The user's uploaded data files (csv, xlsx, json, txt...) are in the working directory under their "
        "original filenames, e.g. pd.read_csv('sales.csv')."
    ),
    parameters={
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Complete, self-contained Python program."}},
        "required": ["code"],
    },
    fn=python_exec,
    timeout_s=60,
)
