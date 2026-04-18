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
- Skills, rules, and commands are loaded from your project's `.cursor/` folder — the crew inherits your project's own conventions automatically

---

## Preconditions

Before setup, make sure you have:

| Requirement | Check / Install |
|---|---|
| Python 3.11+ | `python3 --version` |
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

## projects.yaml

The minimal config — just a name and a path:

```yaml
projects:
  my-app:
    path: /Users/you/dev/my-app
```

If your project has a `.cursor/` folder with skills, rules, and commands, the crew will pick them up automatically. Nothing extra to configure.

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
├── context_loader.py     # project path resolution
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

**Server won't start** — check Python version (`python3 --version` must be 3.11+) and that deps are installed (`.venv/bin/pip install -r requirements.txt`).

**LLM errors** — run the built-in test: open [http://localhost:8765/api/llm-test](http://localhost:8765/api/llm-test). It will tell you if the Groq key is missing or the API is unreachable.

**PR not created** — make sure `gh` is authenticated (`gh auth status`) and has repo access.

**Skills not loading** — the crew reads skills from your project's `.cursor/skills/` directory. If that folder doesn't exist, agents run without skill context (still works, just less project-aware).
