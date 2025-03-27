import os
import socket
import getpass
import sys
import pty
import asyncio
import uuid
import json
import argparse
import shlex

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import ANSI

app = FastAPI(root_path="/gpt-shell")

quiet_mode = True
require_confirmation = True
global_shell_session = None

COLOR_WHITE = "\033[97m"
COLOR_RESET = "\033[0m"
REMOTE_COLOR = "\033[38;2;223;155;255m"

def get_prompt_text():
    user = getpass.getuser()
    hostname = socket.gethostname()
    cwd = os.getcwd()
    return f"{COLOR_WHITE}(sgpt){COLOR_RESET} {user}@{hostname}:{cwd}$ "

def get_prompt():
    return ANSI(get_prompt_text())

def force_ls_color(cmd: str) -> str:
    parts = cmd.split()
    if parts and parts[0] == "ls" and "--color" not in cmd:
        parts.insert(1, "--color=always")
        return " ".join(parts)
    return cmd

def is_interactive(cmd: str) -> bool:
    tokens = shlex.split(cmd)
    if not tokens:
        return False
    if tokens[0] in {"bash", "sh"}:
        return True
    if any(flag in tokens for flag in ["-it", "-i", "-t"]):
        return True
    return False

# ------------------------------------------------------------------------------
# CONFIRMATION QUEUE + ITEM
# ------------------------------------------------------------------------------
pending_confirmations = asyncio.Queue()

class ConfirmationItem:
    def __init__(self, cmd: str, payload: dict, origin: str):
        self.cmd = cmd
        self.payload = payload
        self.origin = origin
        loop = asyncio.get_event_loop()
        self.future = loop.create_future()
        self.id = str(uuid.uuid4())

# ------------------------------------------------------------------------------
# INTERACTIVE SESSION
# ------------------------------------------------------------------------------
class InteractiveSession:
    def __init__(self, session_id: str, master_fd: int, pid: int):
        self.session_id = session_id
        self.master_fd = master_fd
        self.pid = pid
        self.output_buffer = ""
        self._lock = asyncio.Lock()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        loop = asyncio.get_event_loop()
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, self.master_fd, 1024)
                if not data:
                    break
                async with self._lock:
                    self.output_buffer += data.decode(errors="ignore")
            except Exception:
                break

    async def get_output(self) -> str:
        async with self._lock:
            out = self.output_buffer
            self.output_buffer = ""
            return out

    def write_input(self, input_str: str):
        os.write(self.master_fd, input_str.encode())

    def kill(self):
        try:
            os.kill(self.pid, 9)
        except Exception:
            pass
        try:
            os.close(self.master_fd)
        except Exception:
            pass
        self._read_task.cancel()

    async def attach(self):
        self._read_task.cancel()
        return self.master_fd

interactive_sessions = {}

# ------------------------------------------------------------------------------
# LOCAL SHELL
# ------------------------------------------------------------------------------
async def run_in_pty(cmd: str):
    cmd_parts = shlex.split(cmd)
    if not cmd_parts:
        return
    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(cmd_parts[0], cmd_parts)
        except Exception as e:
            print(f"Error exec'ing {cmd_parts}: {e}")
            os._exit(1)
    else:
        loop = asyncio.get_event_loop()
        session = global_shell_session
        stop_event = asyncio.Event()

        async def read_pty():
            while not stop_event.is_set():
                try:
                    data = await loop.run_in_executor(None, os.read, master_fd, 1024)
                    if not data:
                        break
                    sys.stdout.write(data.decode(errors="ignore"))
                    sys.stdout.flush()
                except OSError:
                    break
            stop_event.set()

        async def write_pty():
            while not stop_event.is_set():
                try:
                    user_input = await session.prompt_async("")
                except (EOFError, KeyboardInterrupt):
                    stop_event.set()
                    return
                if not user_input.endswith("\n"):
                    user_input += "\n"
                os.write(master_fd, user_input.encode())

        tasks = [
            asyncio.create_task(read_pty()),
            asyncio.create_task(write_pty()),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass

async def attach_local(session_id: str):
    if session_id not in interactive_sessions:
        print(f"No such session: {session_id}")
        return
    session = interactive_sessions[session_id]
    master_fd = await session.attach()
    loop = asyncio.get_event_loop()
    local_session = global_shell_session
    stop_event = asyncio.Event()

    print(f"[sgpt] Attaching local shell to session {session_id}...\n")

    async def read_pty():
        while not stop_event.is_set():
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, 1024)
                if not data:
                    break
                sys.stdout.write(data.decode(errors="ignore"))
                sys.stdout.flush()
            except OSError:
                break
        stop_event.set()

    async def write_pty():
        while not stop_event.is_set():
            try:
                user_input = await local_session.prompt_async("")
            except (EOFError, KeyboardInterrupt):
                stop_event.set()
                return
            if not user_input.endswith("\n"):
                user_input += "\n"
            os.write(master_fd, user_input.encode())

    tasks = [
        asyncio.create_task(read_pty()),
        asyncio.create_task(write_pty()),
    ]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()

    print(f"\n[sgpt] Detaching from session {session_id}.\n")

# ------------------------------------------------------------------------------
# 1) We run the main shell prompt in a loop
# 2) We run a background task that waits for new confirmations
#    and forcibly breaks the prompt with session.app.exit()
# ------------------------------------------------------------------------------

async def handle_pending_confirmations():
    """
    Runs in background. If a new item arrives, forcibly exit the current prompt
    so that the main shell loop can handle the new item immediately.
    """
    while True:
        item = await pending_confirmations.get()
        # We have a new item: forcibly exit the prompt so main loop can handle it
        if global_shell_session and global_shell_session.app:
            # Force the current prompt to end (which returns control to main loop)
            global_shell_session.app.exit()
        # Now store the item somewhere main loop can pick it up
        # Easiest: put it in a global list for main loop to drain
        new_items.append(item)


new_items = []  # A simple global list that the main loop checks

async def interactive_shell():
    global global_shell_session
    session = PromptSession()
    global_shell_session = session

    # Start the background task
    asyncio.create_task(handle_pending_confirmations())

    while True:
        # 1) Drain newly arrived items first (they appear in new_items after the background task moves them)
        while new_items:
            item = new_items.pop(0)
            print(f"\n[sgpt] GPT wants to run:\n    {item.cmd}\n")
            # Prompt user
            try:
                answer = await session.prompt_async("Confirm execution? [Y/n] ")
            except (EOFError, KeyboardInterrupt):
                item.future.set_result(False)
                continue
            if answer.strip().lower() in ("y", ""):
                print("[sgpt] Command confirmed.\n")
                item.future.set_result(True)
            else:
                print("[sgpt] Command declined.\n")
                item.future.set_result(False)

        # 2) Then do normal user prompt
        try:
            user_input = await session.prompt_async(message=get_prompt())
        except (EOFError, KeyboardInterrupt):
            print("Exiting SGPT shell.")
            os._exit(0)

        if user_input is None:
            continue

        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() == "exit":
            print("Exiting SGPT shell.")
            os._exit(0)

        # cd builtin
        if user_input.startswith("cd"):
            try:
                parts = shlex.split(user_input)
                if len(parts) == 1:
                    os.chdir(os.path.expanduser("~"))
                else:
                    path = os.path.expanduser(parts[1])
                    os.chdir(path)
            except Exception as e:
                print(f"cd: {e}")
            continue

        # attach
        if user_input.startswith("attach "):
            parts = user_input.split()
            if len(parts) == 2:
                sid = parts[1]
                await attach_local(sid)
            else:
                print("Usage: attach <session_id>")
            continue

        user_input = force_ls_color(user_input)
        if is_interactive(user_input):
            await run_in_pty(user_input)
            continue

        # Non-interactive
        try:
            process = await asyncio.create_subprocess_shell(
                user_input,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async def print_stream(stream):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    sys.stdout.write(line.decode())
                    sys.stdout.flush()
            await asyncio.gather(
                print_stream(process.stdout),
                print_stream(process.stderr)
            )
            await process.wait()
        except Exception as e:
            print(f"[sgpt] Error executing command: {e}")

# ------------------------------------------------------------------------------
# HTTP ENDPOINTS
# ------------------------------------------------------------------------------
class ShellCommand(BaseModel):
    command: str
    stdin: str = ""

processes = {}

@app.post("/run")
async def run_command(payload: ShellCommand):
    cmd = payload.command.strip()
    if is_interactive(cmd):
        return {
            "stdout": "",
            "stderr": "Interactive commands require an interactive session. Use /interactive/start, then attach locally if desired.",
            "exit_code": -1
        }
    cmd = force_ls_color(cmd)
    # Enqueue a confirmation if needed
    if require_confirmation:
        item = ConfirmationItem(cmd, payload.dict(), "run")
        await pending_confirmations.put(item)
        decision = await item.future
        if not decision:
            return {
                "stdout": "",
                "stderr": "Command execution declined by user.",
                "exit_code": -1
            }

    print_formatted_text(ANSI(f"\n{REMOTE_COLOR}{cmd}{COLOR_RESET}"))
    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE if payload.stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if payload.stdin:
            process.stdin.write(payload.stdin.encode())
            await process.stdin.drain()
            process.stdin.close()

        stdout_data = []
        stderr_data = []

        async def read_stream(stream, collector):
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode()
                print(decoded.rstrip())
                collector.append(decoded)

        await asyncio.gather(
            read_stream(process.stdout, stdout_data),
            read_stream(process.stderr, stderr_data)
        )
        exit_code = await process.wait()

        print("\n" + get_prompt_text(), end='', flush=True)
        return {
            "stdout": "".join(stdout_data),
            "stderr": "".join(stderr_data),
            "exit_code": exit_code
        }
    except Exception as e:
        print(f"[sgpt] Exception during /run: {e}")
        print("\n" + get_prompt_text(), end='', flush=True)
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


@app.post("/start")
async def start_command(payload: ShellCommand):
    cmd = payload.command.strip()
    if is_interactive(cmd):
        return {
            "stdout": "",
            "stderr": "Interactive commands require an interactive session. Use /interactive/start.",
            "exit_code": -1
        }
    cmd = force_ls_color(cmd)
    stdin_input = payload.stdin
    proc_id = str(uuid.uuid4())

    print(f"\n[sgpt] [START] Launching background process:\n{cmd}\n🆔 ID: {proc_id}")

    if require_confirmation:
        item = ConfirmationItem(cmd, payload.dict(), "start")
        await pending_confirmations.put(item)
        decision = await item.future
        if not decision:
            print("Background process declined by user.")
            return {"error": "Execution declined"}

    process = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE if stdin_input else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if stdin_input:
        process.stdin.write(stdin_input.encode())
        await process.stdin.drain()
        process.stdin.close()

    stdout_buffer = []
    stderr_buffer = []

    async def stream_output(stream, buffer, label):
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode()
            print(f"{label} [{proc_id}]: {decoded.rstrip()}")
            buffer.append(decoded)

    asyncio.create_task(stream_output(process.stdout, stdout_buffer, "STDOUT"))
    asyncio.create_task(stream_output(process.stderr, stderr_buffer, "STDERR"))

    processes[proc_id] = {
        "process": process,
        "stdout": stdout_buffer,
        "stderr": stderr_buffer
    }
    return {"id": proc_id}


@app.get("/output/{id}")
async def get_output(id: str):
    proc = processes.get(id)
    if not proc:
        raise HTTPException(status_code=404, detail="Process not found")
    return {
        "stdout": "".join(proc["stdout"]),
        "stderr": "".join(proc["stderr"]),
        "running": proc["process"].returncode is None,
        "exit_code": proc["process"].returncode
    }

@app.post("/kill/{id}")
async def kill_process(id: str):
    proc = processes.get(id)
    if not proc:
        raise HTTPException(status_code=404, detail="Process not found")
    proc["process"].terminate()
    await proc["process"].wait()
    return {
        "message": f"Process {id} terminated.",
        "exit_code": proc["process"].returncode
    }

@app.get("/openapi.json")
async def get_openapi():
    with open("openapi.json", "r") as f:
        return json.load(f)

def parse_args():
    parser = argparse.ArgumentParser(description="Shell Automation Agent")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Disable confirmation prompts before command execution")
    parser.add_argument("--no-quiet", action="store_true",
                        help="Enable uvicorn logging output (by default uvicorn logs are suppressed)")
    return parser.parse_args()

async def serve_uvicorn(uvicorn_log_level):
    config = uvicorn.Config(app, host="0.0.0.0", port=11000,
                            log_level=uvicorn_log_level, reload=False)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    except SystemExit as e:
        print(f"[sgpt] Uvicorn server failed to start: {e}")

async def main():
    args = parse_args()
    global require_confirmation, quiet_mode
    require_confirmation = not args.no_confirm
    quiet_mode = False if args.no_quiet else True
    uvicorn_log_level = "info" if not quiet_mode else "critical"

    await asyncio.gather(
        serve_uvicorn(uvicorn_log_level),
        interactive_shell()
    )

if __name__ == "__main__":
    asyncio.run(main())
