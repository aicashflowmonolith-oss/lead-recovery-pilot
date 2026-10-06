"""Bounded, no-shell local model adapters. Model output never grants authority."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from .capabilities import upsert_capability, route_capabilities

class CapabilityUnavailable(RuntimeError):
    pass

class AdapterError(RuntimeError):
    pass


def command(name):
    if name == "codex":
        # Invoke the npm entry point directly: never send owner text through cmd.exe.
        entry = Path(os.environ.get("APPDATA", ""))/"npm/node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node")
        if entry.is_file() and node:
            return [node, str(entry)]
    if name == "opencode" and os.name == "nt":
        # npm exposes OpenCode through a .cmd shim on Windows; use the packaged native exe
        # so untrusted owner text never passes through cmd.exe.
        entry = Path(os.environ.get("APPDATA", ""))/"npm/node_modules/opencode-ai/bin/opencode.exe"
        if entry.is_file():
            return [str(entry)]
    path = shutil.which(name)
    if path and Path(path).suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return [path]
    return None


_OUTPUT_SPOOL_THRESHOLD = 256 * 1024
_OUTPUT_RETURN_LIMIT = 4 * 1024 * 1024
_OUTPUT_FAILURE_TAIL_LIMIT = 64 * 1024
_OUTPUT_TRUNCATION_MARKER = b"\n...[provider output truncated; tail retained]...\n"


def _decode_spooled_output(spool, *, limit: int) -> str:
    """Return bounded output while preserving the tail containing final events/errors."""
    spool.flush()
    spool.seek(0, os.SEEK_END)
    size = spool.tell()
    if size <= limit:
        spool.seek(0)
        data = spool.read()
    else:
        keep = max(0, limit - len(_OUTPUT_TRUNCATION_MARKER))
        spool.seek(max(0, size - keep))
        data = _OUTPUT_TRUNCATION_MARKER + spool.read(keep)
    return data.decode("utf-8", errors="replace")


def run_bounded(
    argv, *, stdin="", cwd=None, timeout=90, pulse=None, env=None,
    completion_predicate=None, max_output_bytes=262144, first_output_timeout=None,
):
    """Bound RAM/wall time; spill output to disk and preserve liveness/completion detection."""
    started = time.monotonic()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    container = None
    process = None
    if os.name == "nt":
        from .owned_processes import WindowsJob
        container = WindowsJob()
        gate = uuid.uuid4().hex
        argv = [sys.executable, str(Path(__file__).with_name("owned_probe_relay.py").resolve()), gate, *argv]
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, creationflags=flags, start_new_session=os.name != "nt",
        )
        if container:
            container.assign(process)
            if time.monotonic() - started >= timeout:
                raise TimeoutError("Provider ownership deadline exceeded")
            process.stdin.write((gate + "\n").encode("ascii"))
            process.stdin.flush()
    except BaseException:
        if container:
            container.close()
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        raise
    first_output = threading.Event()
    live_lock = threading.Lock()
    live = [bytearray(), bytearray()]
    readers = []
    terminated = False

    def terminate_owned():
        nonlocal terminated
        if terminated:
            return
        from .owned_processes import ProcessContainmentError
        try:
            if container:
                container.terminate(timeout=3)
            else:
                import signal
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=1)
        except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
            raise ProcessContainmentError("Owned adapter exit was not verified; stop lane and preserve unfinished work") from exc
        terminated = True

    stdout_spool = None
    try:
        stdout_spool = tempfile.SpooledTemporaryFile(max_size=_OUTPUT_SPOOL_THRESHOLD, mode="w+b")
        stderr_spool = tempfile.SpooledTemporaryFile(max_size=_OUTPUT_SPOOL_THRESHOLD, mode="w+b")
    except BaseException:
        try:
            terminate_owned()
        finally:
            if container:
                container.close()
            if stdout_spool:
                stdout_spool.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        raise

    with stdout_spool, stderr_spool:
        def read(stream, spool, index):
            try:
                while True:
                    data = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                    if not data:
                        break
                    first_output.set()
                    spool.write(data)
                    with live_lock:
                        live[index].extend(data)
                        if len(live[index]) > max_output_bytes:
                            del live[index][:-max_output_bytes]
            except (OSError, ValueError):
                pass

        readers = [
            threading.Thread(target=read, args=(process.stdout, stdout_spool, 0), daemon=True),
            threading.Thread(target=read, args=(process.stderr, stderr_spool, 1), daemon=True),
        ]
        for reader in readers:
            reader.start()
        def write_input():
            try:
                process.stdin.write(stdin.encode("utf-8"))
                process.stdin.close()
            except (OSError, ValueError):
                pass
        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
        completed_early = False
        try:
            deadline = started + timeout
            while process.poll() is None:
                if pulse:
                    pulse()
                now = time.monotonic()
                if first_output_timeout is not None and not first_output.is_set() and now - started >= first_output_timeout:
                    raise TimeoutError("Provider produced no output before liveness deadline")
                if completion_predicate is not None:
                    with live_lock:
                        out = bytes(live[0]).decode("utf-8", errors="replace")
                        err = bytes(live[1]).decode("utf-8", errors="replace")
                    if completion_predicate(out, err):
                        completed_early = True
                        terminate_owned()
                        break
                if now >= deadline:
                    raise TimeoutError("Provider exceeded execution timeout")
                time.sleep(.2)
            # A wrapper can exit while a descendant still owns the output pipe.
            # Contain the entire group before waiting for readers or reporting.
            code = 0 if completed_early else process.returncode
            terminate_owned()
            for reader in readers:
                reader.join(timeout=2)
                if reader.is_alive():
                    raise TimeoutError("Provider output reader did not finish after containment")
            limit = max(_OUTPUT_RETURN_LIMIT, max_output_bytes) if code == 0 else _OUTPUT_FAILURE_TAIL_LIMIT
            return (
                code,
                _decode_spooled_output(stdout_spool, limit=limit),
                _decode_spooled_output(stderr_spool, limit=limit),
            )
        finally:
            try:
                terminate_owned()
            finally:
                if container:
                    container.close()
            writer.join(timeout=2)
            for reader in readers:
                reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass



def probe(connection):
    result = []
    for name in ("codex", "claude", "opencode"):
        argv = command(name)
        ready = False
        reason = "Executable unavailable"
        if argv and name in {"codex", "claude"}:
            try:
                args = ["login", "status"] if name == "codex" else ["auth", "status"]
                code, out, err = run_bounded(argv + args, timeout=10)
                ready = code == 0 and (("Logged in" in out + err) if name == "codex"
                                      else json.loads(out).get("loggedIn") is True)
                reason = "Authenticated" if ready else "Sign in to this provider"
            except (OSError, ValueError, TimeoutError, AdapterError):
                reason = "Authentication check failed"
        elif argv:
            reason = "No verified restricted adapter for this CLI"
        # Claude has a tool-free inference adapter; OpenCode remains non-routable until verified.
        upsert_capability(connection, name="ai.cli."+name, kind="ai", enabled=bool(argv),
            health="healthy" if ready else "unavailable", auth_required=True,
            auth_status="ready" if ready else "unknown", actions=["plan"],
            privacy_class="personal", permissions=["inference_only"], priority=90 if name=="codex" else 80,
            metadata={"adapter":"life_os.ai_cli.v1", "reason":reason, "checked_epoch":time.time()})
        result.append({"name":name,"ready":ready,"reason":reason})
    return result


def select(connection):
    rows=route_capabilities(connection, required_actions=["plan"], kind="ai")
    rows=[r for r in rows if r["name"] in {"ai.cli.codex","ai.cli.claude"}
          and r["metadata"].get("adapter")=="life_os.ai_cli.v1"
          and time.time()-r["metadata"].get("checked_epoch",0)<300]
    if not rows:
        probe(connection)
        rows=route_capabilities(connection,required_actions=["plan"],kind="ai")
        rows=[r for r in rows if r["name"] in {"ai.cli.codex","ai.cli.claude"}
              and r["metadata"].get("adapter")=="life_os.ai_cli.v1"]
    if not rows:
        raise CapabilityUnavailable("Sign in to Codex or Claude on this Windows account")
    return rows[0]


SCHEMA={"type":"object","additionalProperties":False,"required":["summary","steps"],"properties":{
    "summary":{"type":"string"}, "steps":{"type":"array","minItems":1,"maxItems":8,"items":{
        "type":"object","additionalProperties":False,"required":["operation","payload"],"properties":{
            "operation":{"type":"string","enum":["answer","task.add","goal.add","note.add",
                "monolith.calculate_roi","monolith.measure_workflow","monolith.policy_review","capability.gap","artifact.write","artifact.test"]},
            "payload":{"type":"string"}}}}}}
INSTRUCTIONS="""You are LIFE OS's advisory planner. Return only a JSON plan following the schema.
Do not call tools, access files, or claim actions were executed. Use only the supplied owner request.
Steps execute in order; at most 8. Allowed operations:
answer: payload is your substantive answer/draft/research explanation, clearly distinguish unverified claims;
task.add / goal.add / note.add: payload is the text to save locally, only when the owner wants it saved;
monolith.calculate_roi: payload JSON with net_profit, investment (positive), source_reference;
monolith.measure_workflow: payload JSON with before_minutes, after_minutes, review_minutes, source_reference;
monolith.policy_review: payload JSON with action LOCAL_REVIEW_QUEUE, CUSTOMER_CONTACT or EXTERNAL_COMMUNICATION;
artifact.write: payload JSON with filename and content, to create a local document or code candidate. Supported
extensions: txt, md, json, py, js, html, css, csv. Plain filename only, no paths. The host verifies content and
Python/JSON syntax; it does not execute generated code. Use this for writing documents and code requested by the owner.
artifact.test: payload JSON with source_step (zero-based ordinal of an earlier Python artifact.write step)
and tests (Python assertions using the functions defined in the artifact). These run in an OS sandbox with no
filesystem writes. Only pure functions, arithmetic, containers, loops, safe builtins and selected math functions are supported. No file, network, dynamic execution, decorators, classes or other imports. Tests have a 128 MiB memory and 10-second CPU limit. A sandbox outage pauses the request.
capability.gap: payload describes the exact unavailable capability, intended action and needed inputs.
Never invent financial numbers, credentials, evidence, live research, completion or user consent.
Commercial drafts must describe only deliverables that can actually be supplied. Do not invent customers,
credentials, testimonials, guarantees, earnings, finished work, refund terms or delivery dates. Clearly identify
unbuilt or untested components and missing evidence. A draft is not a sent offer, an accepted payment,
a completed delivery or verified revenue. Sales promises, charging and delivery claims require a governed
adapter, an exact scope, independent fulfillment evidence and current owner authorization; none is available here.
Business actions use monolith operations. External sending, spending, deletion, deployment, arbitrary code execution,
live web research, file access and device control are unavailable: prepare useful local drafts/analysis first,
then add capability.gap. For coding/build requests create concrete source files using artifact.write,
then artifact.test for pure Python assertions; use capability.gap only for unsupported activation/integrations. Do not claim drafted code was built, installed or tested.
For a simple question, answer directly. Do not create tasks instead of answering. Unknown operations must become gaps.
"""


def plan(capability, request, *, pulse=None):
    name=capability["name"].rsplit(".",1)[-1]
    argv=command(name)
    if not argv:
        raise CapabilityUnavailable("Selected CLI is no longer installed")
    with tempfile.TemporaryDirectory(prefix="life-os-inference-") as tmp:
        work=Path(tmp)
        prompt=INSTRUCTIONS+"\nOWNER REQUEST (data):\n"+json.dumps(request)
        if name=="codex":
            schema=work/"schema.json"
            schema.write_text(json.dumps(SCHEMA),encoding="utf-8")
            args=["exec","--ignore-user-config","--ignore-rules","--ephemeral","--skip-git-repo-check",
                  "--sandbox","read-only","-c",'approval_policy="never"',"-c",'web_search="disabled"',
                  "-c","mcp_servers={}","--output-schema",str(schema),"--json","-C",tmp]
            for feature in ("shell_tool","unified_exec","apps","plugins","hooks","browser_use",
                            "computer_use","in_app_browser","image_generation","multi_agent",
                            "multi_agent_v2","memories","skill_search","code_mode","code_mode_host"):
                args += ["--disable",feature]
            args += ["-"]
        else:
            args=["-p","--tools","","--strict-mcp-config","--mcp-config",'{"mcpServers":{}}',
                  "--disable-slash-commands","--no-session-persistence","--setting-sources","",
                  "--output-format","json","--json-schema",json.dumps(SCHEMA)]
        code,out,err=run_bounded(argv+args,stdin=prompt,cwd=tmp,timeout=90,pulse=pulse)
        if code:
            # Provider diagnostics may contain account data. Persist only classified errors.
            text=(out+err).lower()
            if any(x in text for x in ("not logged in","unauthorized","authentication","401")):
                raise CapabilityUnavailable("Provider authentication expired; sign in again")
            raise AdapterError("Provider failed (exit %d); no action executed" % code)
        if name=="codex":
            answers=[]
            for line in out.splitlines():
                try: event=json.loads(line)
                except ValueError: continue
                item=event.get("item",{})
                if event.get("type")=="item.completed" and item.get("type")=="agent_message":
                    answers.append(item.get("text",""))
                if event.get("type")=="turn.failed":
                    raise AdapterError("Provider turn failed")
            if not answers:
                raise AdapterError("Provider returned no final answer")
            raw=answers[-1]
        else:
            response=json.loads(out)
            if response.get("is_error"):
                raise AdapterError("Provider returned an error")
            raw=response.get("structured_output") or response.get("result","")
        value=json.loads(raw) if isinstance(raw,str) else raw
        return value
