from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import asyncio
import uuid
import json

app = FastAPI(root_path="/gpt-shell")

class ShellCommand(BaseModel):
    command: str
    stdin: str = ""

# Track running background processes by UUID
processes = {}

# ---------- /run endpoint ----------
@app.post("/run")
async def run_command(payload: ShellCommand):
    cmd = payload.command.strip()
    stdin_input = payload.stdin

    print(f"\n🟡 [RUN] Received command:\n{cmd}")

    try:
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

        stdout_data = []
        stderr_data = []

        async def read_stream(stream, collector, label):
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode()
                print(f"{label}: {decoded.strip()}")
                collector.append(decoded)

        await asyncio.gather(
            read_stream(process.stdout, stdout_data, "▶️ STDOUT"),
            read_stream(process.stderr, stderr_data, "⚠️ STDERR"),
        )

        exit_code = await process.wait()

        return {
            "stdout": "".join(stdout_data),
            "stderr": "".join(stderr_data),
            "exit_code": exit_code
        }

    except Exception as e:
        print("❌ Exception during /run:", str(e))
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1
        }

# ---------- /start endpoint ----------
@app.post("/start")
async def start_command(payload: ShellCommand):
    cmd = payload.command.strip()
    stdin_input = payload.stdin
    proc_id = str(uuid.uuid4())

    stdout_buffer = []
    stderr_buffer = []

    print(f"\n🟢 [START] Launching background process:\n{cmd}\n🆔 ID: {proc_id}")

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
            print(f"{label} [{proc_id}]: {decoded.strip()}")
            buffer.append(decoded)

    asyncio.create_task(stream_output(process.stdout, stdout_buffer, "▶️ STDOUT"))
    asyncio.create_task(stream_output(process.stderr, stderr_buffer, "⚠️ STDERR"))

    processes[proc_id] = {
        "process": process,
        "stdout": stdout_buffer,
        "stderr": stderr_buffer,
    }

    return { "id": proc_id }

# ---------- /output/{id} endpoint ----------
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

# ---------- /kill/{id} endpoint ----------
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

# ---------- Optional: Expose openapi.json ----------
@app.get("/openapi.json")
async def get_openapi():
    with open("openapi.json", "r") as f:
        return json.load(f)
