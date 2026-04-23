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


---

## Adding a New Repository

Follow these steps every time you onboard a new repo into the orchestrator.

### 1. Register the project in `projects.yaml`

```yaml
projects:
  my-repo:
    path: ~/path/to/my-repo          # local checkout path
    linear_project: ABC              # Linear project key (optional)
    slack_channel: "#eng"            # Slack channel for notifications (optional)
    default_base_branch: main

    # Always-loaded context files (relative to repo root)
    context_files:
      - CLAUDE.md                    # or README.md, AGENTS.md, etc.

    # Skill files loaded per task_type (relative to repo root)
    skill_map:
      test:
        - .cursor/skills/e2e-tests/SKILL.md
        - .cursor/skills/react-component-tests/SKILL.md
        - .cursor/skills/unit-tests/SKILL.md
      feature:
        - .cursor/skills/feature-development/SKILL.md
        - .cursor/skills/ui-implementation/SKILL.md
      bug:
        - .cursor/skills/browser-testing-loop/SKILL.md
      refactor:
        - .cursor/skills/feature-development/SKILL.md

    # Brief structure hint injected into every agent prompt
    structure_hint: >
      TypeScript/React repo. Source lives under src/.
      Tests: *.test.ts co-located with source files.
      Never place files at repo root.
```

### 2. Create context files in the repo

The orchestrator loads files listed in `context_files` and `skill_map` at task start.
Every repo should have **at least one** of the following:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Project rules, conventions, stack overview |
| `.cursor/skills/e2e-tests/SKILL.md` | Playwright test patterns and examples |
| `.cursor/skills/unit-tests/SKILL.md` | Unit test patterns (Vitest/Jest/etc.) |
| `.cursor/skills/react-component-tests/SKILL.md` | RTL component test patterns |
| `.cursor/skills/feature-development/SKILL.md` | Feature implementation guidelines |

**Minimum viable `CLAUDE.md`** (put this in the repo root):

```markdown
## Stack
- Language: TypeScript
- Framework: React + Vite
- Tests: Vitest + Testing Library
- E2E: Playwright

## Project Structure
src/
  features/        # Feature modules
  components/      # Shared components
  utils/           # Helpers
  *.test.ts        # Unit tests co-located with source

## Test Conventions
- Test files: `*.test.ts` (same directory as source)
- E2E files: `tests/e2e/*.spec.ts`
- Never use `any` in tests
- Mock external calls with vi.mock()

## Naming
- Components: PascalCase
- Hooks: useXxx
- Utils: camelCase
```

### 3. Verify the context is loading

After registering the project, run a test dispatch and check the planner output:

```bash
curl -s -X POST http://localhost:8765/dispatch \
  -H "Content-Type: application/json" \
  -d '{"project":"my-repo","description":"Write unit test for Foo component","task_type":"test"}'
```

Watch the dashboard — the planner agent should reference the correct file paths
and test patterns from your repo. If it still writes wrong paths, check:
- `context_files` paths exist in the repo
- `skill_map` paths exist in the repo
- `structure_hint` is accurate

### 4. Iterate on skill files

Skill files are the primary way to teach agents your conventions.
They are plain Markdown — write them as if explaining to a new developer:

- Show a **good example** of a test file
- List **what NOT to do**
- Specify **exact import paths**
- Mention **naming conventions**

The more concrete and example-driven the skill file, the better the agent output.
