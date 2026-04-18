# Crew Orchestrator

> v0.0.1 — pre-release

An AI crew that plans, writes code, writes tests, and opens a draft PR — driven by your project's own conventions.

Give it a task description. It figures out the type (feature / bug / hotfix / refactor / test / docs), runs the right agent sequence, writes the files, and opens a GitHub PR against your repo.

---

## How it works

A sequential [CrewAI](https://github.com/joaomdmoura/crewAI) pipeline:

```
git preflight → planner → architect → coder → tester → reviewer → standards check → git summary
```

- Each agent runs on **Groq** (LLaMA 3.3 70B — fast, free tier available)
- Agents have **no tools** — all output is plain text / fenced code blocks
- The runner extracts code blocks, writes files, stages them, and opens a **draft PR** via `gh`
- The crew is fully aware of your project's `.cursor/` folder — skills, rules, commands, and agent definitions are automatically loaded and injected into the right agents at runtime

---

## .cursor awareness

If your project uses [Cursor](https://cursor.sh) with a `.cursor/` folder, the orchestrator inherits everything from it automatically — no extra config needed.

| `.cursor/` folder | What happens |
|---|---|
| `rules/` | Injected per-agent based on role (e.g. `clean-code` → coder, `e2e-standards` → tester) |
| `skills/` | Index injected into planner so it knows what skills exist; full content injected into relevant tasks by keyword matching |
| `commands/` | Listed as available commands for agents that execute CLI tools |
| `agents/` | Specialist context files (e.g. `mongodb-specialist.md`) injected when description keywords match |

Projects without a `.cursor/` folder work fine — agents just run without project-specific conventions.

---

## Preconditions

Before setup, make sure you have:

| Requirement | Check / Install |
|---|---|
| Python 3.10+ | `python3 --version` |
| Git configured | `git config --global user.name` |
| `gh` CLI installed + authenticated | `gh auth status` — [install](https://cli.github.com) |
| Groq API key (free) | [console.groq.com](https://console.groq.com) → API Keys |
| A local project repo | Any git repo you want the crew to work on |

---

## Quick setup (recommended)

```bash
git clone git@github.com:vfussa/orchestrator.git ~/crew-orchestrator
cd ~/crew-orchestrator
./setup.sh
```

`setup.sh` will:
- Create a Python virtualenv and install all dependencies
- Ask for your **Groq API key** (one input)
- Ask for the **path to your project** (one input)
- Write `.env` and `projects.yaml`
- Start the dashboard at [http://localhost:8765](http://localhost:8765)

---

## Manual setup

```bash
# 1. Clone
git clone git@github.com:vfussa/orchestrator.git ~/crew-orchestrator
cd ~/crew-orchestrator

# 2. Python env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. API key
cp .env.example .env
# Edit .env — paste your GROQ_API_KEY

# 4. Project config
cp projects.yaml.template projects.yaml
# Edit projects.yaml — set the path to your project

# 5. Start
.venv/bin/python3 dashboard/server.py
# Open http://localhost:8765
```

---

## Cursor MCP integration

To dispatch tasks directly from Cursor without leaving the editor, add this to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "crew-orchestrator": {
      "command": "/path/to/crew-orchestrator/.venv/bin/python3",
      "args": ["/path/to/crew-orchestrator/mcp_server.py"]
    }
  }
}
```

Replace `/path/to/crew-orchestrator` with your actual install path (default: `~/crew-orchestrator`).

After restarting Cursor, the agent chat has access to:

- `start_task` — dispatch a task to the crew
- `task_status` — check status of a running task
- `where_are_changes` — find which branch contains changes for a task
- `improve_orchestrator` — trigger a retrospective run

---

## projects.yaml

The minimal config — just a name and a path:

```yaml
projects:
  my-app:
    path: /Users/you/dev/my-app
```

Optional: add a `structure_hint` to help agents understand your codebase layout:

```yaml
projects:
  my-app:
    path: /Users/you/dev/my-app
    structure_hint: >
      TypeScript/React app. Source files in src/. Components in src/components/.
      Tests co-located with source files as *.test.ts.
```

---

## Restarting the server

```bash
cd ~/crew-orchestrator
.venv/bin/python3 dashboard/server.py
```

Or double-click `start.sh` (macOS).

---

## Adding more projects

Add entries to `projects.yaml`:

```yaml
projects:
  my-app:
    path: /Users/you/dev/my-app
  another-project:
    path: /Users/you/dev/another-project
```

Restart the server — the dashboard will show a project selector.

---

## Directory structure

```
crew-orchestrator/
├── agents.yaml           # agent roles, goals, backstories
├── tasks.yaml            # task pipeline and skill injection config
├── runner.py             # crew execution, file extraction, git
├── router.py             # task type inference from description
├── context_loader.py     # .cursor folder scanning and context injection
├── mcp_server.py         # Cursor MCP integration
├── logger.py             # run logging
├── registry.py           # branch and task state
├── dashboard/
│   ├── server.py         # FastAPI + WebSocket backend
│   └── index.html        # dashboard UI
├── projects.yaml         # (gitignored) your machine-specific project paths
├── .env                  # (gitignored) API keys
├── registry/             # (gitignored) runtime state
└── runs/                 # (gitignored) run logs
```

---

## Troubleshooting

**Server won't start** — check Python version (`python3 --version` must be 3.10+) and that deps are installed (`.venv/bin/pip install -r requirements.txt`).

**LLM errors** — run the built-in test: open [http://localhost:8765/api/llm-test](http://localhost:8765/api/llm-test). It will tell you if the Groq key is missing or the API is unreachable.

**PR not created** — make sure `gh` is authenticated (`gh auth status`) and has repo access.

**Skills not loading** — the crew reads skills from your project's `.cursor/skills/` directory. If that folder doesn't exist, agents run without project-specific conventions (still works).

**MCP not showing in Cursor** — restart Cursor after editing `mcp.json`, and verify the Python path points to the venv (`~/crew-orchestrator/.venv/bin/python3`).
