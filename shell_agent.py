import os
import socket
import getpass
import sys
import asyncio
import uuid
import json
import argparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# prompt_toolkit imports for ANSI support and interactive prompt
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.application.current import get_app

app = FastAPI(root_path="/gpt-shell")

# Global flags (set from command-line arguments)
quiet_mode = True   # Default True: suppress uvicorn web messages
require_confirmation = True

# Global interactive shell session reference.
global_shell_session = None

# ANSI color definitions
COLOR_WHITE = "\033[97m"    # bright white for (sgpt)
COLOR_RESET = "\033[0m"
# ANSI true color for remote command text (approximate #df9bff)
REMOTE_COLOR = "\033[38;2;223;155;255m"

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

def my_run_in_terminal(func):
    """
    Try to run func in the terminal using get_app().run_in_terminal if available;
    otherwise, simply call func.
    """
    app_obj = get_app()
    if hasattr(app_obj, "run_in_terminal"):
        return app_obj.run_in_terminal(func)
    else:
        return func()

# ---------- Interactive Shell Loop using prompt_toolkit ----------
async def interactive_shell():
    global global_shell_session
    session = PromptSession()
    global_shell_session = session
    while True:
        try:
            # Use get_prompt() so ANSI formatting is applied.
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
        # Force ls to show colors if applicable.
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

# ---------- FastAPI Models and Endpoints ----------
class ShellCommand(BaseModel):
    command: str
    stdin: str = ""

# Track running background processes by UUID
processes = {}

@app.post("/run")
async def run_command(payload: ShellCommand):
    cmd = payload.command.strip()
    cmd = force_ls_color(cmd)
    # Print remote command on a new line in the chosen remote color.
    print_formatted_text(ANSI(f"\n{REMOTE_COLOR}{cmd}{COLOR_RESET}"))
    if require_confirmation:
        print("Confirm execution? [Y/n] ")
        answer = sys.stdin.readline().strip().lower()
        if answer and answer != "y":
            print("Command declined by user.")
            return {
                "stdout": "",
                "stderr": "Command execution declined by user.",
                "exit_code": -1
            }
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
        # After remote command execution, force a refresh of the interactive prompt.
        if global_shell_session is not None:
            my_run_in_terminal(lambda: print(get_prompt_text(), end='', flush=True))
            if hasattr(get_app(), "invalidate"):
                get_app().invalidate()
        return {
            "stdout": "".join(stdout_data),
            "stderr": "".join(stderr_data),
            "exit_code": exit_code
        }
    except Exception as e:
        print(f"[sgpt] Exception during /run: {e}")
        if global_shell_session is not None:
            my_run_in_terminal(lambda: print(get_prompt_text(), end='', flush=True))
            if hasattr(get_app(), "invalidate"):
                get_app().invalidate()
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1
        }

@app.post("/start")
async def start_command(payload: ShellCommand):
    cmd = payload.command.strip()
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
    processes[proc_id] = {
        "process": process,
        "stdout": stdout_buffer,
        "stderr": stderr_buffer,
    }
    return { "id": proc_id }

@app.get("/output/{proc_id}")
async def get_output(proc_id: str):
    proc = processes.get(proc_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Process not found")
    return {
        "stdout": "".join(proc["stdout"]),
        "stderr": "".join(proc["stderr"]),
        "running": proc["process"].returncode is None,
        "exit_code": proc["process"].returncode
    }

@app.post("/kill/{proc_id}")
async def kill_process(proc_id: str):
    proc = processes.get(proc_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Process not found")
    proc["process"].terminate()
    await proc["process"].wait()
    return {
        "message": f"Process {proc_id} terminated.",
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
