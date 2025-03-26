# shellgpt


# 🧠 Shell Automation Assistant for ChatGPT

This project enables full automation between ChatGPT (via a **Custom GPT** with tool access) and your local Linux shell. It allows ChatGPT to:

- Understand your natural-language requests
- Respond with shell commands **one step at a time**
- Automatically send those commands to your local shell agent
- Wait for output, analyze it, and proceed with the next step

All commands are locally confirmed (you press `Y/n` before execution), and the full command output is returned to ChatGPT for further decision-making — effectively turning ChatGPT into a fully interactive, step-by-step terminal assistant.

### Why?

- No copy-pasting from GPT responses to terminal
- No manual transcription of CLI output
- Just seamless, safe, command-driven chat

---

## 🛠️ Installation & Setup

Installation requires two components to be configured:

1. Your own private custom GPT
2. Agent running on the linux machine to be worked on


### Custom GPT Setup

#### 1. Visit chat.openai.com → Explore GPTs → Create.

Name your GPT whatever you like. Then, paste the following system prompt:

```
You are an advanced shell automation assistant. Your responses are executed by a real shell on the user's machine, via a tool. Please strictly follow these rules:

1. Always output only one code block at a time. This code block may contain multiple commands only if they are meant to be executed together in one shell invocation.
2. Do not provide multiple alternative commands. Do not write things like: “If that doesn’t work, try this.” Instead, wait for the result of the current command before deciding what to do next.
3. Handle multi-step tasks sequentially. First send the command, wait for the result, then analyze and proceed.
4. The user does not run the commands. You are responsible for issuing them via the tool.
5. Avoid asking the user to copy-paste or manually run anything. Use only tool calls.
6. After each command execution, analyze the output before suggesting the next command.
7. If the command is short or one-shot, use the /run endpoint. If it’s a long-running one, maybe /start. If it’s an interactive or TTY-based command (like docker exec -it, qemu, or bash), then do the following sequence:
   a. POST /interactive/start to create a session.
   b. Periodically call GET /interactive/output/{session_id} for new output.
   c. POST /interactive/input/{session_id} to send keystrokes.
   d. POST /interactive/kill/{session_id} to end.
```

#### 2. Add an Action (Tool)

Congrats! You have just created a custom GPT.

Switch to the *Configure* tab.

Click Add Action and configure it as follows:

* Authentication: None
* Schema: *copy paste the openapi.json content from the repository here*

_Notes:_ 

* Make sure to replace the "url" in the JSON with YOUR_URL (explained below).
* YOUR_URL must be https, and without port (meaning, something like https://[YOUR_URL]:11000 won't be accepted by ChatGPT, only https://[YOUR_URL]).

#### 3. Configure YOUR_URL

To achieve this (YOUR_URL), you can either use ngrok (reverse tunnel to expose your port to the internet), or, if you control your domain, configure nginx or similar for proxy pass to a specific port (I used 11000 in the example below).

Example of using ngrok:

```
ngrok http 11000
```

(This will expose https as well).

Then, use the generated https://xxxxxx.ngrok.io URL as YOUR_URL.



### Agent Setup

#### 1. Clone the repository

```
git clone https://github.com/omeriko9/shellgpt.git
cd shellgpt
```

#### 2. Create a virtual environment and install dependencies

```
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

#### 3. Start the shell agent

```
python3 shell_agent.py
or
python3 shell_agent.py --no-confirm   # This won't ask you for confirmation before each command (dangerous!) 

```

✅ You're Done!

You should now be able to test the custom GPT with your shellgpt agent successfully.


🔐 Notes & Caveats
* You will see a “Confirm access to [YOUR_URL]” prompt when the GPT first talks to your agent — this is normal OpenAI sandbox behavior.
* All command executions are locally confirmed (you decide to run or decline), can be bypassed with --no-confirm cmd line option.

📦 Future Ideas
* Persistent working directory across steps
* Auto-start on system boot
* Live file upload/download integration
* Docker/Firejail sandboxing
* Multi-agent orchestration support
