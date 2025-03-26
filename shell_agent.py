import os
import socket
import getpass
import sys
import pty
import asyncio
import uuid
import json
import argparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from pydantic import BaseModel
import uvicorn

# prompt_toolkit imports for ANSI support and interactive shell
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.application.current import get_app

app = FastAPI(root_path="/gpt-shell")

# Global flags and variables
quiet_mode = True        # Default True: suppress uvicorn web messages
require_confirmation = True
global_shell_session = None  # For the local interactive shell

# ANSI color definitions
COLOR_WHITE = "\033[97m"    # bright white for (sgpt)
COLOR_RESET = "\033[0m"
REMOTE_COLOR = "\033[38;2;223;155;255m"  # approx. #df9bff

def get_prompt_text():
    """Generate the prompt text with ANSI escape codes."""
    user = getpass.getuser()
    hostname = socket.gethostname()
    cwd = os.getcwd()
    return f"{COLOR_WHITE}(sgpt){COLOR_RESET} {user}@{hostname}:{cwd}$ "

def get_prompt():
    """Return an ANSI formatted prompt for prompt_toolkit."""
    return ANSI(get_prompt_text())

def force_ls_color(cmd: str) -> str:
    """
    If the command starts with 'ls' and does not already include a --color flag,
    insert '--color=always' so that ls produces colored output.
    """
    parts = cmd.split()
    if parts and parts[0] == "ls" and "--color" not in cmd:
        parts.insert(1, "--color=always")
        return " ".join(parts)
    return cmd

def is_interactive(cmd: str) -> bool:
    """
    A simple check to see if the command appears interactive.
    For example, commands with "-it" are considered interactive.
    """
    return "-it" in cmd or cmd.strip() in {"bash", "sh"}

# ------------------------------
# Local Interactive Shell
# ------------------------------
async def interactive_shell():
    global global_shell_session
    session = PromptSession()
    global_shell_session = session
    while True:
        try:
            user_input = await session.prompt_async(message=get_prompt)
        except (EOFError, KeyboardInterrupt):
            print("Exiting SGPT shell.")
            os._exit(0)
        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() == "exit":
            print("Exiting SGPT shell.")
            os._exit(0)
        # Disallow interactive commands here:
        if is_interactive(user_input):
            print("Interactive commands are not supported via this local shell. Use the interactive session endpoints.")
            continue
        user_input = force_ls_color(user_input)
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

# ------------------------------
# HTTP Endpoints for One-Shot Commands
# ------------------------------
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
            "stderr": "Interactive commands require an interactive session. Use the /interactive endpoints.",
            "exit_code": -1
        }
    cmd = force_ls_color(cmd)
    print_formatted_text(ANSI(f"\n{REMOTE_COLOR}{cmd}{COLOR_RESET}"))
    if require_confirmation:
        print("Confirm execution? [Y/n] ")
        answer = sys.stdin.readline().strip().lower()
        if answer and answer != "y":
            print("Command declined by user.")
            return {"stdout": "", "stderr": "Command execution declined by user.", "exit_code": -1}
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
        return {"stdout": "".join(stdout_data), "stderr": "".join(stderr_data), "exit_code": exit_code}
    except Exception as e:
        print(f"[sgpt] Exception during /run: {e}")
        print("\n" + get_prompt_text(), end='', flush=True)
        return {"stdout": "", "stderr": str(e), "exit_code": -1}

@app.post("/start")
async def start_command(payload: ShellCommand):
    cmd = payload.command.strip()
    if is_interactive(cmd):
        return {"stdout": "", "stderr": "Interactive commands require an interactive session. Use the /interactive endpoints.", "exit_code": -1}
    cmd = force_ls_color(cmd)
    stdin_input = payload.stdin
    proc_id = str(uuid.uuid4())
    stdout_buffer = []
    stderr_buffer = []
    print(f"\n[sgpt] [START] Launching background process:\n{cmd}\n🆔 ID: {proc_id}")
    if require_confirmation:
        print("Confirm background execution? [Y/n] ")
        answer = sys.stdin.readline().strip().lower()
        if answer and answer != "y":
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
    processes[proc_id] = {"process": process, "stdout": stdout_buffer, "stderr": stderr_buffer}
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
    return {"message": f"Process {id} terminated.", "exit_code": proc["process"].returncode}

@app.get("/openapi.json")
async def get_openapi():
    with open("openapi.json", "r") as f:
        return json.load(f)

# ------------------------------
# Interactive Session Management (HTTP Polling)
# ------------------------------

# In-memory store for interactive sessions.
interactive_sessions = {}

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
        os.close(self.master_fd)
        self._read_task.cancel()

@app.post("/interactive/start")
async def interactive_start(request: Request):
    """
    Start an interactive session. JSON body can include:
    { "cmd": "bash" }
    If not provided, defaults to bash.
    Returns a session_id.
    """
    data = await request.json()
    cmd = data.get("cmd", "bash")
    session_id = str(uuid.uuid4())
    # Create a PTY and fork a process.
    pid, master_fd = pty.fork()
    if pid == 0:
        # Child process: execute the command.
        os.execvp(cmd, [cmd])
    else:
        # Parent process: store the session.
        session = InteractiveSession(session_id, master_fd, pid)
        interactive_sessions[session_id] = session
        return {"session_id": session_id}

@app.get("/interactive/output/{session_id}")
async def interactive_output(session_id: str):
    session = interactive_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    out = await session.get_output()
    return {"output": out}

class InputPayload(BaseModel):
    input: str

@app.post("/interactive/input/{session_id}")
async def interactive_input(session_id: str, payload: InputPayload):
    session = interactive_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.write_input(payload.input)
    return {"status": "input sent"}

@app.post("/interactive/kill/{session_id}")
async def interactive_kill(session_id: str):
    session = interactive_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.kill()
    del interactive_sessions[session_id]
    return {"status": "session terminated"}

# ------------------------------
# Uvicorn Server and Main
# ------------------------------
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
        print(f"[sgpt] Uvicorn server failed to start (possibly due to port already in use): {e}")

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
