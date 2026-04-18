# Multi-Agent Orchestrator Setup Plan

**Goal:** Run parallel task workflows on separate git branches, triggered from Cursor.  
Only the agents actually needed for a given task type are activated — no unnecessary spend.

**Stack:** CrewAI (orchestrator) + MCP server (Cursor integration) + git worktrees (parallel branches)

**Key principle:** The orchestrator is a standalone local tool that lives entirely outside any project repo. It can target any project — Planable today, anything else tomorrow. Nothing lands inside `planable-app/` except the CLAUDE.md compression (which is project-local by nature).

---

## Directory Structure

```
~/crew-orchestrator/            ← standalone local tool, its own git repo, zero overlap with any project
├── agents.yaml                 ← all agent definitions
├── agents.original.yaml        ← caveman:compress backup
├── tasks.yaml                  ← task pipeline definitions
├── tasks.original.yaml
├── projects.yaml               ← registered projects + per-project enforcement rules
├── mcp_server.py               ← MCP server registered globally in Cursor
├── linear_poller.py            ← Linear dispatch relay (background service)
├── dashboard/                  ← local web dashboard (localhost:8765)
├── docs/                       ← orchestrator reference docs (this plan, mockups, etc.)
│   ├── agent-orchestrator-plan.md
│   └── orchestrator-dashboard-mockup.html
├── scripts/
│   └── new-task.sh             ← worktree creation helper
├── runs/                       ← run logs, separated by project
│   └── planable-app/
│       └── 2026-04-17-P-15301.json
├── registry/                   ← git branch registry, separated by project
│   └── planable-app.json
├── retrospectives/             ← retrospective reports and proposals
│   └── planable-app/
└── .env                        ← all API keys (ANTHROPIC, GITHUB, SLACK, LINEAR, CLOUDFLARE)
```

**`projects.yaml`** registers each codebase the orchestrator targets and declares all enforcement rules the agents must follow when working on it. The orchestrator reads these files directly from the project at task start — the project itself is never modified.

```yaml
projects:
  planable-app:
    path: ~/dev/planable-app
    linear_project: PLN
    slack_channel: "#eng"           # TODO: fill in before execution
    default_base_branch: main

    # Files the orchestrator reads and injects into agent context at task start
    context_files:
      - CLAUDE.md                   # project conventions, always injected for all agents
      - package.json                # scripts reference, injected for Coder + QA

    # Skills the orchestrator maps to agents (paths relative to project root)
    skills:
      planner:
        - .cursor/skills/feature-development/SKILL.md
        - .cursor/skills/investigate-linear-triage/SKILL.md
      architect:
        - .cursor/skills/feature-development/SKILL.md
        - .cursor/skills/mongodb/SKILL.md
        - .cursor/skills/trpc/SKILL.md
      coder:
        - .cursor/skills/ui-implementation/SKILL.md
        - .cursor/skills/trpc/SKILL.md
        - .cursor/skills/analytics-events/SKILL.md
        - .cursor/skills/reflag/SKILL.md
        - .cursor/skills/migrate-antd-button/SKILL.md
      test_writer:
        - .cursor/skills/unit-tests/SKILL.md
        - .cursor/skills/react-component-tests/SKILL.md
        - .cursor/skills/generate-test-description/SKILL.md
      qa_engineer:
        - .cursor/skills/e2e-tests/SKILL.md
        - .cursor/skills/generate-test-plan/SKILL.md
        - .cursor/skills/playwright-login-navigation/SKILL.md
        - .cursor/skills/reflag/SKILL.md
        - .cursor/skills/browser-testing-loop/SKILL.md
        - .cursor/skills/playwright-trace/SKILL.md
        - .cursor/skills/debug-failed-test/SKILL.md
        - .cursor/skills/testpick/SKILL.md
      pr_reviewer:
        - .cursor/skills/github-pull-requests/SKILL.md
      git_expert:
        - .cursor/skills/worktree/SKILL.md

    # Rules injected into agents that perform code/review/standards work
    rules:
      - .cursor/rules/clean-code.md
      - .cursor/rules/git-conventions.md
      - .cursor/rules/test-naming-conventions.mdc
      - .cursor/rules/e2e-standards.mdc
      - .cursor/rules/rules-index.mdc

    # Commands agents are permitted to run (enforced — agents cannot run unlisted commands)
    allowed_commands:
      coder:
        - npm run eslint:fix
        - npm run prettier:all
        - npm run lint:folder-structure
        - npm run generact
        - npm run reflag:new-flag
        - npm run reflag:generate-types
        - npm run build:icons
      test_writer:
        - npm run test
        - npm run test:unit
        - npm run test:jsdom
      qa_engineer:
        - npm run playwright:regression-functional
        - npm run playwright:regression-visual
        - npm run playwright:smoke-test
        - npm run playwright:open
        - npm run coverage:e2e
        - npm run coverage:merge
      pr_reviewer:
        - npm run eslint:ci
        - npm run spellcheck
      standards_guardian:
        - npm run lint:folder-structure
        - npm run eslint:ci
```

Adding a new project later means a new entry in this file. The orchestrator infrastructure and agents stay completely unchanged.

---

## Why CrewAI

- Free and open source
- Built around named, role-based agents — maps directly to the full crew below
- YAML-based config, readable and easy to extend
- Has a native MCP server wrapper so Cursor can call crews as tools
- Supports multiple LLM backends, so each agent can use the cheapest model that meets its needs
- Actively maintained, large community

---

## Phase 1 — Install CrewAI + Caveman (agent handles this)

1. Verify Python 3.11+ is available on the machine
2. Create the orchestrator home directory and init a git repo:
   ```bash
   mkdir -p ~/crew-orchestrator && cd ~/crew-orchestrator && git init
   ```
3. Install CrewAI and its tools package:
   ```
   pip install crewai crewai-tools
   ```
4. Install the CrewAI MCP server package:
   ```
   pip install crewai-mcp
   ```
5. Install Caveman for Cursor (token compression in the IDE):
   ```
   npx skills add JuliusBrussee/caveman -a cursor
   ```
6. Create `~/crew-orchestrator/projects.yaml` and register `planable-app` as the first project
7. Run `caveman:compress` on orchestrator config files (agents read these every session):
   ```
   /caveman:compress ~/crew-orchestrator/agents.yaml
   /caveman:compress ~/crew-orchestrator/tasks.yaml
   ```
   Backups land in `~/crew-orchestrator/` as `agents.original.yaml` etc. — nothing touches planable-app.

   CLAUDE.md and project skill files are read directly from the project at task-start and compressed in-memory per run. No backup files are written into the project.

**Verify:** `crewai --version` returns without error, Caveman skill appears in Cursor, `~/crew-orchestrator/` directory exists

---

## Phase 2 — Define the Agents

Files live at `~/crew-orchestrator/`. Create:

### `agents.yaml`

Each agent is assigned the cheapest model capable of its reasoning demands.  
Model tier rationale is documented inline.

**All agents carry the Caveman system prompt** — inter-agent handoffs are the biggest token sink in a pipeline. No human reads Planner → Architect → Coder messages directly, so there is no reason to generate verbose prose. Caveman ultra on all internal outputs cuts ~65–87% of those tokens.

The always-on snippet injected into every agent's `backstory`:
```
Terse like caveman. Technical substance exact. Only fluff die. Drop: articles, filler, pleasantries, hedging. Fragments OK. Short synonyms. Code unchanged. Pattern: [thing] [action] [reason]. [next step]. ACTIVE EVERY RESPONSE. No revert. No filler drift.
```

Notifications to Slack/Linear and PR bodies use **lite** mode — humans read those.

```yaml
planner:
  role: Task Planner
  goal: Break down the user's task into clear, sequenced subtasks
  backstory: >
    You are a senior engineering manager. You receive a feature request
    and decompose it into a concrete implementation plan with no ambiguity.
    You identify dependencies, risks, and the correct order of execution.
  llm: claude-3-5-haiku  # structured decomposition, no deep reasoning needed — haiku is sufficient

architect:
  role: Solution Architect
  goal: Design the technical approach for each subtask
  backstory: >
    You are a staff engineer. You take a plan and decide exactly what
    files to touch, what patterns to follow, and what edge cases to handle.
    You produce a precise spec the coder can execute without guessing.
  llm: claude-3-5-sonnet  # needs real reasoning about tradeoffs and codebase patterns

coder:
  role: Senior Software Engineer
  goal: Implement the solution exactly as designed, with clean production-ready code
  backstory: >
    You are a senior software engineer with deep expertise in the codebase.
    You receive a precise spec and implement it cleanly, following all project
    conventions, keeping changes surgical, and leaving no loose ends.
  llm: claude-3-7-sonnet  # most demanding task — warrants the strongest model

test_writer:
  role: Test Writer
  goal: Write focused, targeted unit and integration tests for the specific code that was changed
  backstory: >
    You are a specialist in writing tests. You receive the diff of what was
    implemented and write precise tests for exactly those changes — not the whole
    system. You cover the happy path and the most important failure cases, nothing
    more. Fast and focused.
  llm: claude-3-5-haiku  # pattern-based test writing, haiku handles this well

qa_engineer:
  role: Senior Automation QA Engineer
  goal: Design and oversee the full test strategy, write complex e2e and integration tests
  backstory: >
    You are a senior QA engineer specialising in automation. You assess test
    coverage holistically, identify gaps the test writer missed, and write
    complex e2e and integration tests that span multiple layers. You do not
    ship untested code. You follow the project's existing test conventions.
  llm: claude-3-5-sonnet  # broader reasoning than test_writer, sonnet appropriate

pr_reviewer:
  role: Pull Request Reviewer
  goal: Catch every issue — logic, style, security, performance — before code merges
  backstory: >
    You are a meticulous and proficient code reviewer with a keen eye for
    correctness, clarity, and risk. You review diffs line by line. You flag
    logic errors, missing error handling, security gaps, performance pitfalls,
    and deviations from project conventions. You are constructive but thorough —
    nothing slips past you.
  llm: claude-3-5-sonnet  # careful reading but not creative — sonnet is right

standards_guardian:
  role: Standards & Requirements Guardian
  goal: Ensure all requirements, conventions, and acceptance criteria are met and stay met
  backstory: >
    You are the team's enforcer of quality and completeness. You hold the
    original task requirements in one hand and the final output in the other,
    and you check every item has been addressed. You verify coding conventions,
    project structure rules, test coverage requirements, and any team-specific
    standards. If anything is missing, drifted, or inconsistent, you raise it
    with a precise description and the fix needed. You are the last gate before
    a task is declared done.
  llm: claude-3-5-haiku  # checklist verification — structured, cheap, fast

git_expert:
  role: Git Flow Expert
  goal: Maintain a live, accurate map of every branch, worktree, and change — and answer
    precisely where any piece of work lives at any moment
  backstory: >
    You are an expert in Git internals and branching strategies. You track every
    worktree, branch, and commit that the crew creates. After each task you log
    what changed, on which branch, and in which worktree. You maintain a running
    branch registry so that at any moment — mid-task or after — you can answer
    exactly where specific changes live, what state a branch is in, whether it
    has been merged, and what is still in progress. When asked "where are the
    changes for X?", you consult your registry and the live git state and give
    a precise, unambiguous answer.
  llm: claude-3-5-haiku  # registry lookups and git command output — no heavy reasoning
```

---

## Phase 2b — Task-Type Agent Matrix

**Not every task needs every agent.** The orchestrator reasons about task type at kickoff and activates only the agents required. This keeps costs low and pipelines fast.

| Task Type | Planner | Architect | Coder | Test Writer | QA Engineer | PR Reviewer | Standards Guardian | Git Expert |
|-----------|:-------:|:---------:|:-----:|:-----------:|:-----------:|:-----------:|:-----------------:|:----------:|
| New feature | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Bug fix | ✅ | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Hotfix / critical patch | — | — | ✅ | ✅ | ✅ | ✅ | — | ✅ |
| Refactor | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Test-only task | — | — | — | ✅ | ✅ | ✅ | — | ✅ |
| Documentation / config | ✅ | — | ✅ | — | ✅ | — | ✅ | ✅ |
| Small chore / typo fix | — | — | ✅ | — | ✅ | — | — | ✅ |

**QA mandate per task type:**

- **Bug fix** — QA Engineer writes a regression test that reproduces the exact bug before the fix, then confirms it passes after. If this test didn't exist before, it must exist after. No exceptions.
- **Hotfix** — same regression test requirement as bug fix, compressed timeline. Test Writer handles the targeted unit test, QA Engineer validates it actually catches the original failure.
- **New feature / Refactor** — full QA coverage: happy path, edge cases, failure scenarios, e2e where applicable.
- **Documentation / config** — QA Engineer verifies config values are valid, no broken references, and that any documented behaviour actually matches the code.
- **Small chore** — QA Engineer does a quick sanity check: does the change break any existing tests? Runs the test suite on the affected area and signs off.

The orchestrator evaluates the task description at kickoff and selects the appropriate column. This decision is logged so you can audit or override it.

**Model cost summary per task type (approximate token spend):**

- New feature / Refactor: full crew, highest spend — used only when justified
- Bug fix / Hotfix: mid spend, no architect — QA still mandatory
- Chore: lean crew, QA runs a quick sanity pass only

---

## Phase 2c — Agent Skills, Commands & MCP Attribution

Each agent is equipped with only the skills, commands, and MCP access it actually needs.

---

### 🗂 Planner
**MCPs:** Linear (read issue, extract acceptance criteria), Notion (read linked specs or design docs)
**Skills:**
- `feature-development` — read Linear issue context, understand scope before decomposing
- `investigate-linear-triage` — if task is a bug, pull Sentry/log context to inform the plan

**What it does:** Reads the Linear issue and any linked Notion docs to understand the full requirement before producing the subtask list. For bugs, checks Sentry context so the plan addresses root cause, not symptoms.

---

### 🏗 Architect
**MCPs:** Linear (read issue + comments), Notion (read design specs, API contracts), GitHub (browse existing file structure and patterns)
**Skills:**
- `feature-development` — full workflow context for multi-file changes
- `mongodb` — understand collection schemas before deciding data layer approach
- `trpc` — understand TRPC conventions before designing new routes

**What it does:** Reads existing code patterns via GitHub MCP, consults MongoDB schema reference and TRPC conventions before speccing out the implementation. Produces a file-level plan the coder can follow without guessing.

---

### 💻 Coder (Senior Software Engineer)
**MCPs:** GitHub (read existing files, write changes)
**Skills:**
- `ui-implementation` — Tailwind tokens, Radix components, `tv()` variants, accessibility patterns
- `trpc` — implement queries, mutations, subscriptions with proper invalidation
- `analytics-events` — instrument Mixpanel/Segment events when a feature requires tracking
- `reflag` — create or enable Reflag feature flags when the feature is flag-gated
- `migrate-antd-button` — if touching any Ant Design button components, migrate them
**Commands:**
- `npm run eslint:fix` — auto-fix linting after implementation
- `npm run prettier:all` — format all changed files
- `npm run lint:folder-structure` — verify new files are in the correct locations
- `npm run generact` — scaffold new component boilerplate
- `npm run reflag:new-flag` — create a new feature flag if needed
- `npm run reflag:generate-types` — regenerate flag types after creating a flag
- `npm run build:icons` — rebuild icon assets if new icons were added

**Rules applied:** `clean-code.md`, `git-conventions.md`

---

### ✍️ Test Writer
**MCPs:** GitHub (read the diff of what was just implemented)
**Skills:**
- `unit-tests` — write `.tests.ts` files for Meteor methods, TRPC routes, services
- `react-component-tests` — write `.tests.tsx` files using `renderWithProviders`, RTL, Vitest
- `generate-test-description` — create `testDescription.md` for any new test folder
**Commands:**
- `npm run test` — run full unit + jsdom suite to verify tests pass
- `npm run test:unit` — run unit tests only (faster feedback loop)
- `npm run test:jsdom` — run DOM-dependent tests only

**Rules applied:** `test-naming-conventions.mdc`

---

### 🔬 QA Engineer
**MCPs:** GitHub (read implementation + test output)
**Skills:**
- `e2e-tests` — write Playwright E2E and visual tests using action sandwich + POM patterns
- `generate-test-plan` — produce exhaustive test plan before writing tests (for new features)
- `playwright-login-navigation` — handle login fixtures and deep-linking for E2E setup
- `reflag` — enable feature flags in E2E tests using `onReflag` fixture
- `browser-testing-loop` — iterative UI verification: change → reload → snapshot → verify
- `playwright-trace` — inspect Playwright trace files when a test fails unexpectedly
- `debug-failed-test` — triage test failures using trace analysis before marking as broken
- `testpick` — determine the minimal set of E2E/visual tests to run for a given PR (CI cost control)
- `qa-maintenance` — if touching test infrastructure, apply current rule standards
**Commands:**
- `npm run playwright:regression-functional` — run functional E2E suite
- `npm run playwright:regression-visual` — run visual regression suite
- `npm run playwright:smoke-test` — quick smoke pass for hotfixes
- `npm run playwright:open` — interactive Playwright UI for debugging
- `npm run coverage:e2e` — measure E2E coverage
- `npm run coverage:merge` — merge coverage with unit reports

**Rules applied:** `e2e-standards.mdc`, `test-naming-conventions.mdc`

---

### 👀 PR Reviewer
**MCPs:** GitHub (read full PR diff, post inline review comments), Linear (link review findings back to the issue)
**Skills:**
- `github-pull-requests` — PR title format, emoji conventions, structured description
- `caveman-review` — one-line inline PR comments: `L42: 🔴 bug: user null. Add guard.` No throat-clearing, no padding
**Commands:**
- `npm run eslint:ci` — CI-level lint on changed files to catch what `eslint:fix` missed
- `npm run spellcheck` — catch typos in comments, strings, and documentation

**Rules applied:** `clean-code.md`, `git-conventions.md`, `test-naming-conventions.mdc`, `e2e-standards.mdc`

**What it does:** Reviews the diff via GitHub MCP, runs linting and spellcheck, posts `caveman-review`-style one-line inline comments on the PR, and links findings to the Linear issue if acceptance criteria are not met.

---

### 🛡 Standards Guardian
**MCPs:** Linear (read original acceptance criteria), Notion (verify against any linked spec)
**Commands:**
- `npm run lint:folder-structure` — confirm new files follow project structure conventions
- `npm run eslint:ci` — final CI lint pass

**Rules applied (full set):** `clean-code.md`, `git-conventions.md`, `test-naming-conventions.mdc`, `e2e-standards.mdc`, `rules-index.mdc`

**What it does:** Pulls the original Linear acceptance criteria, compares against what was implemented and tested, runs folder structure and lint checks, and signs off only when every item is confirmed. Any gap is reported with the exact rule violated and the fix required.

---

### 🌿 Git Expert
**MCPs:** GitHub (create draft PR, read branch state, list open PRs)
**Skills:**
- `worktree` — set up isolated git worktrees with auto dependency install
- `caveman-commit` — terse commit messages, Conventional Commits, ≤50 char subject, why over what
**Commands:**
- `npm run setup-worktree -- <branch-name>` — create worktree with deps installed
- `git worktree list / add / remove` — manage worktree lifecycle
- `gh pr create --draft` — create draft PR after coder finishes
- `gh pr list / gh pr view` — query open PRs for the branch registry

**What it does:** Creates and tracks worktrees for every task, maintains the branch registry JSON file, creates draft PRs via GitHub MCP, and answers "where are the changes for X?" using live git state plus the registry.

---

### MCP Access Summary

| Agent | Linear | Notion | GitHub |
|-------|:------:|:------:|:------:|
| Planner | ✅ read issue + criteria | ✅ read specs | — |
| Architect | ✅ read issue + comments | ✅ read design docs | ✅ read existing files |
| Coder | — | — | ✅ read + write |
| Test Writer | — | — | ✅ read diff |
| QA Engineer | — | — | ✅ read implementation |
| PR Reviewer | ✅ link findings to issue | — | ✅ read diff + post comments |
| Standards Guardian | ✅ read acceptance criteria | ✅ read linked spec | — |
| Git Expert | — | — | ✅ create draft PR + read branches |

---

### Caveman Token Budget

Caveman is active at three levels depending on who consumes the output:

| Output destination | Caveman level | Why |
|-------------------|:-------------:|-----|
| Agent → Agent (internal handoffs) | **Ultra** | No human reads this; pure information transfer — maximum compression |
| Agent → PR inline comments | **`caveman-review`** | One line per finding: `L42: 🔴 bug: null ref. Add guard.` |
| Agent → commit messages | **`caveman-commit`** | ≤50 char subject, why over what, Conventional Commits format |
| Agent → Slack / Linear notifications | **Lite** | Humans read these — keep grammar, drop filler |
| Agent → dashboard display | **Lite** | Human-readable status strings |
| Orchestrator config files (`agents.yaml`, `tasks.yaml`) | **`caveman:compress`** | ~46% fewer input tokens; backups stay in `~/crew-orchestrator/` |
| Project context files (CLAUDE.md, skill files) | **compressed in-memory** at task start | Read from project, compressed ephemerally — no files written to the project |

**Expected savings per full crew run (new feature, all 8 agents):**
- Without Caveman: ~12,000–18,000 output tokens across the full pipeline
- With Caveman ultra on inter-agent messages: ~3,000–5,000 output tokens (~70–75% reduction)
- Input savings from compressed context files: ~46% on every file read at session start

---

## Phase 3 — Git Worktree Integration

Each task runs in its own branch via `git worktree`, so you can have N tasks in flight simultaneously without switching branches.

The agent will create a helper script `~/crew-orchestrator/scripts/new-task.sh`:

```bash
#!/bin/bash
# Usage: ./scripts/new-task.sh planable-app P-12345 "add user auth"
PROJECT=$1
TASK_ID=$2
DESCRIPTION=$3

# Resolve project path from projects.yaml
PROJECT_PATH=$(python3 -c "import yaml; print(yaml.safe_load(open('$HOME/crew-orchestrator/projects.yaml'))['projects']['$PROJECT']['path'])")
BRANCH="feature/${TASK_ID}-${DESCRIPTION// /-}"
WORKTREE_PATH="${PROJECT_PATH}-${TASK_ID}"   # sibling dir, outside the project

cd "$PROJECT_PATH"
git worktree add "$WORKTREE_PATH" -b "$BRANCH"
echo "Worktree ready at $WORKTREE_PATH on branch $BRANCH"
```

Each worktree is created as a **sibling directory** of the project — never inside it. Cursor can open multiple worktree windows simultaneously, one per task. The orchestrator itself is never touched.

**Verify:** Running the script creates a new directory + branch, visible in `git worktree list`

---

## Phase 4 — Automated Draft PR Creation

After the coder finishes, a draft PR is created automatically via the GitHub CLI. **PRs are always created as drafts — never as ready for review.** You promote them manually when you're satisfied.

```bash
gh pr create \
  --title "$(task_title)" \
  --body "$(crew_summary)" \
  --draft \
  --base main \
  --head "$BRANCH"
```

The Git Expert logs the PR URL into the branch registry alongside the branch entry.  
The PR body is auto-populated with the Planner's subtask list and the PR Reviewer's findings.

**Verify:** After a test crew run, a draft PR appears in GitHub with correct branch and body

---

## Phase 5 — Slack & Linear Notifications

When a crew completes (all agents done, draft PR created), a notification is sent.

**Slack:**
- Channel: `⚠️ TODO — fill in your Slack channel name (e.g. #eng-deployments)`
- Message format: `[CREW DONE] P-12345 · "Add CSV export" · Draft PR: <link> · Branch: feature/P-12345-add-csv-export`
- Sent via Slack MCP or `slack-sdk` if MCP is not connected

**Linear:**
- The task is moved to "In Review" state automatically
- A comment is added to the Linear issue with the draft PR link and a one-line crew summary
- Sent via Linear MCP or Linear API

**Setup:** Both integrations are configured via environment variables (`SLACK_BOT_TOKEN`, `LINEAR_API_KEY`) checked at install time.

**⚠️ Action needed:** Provide the Slack channel name before execution so the agent can configure it.

---

## Phase 6 — Orchestrator Dashboard

A local web dashboard at `http://localhost:8765` — the single place to see everything happening across all active tasks, branches, and agents at any moment.

**Tech stack:**
- FastAPI backend with WebSocket support (real-time push, no polling)
- React + Tailwind frontend, served as static files from `~/crew-orchestrator/dashboard/`
- Data sources: live WebSocket events from CrewAI + branch registry JSON + run logs

Launches automatically when any crew starts. Stays running as a background service.

---

### Panel 1 — Active Crews (main view)

One card per running task. Cards are live — they update in real time via WebSocket.

Each card shows:
- **Task ID + description** (e.g., `P-15301 · dark mode toggle`)
- **Project + branch** (e.g., `planable-app · feature/P-15301-dark-mode-toggle`)
- **Visual agent pipeline** — all 8 agent steps shown as nodes in a horizontal flow:

  ```
  [Planner] → [Architect] → [Coder] → [Test Writer] → [QA] → [PR Reviewer] → [Guardian] → [Git Expert]
  ```
  - ✅ Completed steps: green
  - 🔵 Active step: blue + pulsing animation
  - ⬜ Pending steps: grey
  - Skipped steps (based on task type): shown as dashed outlines

- **Current agent action** — one live line below the pipeline: `Coder: writing CsvExporter.tsx`
- **Time elapsed + estimated remaining** (based on average from run logs)
- **Token spend so far** — updates per agent completion
- **Status badge**: `Running` / `Blocked` / `Needs Review` / `Done`

---

### Panel 2 — Branch Map

Visual tree showing all active and recent branches across all projects:

```
planable-app
├── feature/P-15301-dark-mode-toggle     [🔵 Running — Coder]     worktree: ~/dev/planable-app-P-15301
├── feature/P-15200-csv-export           [✅ Done]                 Draft PR: #482
└── fix/P-15290-auth-null-crash          [✅ Done — merged]        ↳ merged to main 2h ago
```

- Clicking a branch row expands it to show the full commit list and which agent made each commit
- Branches that have been merged are shown faded with a timestamp
- Clicking the Draft PR link opens GitHub in the browser

---

### Panel 3 — Task Queue

Tasks that have been dispatched (via Linear relay or mobile) but not yet started, waiting for an available crew slot:

```
Queued (2)
  P-15310 · "add email digest settings"     [via Linear · 3m ago]
  P-15312 · "fix pagination offset bug"     [via mobile · 1m ago]
```

---

### Panel 4 — Completed Today

Compact list of all tasks finished in the current day:

```
✅ P-15295 · dark mode toggle            14:47  18m  Draft PR #480   feature  8 agents  4,200 tok
✅ P-15280 · fix null user crash          11:23  6m   Draft PR #479   bug      5 agents  1,800 tok
✅ P-15271 · update onboarding copy      09:15  4m   Draft PR #478   chore    2 agents    620 tok
```

---

### Panel 5 — Cost & Token Tracker

Running totals for the current day and the past 7 days:

- Bar chart: tokens per task type (feature / bug / hotfix / refactor / chore)
- Bar chart: tokens per agent across all runs (shows which agents are most expensive)
- Daily spend trend line (7 days)
- Today's total vs. daily average

---

### Panel 6 — Retrospective Status

```
Next auto-retrospective: in 3 runs (run 7/10)
Last retrospective:      Apr 14 · 3 proposals → Draft PR #471 (open)
Weekly scheduled:        Sunday 09:00
```

Button: **Run Retrospective Now** → triggers immediately, shows progress inline

---

### Quick Actions (top bar)

- **+ New Task** — opens a small form: description, project, task type → dispatches directly
- **Improve Orchestrator** — triggers retrospective run
- **Refresh** — force-reloads all data from disk (fallback if WebSocket drops)

---

**Verify:** Dashboard loads at `localhost:8765`, active crew card animates in real time, branch map updates when a new worktree is created

---

## Phase 7 — MCP Server (Cursor Integration)

This is what makes everything accessible from Cursor's AI panel.

1. Create `~/crew-orchestrator/mcp_server.py` — a thin MCP wrapper that exposes these tools:
   - `start_task(description, branch, task_type, project?)` — selects the right agent subset and kicks off the pipeline (defaults to `planable-app` if project is omitted)
   - `task_status(task_id)` — returns current agent, progress, and branch state
   - `where_are_changes(query)` — delegates to Git Expert, returns branch + worktree + PR link
   - `improve_orchestrator(project?)` — triggers a retrospective run immediately

2. Register the server **globally** in Cursor's MCP config (`~/.cursor/mcp.json`) — one registration, works across all projects:
   ```json
   {
     "mcpServers": {
       "crew-orchestrator": {
         "command": "python",
         "args": ["~/crew-orchestrator/mcp_server.py"]
       }
     }
   }
   ```

3. Restart Cursor — the tools appear in the AI panel automatically

**Verify:** In Cursor chat, confirm `start_task`, `task_status`, and `where_are_changes` are available as tools

---

## Phase 8 — End-to-End Workflow

Once set up, the daily flow for a **new feature** looks like this:

```
1.  Cursor: "start_task: add CSV export, branch: P-15200, type: feature"
2.  Orchestrator selects full crew (all 8 agents)
3.  Git Expert      → creates branch + worktree, logs to registry
4.  Planner         → subtask list with sequencing and dependencies
5.  Architect       → file-level implementation plan and spec
6.  Coder           → implements changes in the worktree
7.  Test Writer     → writes targeted unit tests for the changed code
8.  QA Engineer     → writes e2e + integration tests, fills coverage gaps
9.  Git Expert      → logs all commits, files changed, branch status
10. PR Reviewer     → reviews full diff, flags issues → Coder fixes → re-review
11. Standards Guardian → confirms all requirements and conventions satisfied
12. Draft PR created automatically (never ready-for-review)
13. Slack message sent to channel + Linear issue moved to "In Review"
14. Git Expert      → marks branch as review-ready in registry
15. Dashboard updates to show "Done — awaiting your review"

# At any point: "where are the changes for P-15200?"
# Git Expert answers immediately from registry + live git state.
```

For a **bug fix**, steps 5 (Architect) and 8 (QA Engineer) are skipped automatically.  
For a **hotfix**, only Coder + Test Writer + PR Reviewer + Git Expert run.

---

## Phase 9 — Claude Mobile Dispatch

Claude mobile (claude.ai on iOS/Android) can dispatch tasks to the orchestrator via two complementary paths. Both are set up — mobile uses whichever is available.

---

### Path A: Linear as the relay (preferred — no new infrastructure)

This uses the Linear MCP that's already connected. The orchestrator watches for a specific issue state and auto-dispatches when it sees one.

**How it works:**

```
1. You open Claude mobile and describe the task
2. Claude mobile uses the Linear MCP to create a Linear issue
   with label "agent-queue" and the task description as the body
3. The orchestrator's Linear poller (runs every 30s) detects the new issue
4. It reads the issue, infers the task type, and dispatches the crew automatically
5. When done, the crew moves the issue to "In Review" and posts the draft PR link
   — visible on your phone in the Linear app
```

**Orchestrator-side:** A `~/crew-orchestrator/linear_poller.py` service runs in the background, polls for `agent-queue` issues, dispatches them, and updates the issue state to prevent double-dispatch.

**On mobile:** Tell Claude mobile _"Create a Linear task for the agent queue: [description]"_. Claude formats and creates the issue via MCP. No special setup needed on the phone.

**Latency:** ~30 seconds from issue creation to crew start.

---

### Path B: Cloudflare Tunnel + direct HTTP dispatch (real-time, zero latency)

Exposes the local MCP server's `/dispatch` endpoint publicly over HTTPS via a free Cloudflare Tunnel. Claude mobile calls it directly with no relay.

**Setup (agent handles this):**

```bash
# Install cloudflared (free, no account required for quick tunnels)
brew install cloudflared

# Start a persistent named tunnel pointed at the local MCP server
cloudflared tunnel --url http://localhost:8765
# → returns a stable public URL, e.g. https://crew-dispatch.yourdomain.com
```

For a persistent (non-expiring) URL, the agent creates a free Cloudflare account tunnel and stores the credentials in `.env`:
```
CLOUDFLARE_TUNNEL_TOKEN=...
CREW_DISPATCH_URL=https://crew-dispatch.yourdomain.com
```

**The `/dispatch` endpoint accepts:**
```json
POST /dispatch
{
  "description": "Add CSV export to analytics page",
  "task_type": "feature",       // optional — orchestrator infers if omitted
  "linear_id": "P-15200"        // optional — links the crew to an existing issue
}
```

**On mobile:** Tell Claude mobile _"Dispatch a task: [description]"_. Claude formats the POST and calls the tunnel URL. The crew starts in under 2 seconds.

For quick access without typing, the agent also generates an **iOS Shortcut** that prompts for a task description and fires the POST — one tap from the home screen.

---

### Claude Mobile Project Setup

To make Claude mobile aware of the orchestrator, create a **Claude Project** on claude.ai with this system prompt:

```
You are connected to a local development orchestrator at {CREW_DISPATCH_URL}.

To dispatch a task:
- Use the /dispatch endpoint with a JSON body: { "description": "...", "task_type": "feature|bug|hotfix|refactor|chore" }
- If task_type is unclear, omit it and the orchestrator will infer it
- If a Linear issue ID is known (e.g. P-12345), include it as "linear_id"

To check task status:
- GET /status/{task_id}

To ask where changes are:
- GET /where?q={description or linear id}

Always confirm the dispatch was accepted (HTTP 200) before telling the user the task is running.
```

This gives Claude mobile the full context to dispatch, check status, and query the Git Expert — all from a phone conversation.

---

### Mobile Dispatch Flow (combined)

```
You (mobile): "Start working on the dark mode toggle, it's P-15301"

Claude mobile:
  → Calls POST /dispatch { "description": "dark mode toggle", "linear_id": "P-15301" }
  → Orchestrator responds: { "status": "dispatched", "task_id": "P-15301", "crew": "feature" }
  → Claude replies: "Done — full crew dispatched for P-15301. I'll notify you on
    Slack when it's ready for review."

[~15 minutes later]
Slack: "[CREW DONE] P-15301 · Dark mode toggle · Draft PR: github.com/... · Branch: feature/P-15301-dark-mode-toggle"
```

**Verify:** Send a dispatch from Claude mobile → crew appears in dashboard → Slack notification received on phone

---

## Phase 10 — LLM Key Configuration

The crews need API access:

All keys live in `~/crew-orchestrator/.env` — one file, outside every project repo:

- **Claude (Anthropic API):** `ANTHROPIC_API_KEY`
- **GitHub CLI:** `gh auth login` (needed for draft PR creation)
- **Slack:** `SLACK_BOT_TOKEN`
- **Linear:** `LINEAR_API_KEY`
- **Cloudflare Tunnel:** `CLOUDFLARE_TUNNEL_TOKEN` (generated during tunnel setup)

The agent checks for all keys at install time and reports which are missing before starting any work.

---

## Phase 11 — Continuous Orchestrator Improvement

The orchestrator improves itself over time using a feedback loop: every run is logged, patterns are detected, and improvement proposals become draft PRs on the orchestrator's own config files.

---

### 11a — Run Logging

Every crew run writes a structured log to `~/crew-orchestrator/runs/{project}/YYYY-MM-DD-{task_id}.json`:

```json
{
  "task_id": "P-15301",
  "task_type": "feature",
  "description": "dark mode toggle",
  "started_at": "2026-04-17T14:32:00Z",
  "completed_at": "2026-04-17T14:47:12Z",
  "duration_seconds": 912,
  "agents_used": ["planner", "architect", "coder", "test_writer", "qa_engineer", "pr_reviewer", "standards_guardian", "git_expert"],
  "token_usage": {
    "planner": { "input": 1200, "output": 180 },
    "architect": { "input": 2100, "output": 310 },
    "coder": { "input": 3400, "output": 890 },
    ...
  },
  "flags": [],
  "pr_url": "https://github.com/...",
  "outcome": "draft_pr_created"
}
```

**Flags** are mid-run signals any agent can emit when something feels wrong with the orchestrator config itself — not the task, but the crew setup. Examples:
- `"architect_needed_but_skipped"` — Coder found the task more complex than classified
- `"qa_test_gaps_found"` — QA found areas with no tests where the routing said test_writer was sufficient
- `"wrong_task_type"` — Standards Guardian believes the task was misclassified at kickoff

Flags accumulate and feed the retrospective.

---

### 11b — The Retrospective Agent

A dedicated `retrospective` meta-agent that reads run logs and proposes concrete improvements to the orchestrator config.

**Added to `agents.yaml`:**

```yaml
retrospective:
  role: Orchestrator Improvement Specialist
  goal: Analyse crew run logs, identify systematic weaknesses, and propose concrete
    config improvements that make the orchestrator faster, cheaper, and more accurate
  backstory: >
    You are a systems thinker who studies how the crew performs over time. You read
    run logs, token counts, duration data, agent flags, and PR outcomes. You identify
    patterns — agents that consistently over-spend, task types that are misclassified,
    routing rules that are too aggressive or too conservative. You propose specific,
    actionable changes to agents.yaml, tasks.yaml, and the routing matrix. Every
    proposal includes the evidence behind it and the expected improvement.
    Terse like caveman. Technical substance exact. Only fluff die.
  llm: claude-3-5-haiku  # reads structured logs — no deep reasoning needed
```

**What it analyses per retrospective run:**
- Token spend per agent per task type — identifies which agents consistently over-generate
- Duration outliers — flags agents that take disproportionately long
- Flag frequency — if `architect_needed_but_skipped` fires on 3+ bug fixes, add Architect to bug fix matrix
- PR Reviewer rejection rate — if Coder consistently needs rework on certain task types, model upgrade or prompt tweak
- Task type misclassification — if Standards Guardian consistently flags wrong type, improve routing logic
- Caveman compression ratios — if a compressed file has drifted from its original, re-compress

---

### 11c — Improvement Triggers

**Automatic — after every 10 runs:**
The orchestrator automatically queues a retrospective run. The retrospective agent reads the last 10 logs, produces a proposal, and opens a draft PR against `~/crew-orchestrator/` with the proposed changes.

**Manual — on demand:**
From Cursor: `improve_orchestrator` MCP tool  
From Claude mobile: _"Improve the orchestrator"_ → dispatches a retrospective run immediately  
From dashboard: "Run Retrospective" button

**Scheduled — weekly:**
A weekly cron job runs the retrospective on all logs from the past 7 days. Useful for catching slow-building patterns that don't show up in 10-run windows.

---

### 11d — Improvement PR Flow

The retrospective agent's proposals never auto-merge. They always become a draft PR:

```
~/crew-orchestrator/agents.yaml                           ← proposed changes (model swaps, prompt tweaks, new agents)
~/crew-orchestrator/tasks.yaml                            ← proposed pipeline changes
~/crew-orchestrator/ROUTING_MATRIX.md                     ← proposed task-type routing changes
~/crew-orchestrator/retrospectives/{project}/NNN.md       ← evidence report: what data drove each proposal
```

The PR body contains the full evidence: run log references, flag counts, token tables, and the specific line-level change proposed and why.

You review, accept or reject each change, and merge. The orchestrator evolves based on real run data, not guesses.

After any merge that touches `agents.yaml` or `tasks.yaml`, the agent automatically re-runs `caveman:compress` on the updated files.

---

### 11e — Improvement Scope

Things the retrospective agent can propose changes to:

| Config area | Example improvement |
|-------------|-------------------|
| Model assignment | Downgrade QA Engineer from sonnet to haiku if output quality is consistently sufficient |
| Routing matrix | Add Architect to hotfix flow if flags show it's needed |
| Agent prompts | Sharpen Planner's backstory if subtask lists are consistently too vague |
| Caveman level | Upgrade specific agents from full → ultra if their output is still too verbose |
| New agents | Propose adding a Performance Reviewer if perf regressions keep slipping through |
| Deprecated agents | Flag an agent as unused if it hasn't contributed meaningfully in 20+ runs |
| Token budget | Re-compress context files if they've grown beyond the compressed baseline |

Things it cannot propose automatically (requires your explicit decision):
- Removing an existing agent entirely
- Changing which MCPs an agent has access to
- Switching to a more expensive model

---

### 11f — Dashboard Integration

The dashboard (`localhost:8765`) gains a **Retrospective** tab:

| Column | Content |
|--------|---------|
| Run # | 42 |
| Runs analysed | 10 (P-15200 → P-15291) |
| Proposals | 3 changes |
| Status | Draft PR open / Merged / Pending |
| Token trend | ↓ 12% vs previous 10 runs |
| Top flag | `architect_needed_but_skipped` (×4) |

---

## Zero Footprint Rule

The orchestrator has **no presence inside `planable-app`** whatsoever — no files, no `.gitignore` entries, no `.git/info/exclude` entries, no config, nothing. The repo is completely unaware the orchestrator exists.

Everything lives in `~/crew-orchestrator/`:
- Agent and task config, run logs, branch registry, retrospectives, dashboard, scripts, `.env`, reference docs, this plan, the dashboard mockup

The orchestrator reads from `planable-app` (skills, rules, CLAUDE.md, package.json scripts) but never writes to it. The only writes it makes are into worktrees — which are sibling directories outside the repo.

**How planable-app standards are enforced without touching the repo:**
The orchestrator's `projects.yaml` entry for planable-app declares every skill path, rule file, and command the agents must use. When a crew run starts, the orchestrator reads these files directly from the project and injects the relevant constraints into each agent's context. The project itself doesn't need to know this is happening.

Everything else the orchestrator creates goes into worktrees (sibling directories outside the repo) or into `~/crew-orchestrator/` directly.

---

## Future Improvements (not in this plan)

- Cursor extension for one-click task kickoff with Linear issue auto-filled
- Agent retry loop: PR Reviewer flags → Coder fixes → Reviewer re-checks (already partially in Phase 8 step 10)
- Configurable agent overrides per task (e.g., force Architect on a bug fix)

---

## Execution Order (for the agent to follow)

| Step | Action | Verify |
|------|--------|--------|
| 0 | Verify `planable-app` is completely untouched — no new files, no gitignore changes, no exclude entries | `git status` and `git diff` in planable-app are clean before and after orchestrator setup |
| 1 | Create `~/crew-orchestrator/` git repo + install Python deps, GitHub CLI, cloudflared, Caveman | Dir exists, `crewai --version`, `gh --version`, `cloudflared --version`, Caveman in Cursor |
| 2 | Create `agents.yaml` + `tasks.yaml` + `projects.yaml` in `~/crew-orchestrator/` | Files exist, valid YAML, planable-app registered |
| 2a | `caveman:compress` on `agents.yaml`, `tasks.yaml`, and `planable-app/CLAUDE.md` | Compressed files exist, originals backed up |
| 3 | Implement task-type routing logic | Matrix applied correctly on test inputs |
| 4 | Run a smoke test crew (bug fix type — minimal agents) | End-to-end completes, worktree created as sibling dir |
| 5 | Create `~/crew-orchestrator/scripts/new-task.sh` | Worktree created outside project on test branch |
| 6 | Implement draft PR auto-creation | Draft PR appears in GitHub after test run |
| 7 | Set up Slack + Linear notifications | Messages received after test crew |
| 8 | Build crew dashboard in `~/crew-orchestrator/dashboard/` | `localhost:8765` shows live crew row |
| 9 | Create `~/crew-orchestrator/mcp_server.py` + register globally in `~/.cursor/mcp.json` | All four tools visible in Cursor |
| 10 | Set up Cloudflare Tunnel + `/dispatch` endpoint | Public URL reachable, returns 200 on test POST |
| 11 | Set up `~/crew-orchestrator/linear_poller.py` | Issue with `agent-queue` label triggers crew within 30s |
| 12 | Configure Claude mobile Project with system prompt | Claude mobile dispatches a test task end-to-end |
| 13 | Run full end-to-end test from mobile (feature type) | All agents run, PR created, Slack notif received on phone |
| 14 | Set up run logging to `~/crew-orchestrator/runs/planable-app/` | Log file written after test crew run |
| 15 | Add `retrospective` agent to `~/crew-orchestrator/agents.yaml` | Agent present, valid YAML |
| 16 | Wire automatic trigger (every 10 runs) + `improve_orchestrator` MCP tool | Retrospective fires after 10th test run, draft PR opened on `~/crew-orchestrator/` |
| 17 | Add Retrospective tab to dashboard | Tab visible, shows run stats and proposal status |
