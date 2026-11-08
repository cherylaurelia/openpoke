# OpenPoke 🌴

OpenPoke is a simplified, open-source take on [Interaction Company’s](https://interaction.co/about) [Poke](https://poke.com/) assistant—built to show how a multi-agent orchestration stack can feel genuinely useful. It keeps the handful of things Poke is great at (email triage, reminders, and persistent agents) while staying easy to spin up locally.

- Multi-agent FastAPI backend that mirrors Poke's interaction/execution split, powered by [OpenRouter](https://openrouter.ai/).
- Gmail tooling via [Composio](https://composio.dev/) for drafting/replying/forwarding without leaving chat.
- Trigger scheduler and background watchers for reminders and "important email" alerts.
- Next.js web UI that proxies everything through the shared `.env`, so plugging in API keys is the only setup.

## What this fork adds

The full reasoning, evidence, and tradeoffs are in **[WRITEUP.md](WRITEUP.md)**. In short:

1. **Agents don't overload the main assistant.** Poke sees the 20 most recently used agents plus a search tool instead of every agent ever made, finds agents by name regardless of case or punctuation, can't silently create duplicates, and asks the user instead of guessing when two agents fit equally.
2. **An Activity panel** (the button next to Settings) shows what needs you, what's coming up, and what was actually done, built from records the code writes as each step happens rather than from the AI's own summary.
3. **"Waiting on": Poke chases replies for you.** When you email someone, Poke waits for their reply; if none comes in 2 days, it drafts one nudge and asks you before sending. Listed in Activity, with Stop.
4. **Emails wait for approval, enforced in code.** Sending, replying, and forwarding create a request you approve in chat ("yes, send it") or in the panel, which shows everyone it goes to, cc and bcc included. Each request sends at most once. Emails only to yourself skip approval, so scheduled summaries still work.

Also fixed along the way: the default model (`anthropic/claude-sonnet-4`) was retired on OpenRouter, so every call failed; reminders could fire twice after a restart; and newer models looped on the `wait` tool until the step limit. Set `OPENPOKE_MODEL` in `.env` to use a different model.

## Requirements
- Python 3.10+
- Node.js 18+
- npm 9+

## Quickstart
1. **Clone and enter the repo.**
   ```bash
   git clone https://github.com/cherylaurelia/openpoke
   cd openpoke
   ```
2. **Create a shared env file.** Copy the template and open it in your editor:
   ```bash
   cp .env.example .env
   ```
3. **Get your API keys and add them to `.env`:**
   
   **OpenRouter (Required)**
   - Create an account at [openrouter.ai](https://openrouter.ai/)
   - Generate an API key
   - Replace `your_openrouter_api_key_here` with your actual key in `.env`
   
   **Composio (Required for Gmail)**
   - Sign in at [composio.dev](https://composio.dev/)
   - Create an API key
   - Set up Gmail integration and get your auth config ID
   - Replace `your_composio_api_key_here` and `your_gmail_auth_config_id_here` in `.env`
4. **(Required) Create and activate a Python 3.10+ virtualenv:**
   ```bash
   # Ensure you're using Python 3.10+
   python3.10 -m venv .venv
   source .venv/bin/activate
   
   # Verify Python version (should show 3.10+)
   python --version
   ```
   On Windows (PowerShell):
   ```powershell
   # Use Python 3.10+ (adjust path as needed)
   python3.10 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   
   # Verify Python version
   python --version
   ```

5. **Install backend dependencies:**
   ```bash
   pip install -r server/requirements.txt
   ```
6. **Install frontend dependencies:**
   ```bash
   npm install --prefix web
   ```
7. **Start the FastAPI server:**
   ```bash
   python -m server.server --reload
   ```
8. **Start the Next.js app (new terminal):**
   ```bash
   npm run dev --prefix web
   ```
9. **Connect Gmail for email workflows.** With both services running, open [http://localhost:3000](http://localhost:3000), head to *Settings → Gmail*, and complete the Composio OAuth flow. This step is required for email drafting, replies, and the important-email monitor.

The web app proxies API calls to the Python server using the values in `.env`, so keeping both processes running is required for end-to-end flows.

## Tests

```bash
pip install pytest
python -m pytest tests -q
```

The tests run without API keys or Gmail: model calls and email sending are replaced with fakes.

The live routing evals, which call the real model and compare against the original code, are in [`evals/`](evals/README.md).

## Project Layout
- `server/` – FastAPI application and agents
- `web/` – Next.js app
- `server/data/` – runtime data (ignored by git)

## License
MIT — see [LICENSE](LICENSE).
