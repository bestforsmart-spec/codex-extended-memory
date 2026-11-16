# Extended Memory for Codex

[Русский](https://github.com/bestforsmart-spec/codex-extended-memory/blob/main/README.ru.md) · **English**

Every new Codex session starts from a blank slate: you explain again who you are, how the project is set up and what you have already tried. This package gives Codex **long-term memory**. It runs entirely on your computer, sends nothing out and installs with one command. Claude Code and other agents can connect to the same memory: what one writes, another finds.

Codex has built-in memory too, but it is off by default and works differently — see the comparison [below](#how-it-differs-from-the-built-in-codex-memory).

> **Languages.** The package speaks English and Russian. The installer picks one from your system language, and `memory lang en` or `memory lang ru` switches it at any time. The author uses it daily in Russian; the English setup is checked by the automated tests.

## What you get

- **Codex remembers you and your rules.** The main memory file, `MEMORY.md`, is added to the start of every session.
- **Codex remembers what you did before.** A journal of decisions, automatic records of significant turns and the history of past Codex sessions. Search works by keywords and by meaning: "emails go to spam" finds the note about the SMTP relay.
- **Codex writes things down itself.** Results of the work, decisions and the reasons behind them, project state. If the agent forgets, a safety-net hook saves the outcome of a significant turn.
- **One memory for all agents.** Claude Code, Claude Desktop, Cursor, Gemini CLI and other MCP-capable agents connect to the same memory. Agree on something in one, and the other already knows.
- **The memory is yours, not a service's.** It is ordinary files on your disk. If your account is blocked, your subscription ends or you switch agents, the memory stays with you and can be connected to another agent.
- **No tokens spent.** A local model sorts records by meaning and searches them: neither tokens nor subscription limits are used for that. Only the short main memory and the fragments that were found go into the agent's context.
- **Private.** Everything lives in `~/.codex-memory` (on Windows, `%USERPROFILE%\.codex-memory`). Semantic search runs on a local model through Ollama. Passwords, tokens and keys are scrubbed automatically.
- **macOS, Linux and Windows.** There is an installer for each system.
- **In English and Russian.** The installer picks the language from your system, and one command switches it.

## How it works

```
┌──────────────────────── Codex ──────────────────────────┐
│ session start ──► SessionStart hook ──► adds MEMORY.md + the recent journal
│ during work ────► MCP server extended-memory:
│                    memory_search / memory_show        — recall
│                    memory_remember / memory_curate    — write down
│                    memory_forget                      — forget
│ end of turn ────► Stop hook ──► auto-record "request → result" (significant turns only)
└─────────────────────────────────────────────────────────┘
                              │
~/.codex-memory/
├── MEMORY.md            main memory — short, visible in every session
├── memory/YYYY-MM-DD.md journal (memory_remember)
├── memory/auto/         automatic records from the Stop hook
├── memory/history/      history of past Codex sessions
├── index-roots.json     what to index and which model computes meaning
└── .index/              search index: words (SQLite FTS5) + meaning (bge-m3 vectors)
```

Instructions for the agent (when to recall and what to write down) are added to `~/.codex/AGENTS.md`.

## Installation (about 5 minutes)

**You need:** macOS, Linux or Windows 10/11, Codex (app or CLI) and Python 3.8+. **Recommended:** Ollama for semantic search. Without it the memory still works, but it searches by keywords only.

The installer finds Python and downloads the `bge-m3` semantic search model (about 1.2 GB, once). It registers the memory in the Codex settings, after saving copies of them in the `backups` folder inside the memory folder. Then it loads the significant turns of your Codex sessions from the last 120 days into the memory and turns on background index updates.

**Where to get the package.** Clone the repository — `git clone https://github.com/bestforsmart-spec/codex-extended-memory.git` — or download it as an archive: the **Code → Download ZIP** button on the [repository page](https://github.com/bestforsmart-spec/codex-extended-memory). The folder in that archive is called `codex-extended-memory-main` — use that name in the steps below.

### macOS and Linux

On a Mac, Python is already there if the Command Line Tools are installed; if not, the installer tells you what to do.

1. *(Recommended)* Install Ollama: `brew install ollama`, or download the app from [ollama.com](https://ollama.com) and start it.
2. Unpack the archive and run the installer in Terminal:

   ```bash
   cd ~/Downloads/codex-extended-memory
   ```
   ```bash
   bash install.sh
   ```

### Windows

1. *(Recommended)* Install Ollama: download the installer from [ollama.com/download](https://ollama.com/download) or run `winget install -e --id Ollama.Ollama` in PowerShell.
2. If Python is not installed yet: `winget install -e --id Python.Python.3.13`, or the installer from [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
3. Unpack the archive **completely** (right-click → "Extract All"), open the `codex-extended-memory` folder and double-click `install.cmd`. Windows may ask whether to run a file from the internet — allow it. The same from a terminal, inside that folder:

   ```powershell
   .\install.cmd
   ```

The memory is installed to `%USERPROFILE%\.codex-memory`, and two Task Scheduler tasks (`CodexMemorySync` and `CodexMemoryMaintain`) update the index in the background; administrator rights are not needed. If your Codex runs inside WSL, install the memory there, as on Linux: `bash install.sh`.

Windows support arrived in version 1.1.0. Installation, hooks, the memory server, background tasks and semantic search are checked by [automated tests](https://github.com/bestforsmart-spec/codex-extended-memory/actions) on Windows Server 2025. If something does not work on your computer, write to me (contacts at the end) and I will fix it.

### After installation

1. **Restart Codex completely**: quit the app (Cmd+Q on a Mac) and open it again.
2. Codex will show **"Hooks need review"**. Choose **"Trust all and continue"**. Without this the memory will not be added to the start of a session or saved automatically. If you missed the prompt: the `/hooks` command in the CLI, or the hooks section of the app settings.
3. Check the installation — every item should have a ✓:

   ```bash
   ~/.codex-memory/bin/memory doctor
   ```

   On Windows (PowerShell):

   ```powershell
   & "$HOME\.codex-memory\bin\memory.cmd" doctor
   ```
4. Ask Codex: "what do you remember about me?"

Installer options: `bash install.sh --help` (on Windows, `.\install.cmd --help`). For example, `--no-history` skips loading past sessions, `--no-embeddings` installs without Ollama, `--history-days 365` loads a year of history, and `--lang ru` installs in Russian.

### Language

The memory runs in English or in Russian: that is the language of the installer and `memory` messages, of the `MEMORY.md` template and of the agent instructions in `AGENTS.md`. The installer chooses it for you: Russian on a Russian system, English on any other. To set the language at install time, add `--lang en` or `--lang ru`.

To switch later:

```bash
~/.codex-memory/bin/memory lang ru
```

The command switches the messages and rewrites the agent instructions; restart Codex afterwards. To go back, run `memory lang en`. Your records are not changed by the switch, and you can keep them in any language. The language choice arrived in version 1.2.0; after an upgrade from an earlier version the memory stays in Russian until you switch it yourself.

## How to use it

Just work as before. Codex searches the memory before tasks and writes down the results on its own. Useful phrases:

- "remember: deploy only through GitHub Actions" / "remember about me: I'm a backend developer, I write Go"
- "remember: we write to clients formally, no emoji" / "remember: we pay supplier Alpha after 14 days"
- "what do you remember about project X?", "how did we fix error Y last time?"
- "forget that we use MySQL — we moved to PostgreSQL"

**First of all, fill in "About me" and "Working rules"** in `~/.codex-memory/MEMORY.md`: 5–10 lines about yourself, your projects and how to work with you. You can simply tell Codex. Keep the main memory short: it goes into every session (up to ~9,000 characters), and everything else is found by search.

### Not only for code

The memory is not tied to programming: it keeps what you told it and what you and the agent have already done. What is worth telling it if you are not a developer:

- **Your business.** Company details, rates and document templates, suppliers and prices, what you agreed with clients.
- **Writing and marketing.** Brand tone and banned words, briefs, what has already been published, which hypotheses you tested.
- **Reports and finance.** Where the data lives, the report template, how to map line items.
- **Study and research.** The topic and work plan, sources you have read, formatting requirements.
- **Personal matters.** Your expense categories, how you decided things last year, where the documents are.

You don't need to tell it secrets — passwords, keys, card details: the memory scrubs them anyway.

## One memory for all agents

The memory server is an ordinary local MCP server, so other agents can be connected to the same `~/.codex-memory` folder. They search and write through the same `memory_*` tools: what Codex wrote, Claude finds, and vice versa. One local model computes the meaning of queries for all of them.

**Claude Code:**

```bash
claude mcp add extended-memory --scope user -e CODEX_MEMORY_HOME=$HOME/.codex-memory -- python3 $HOME/.codex-memory/app/mcp_server.py
```

**Gemini CLI:**

```bash
gemini mcp add -s user -e CODEX_MEMORY_HOME=$HOME/.codex-memory extended-memory python3 $HOME/.codex-memory/app/mcp_server.py
```

**Claude Desktop, Cursor and others** connect the server with a block in their MCP settings file. This command prints a ready block with your paths:

```bash
python3 -c 'import json,os;h=os.path.expanduser("~/.codex-memory");print(json.dumps({"mcpServers":{"extended-memory":{"command":"python3","args":[h+"/app/mcp_server.py"],"env":{"CODEX_MEMORY_HOME":h}}}},indent=2))'
```

Paste it into the settings file: in Claude Desktop it opens via Settings → Developer → Edit Config; for Cursor it is `~/.cursor/mcp.json`. If the file already has an `mcpServers` section, add only the `extended-memory` entry to it. Restart the agent after editing.

**On Windows** the same commands look like this (PowerShell):

```powershell
claude mcp add extended-memory --scope user -e CODEX_MEMORY_HOME="$HOME\.codex-memory" -- python "$HOME\.codex-memory\app\mcp_server.py"
```
```powershell
gemini mcp add -s user -e CODEX_MEMORY_HOME="$HOME\.codex-memory" extended-memory python "$HOME\.codex-memory\app\mcp_server.py"
```
```powershell
python -c "import json,os,sys;h=os.path.join(os.path.expanduser('~'),'.codex-memory');print(json.dumps({'mcpServers':{'extended-memory':{'command':sys.executable,'args':[os.path.join(h,'app','mcp_server.py')],'env':{'CODEX_MEMORY_HOME':h}}}},indent=2))"
```

Good to know:

- **Automation is set up only for Codex.** Adding the memory to the start of a session, auto-recording turns and loading history work in Codex. Other agents find what is already in the memory and write to it when you say "remember…" or when they decide to.
- **Tell the agent how to use the memory.** Copy the "Extended memory" section from `~/.codex/AGENTS.md` into the other agent's instructions file (for Claude Code, `~/.claude/CLAUDE.md`) — then it will search the memory before a task and write down results itself.
- **What has been tested.** The package is in daily use with Codex on macOS. The commands for Claude Code and Gemini CLI were checked on current versions on macOS. Installation on Windows and Linux is checked by automated tests, in both languages. Other agents connect local MCP servers according to their own documentation.

## How it differs from the built-in Codex memory

Codex has its own memory (Memories). It is off by default, and it collects summaries of past sessions in the background rather than keeping records as you work.

| | Built-in memory | Extended memory |
|---|---|---|
| When it writes | in the background, after the chat has been idle | right at the end of a significant turn |
| How it finds | a general summary and reading files; semantic search is not described | by keywords and by meaning, with stemming for English and Russian |
| How much it remembers | by default, sessions from the last 30 days | no expiry: the journal and history are not deleted |
| Forgetting one thing | the documentation describes only a full memory reset | targeted: "forget that…" |
| The files | generated state, editing is not advised | plain markdown, you can edit it |
| What computes it | the Codex model, out of your subscription limit | a local model, no limits spent |
| Whose memory | works only in Codex | your files, which connect to any MCP-capable agent |

Checked against the Codex documentation on 15 November 2026 (CLI 0.167). The built-in memory is developing quickly — check the current version.

## Command line

All commands: `~/.codex-memory/bin/memory --help`. A handy alias: `alias memory=~/.codex-memory/bin/memory`.

On Windows the command is called `memory.cmd`: in PowerShell, `& "$HOME\.codex-memory\bin\memory.cmd" --help`. An alias for the current session: `Set-Alias memory "$HOME\.codex-memory\bin\memory.cmd"`.

| Command | What it does |
|---|---|
| `memory status` | how much is in the memory, whether semantic search works |
| `memory search "query"` | search as Codex sees it |
| `memory remember "fact"` | write to the journal |
| `memory curate "fact"` | write to the main memory |
| `memory forget "fragment"` | delete an item (a copy is kept in `.state/forgotten.md`) |
| `memory add-folder ~/Notes` | add your own notes folder to search (Obsidian and the like) |
| `memory import-codex-history --days 365` | load Codex history for a longer period |
| `memory context` | show what is added to the start of a session |
| `memory sync` | reindex now (usually not needed: it happens on its own) |
| `memory maintain` | scrub secrets and duplicates (runs on its own every night) |
| `memory lang ru` | switch the language of the messages and the agent instructions: `ru` or `en` |
| `memory doctor` | check the installation |

## Privacy and data

- Everything is stored in `~/.codex-memory`, accessible only to your user. Nothing leaves your computer: vectors are computed by local Ollama.
- Strings that look like secrets (API keys, tokens, passwords, private keys, JWTs) are replaced with `[REDACTED]` both when written and when results are returned.
- Notes are ordinary markdown files: you can read and edit them in any editor.
- Backup: copy the `~/.codex-memory` folder (you can skip `.index`; the index rebuilds itself).
- Moving to another computer: install the package and copy `MEMORY.md` and `memory/` there.

## Troubleshooting

Start with `~/.codex-memory/bin/memory doctor` (on Windows, `& "$HOME\.codex-memory\bin\memory.cmd" doctor`): it shows what exactly is wrong.

| Symptom | What to do |
|---|---|
| Codex does not see the `memory_*` tools | Restart Codex completely. In the CLI, the `/mcp` command lists the connected servers. |
| The memory is not added to sessions, there are no auto-records | The hooks have not been trusted: `/hooks` → Trust in the CLI; in the app, confirm the hooks in settings. |
| "semantic search unavailable" | Ollama is not running: open the Ollama app or run `brew services start ollama`. |
| Codex asks for confirmation on every write | An old Codex version without `approval_mode`: update Codex and run the installer again. |
| Windows: `install.cmd` says "Python 3.8 or newer is required" | Python is not installed or cannot be found: install it (step 2 of the installation), open a new terminal window and run `install.cmd` again. |
| Windows: `install.cmd` says "The archive is not fully extracted" | The file was run straight from the archive. Extract the archive completely and run `install.cmd` from the extracted folder. |
| Windows: Codex runs in WSL and does not see the memory | Install the memory where Codex runs: inside WSL, with `bash install.sh`. |
| Search does not find a fresh record | `memory sync`, then `memory status`: check how many vectors have been computed. |
| Another agent does not see the `memory_*` tools | Check that the paths in its MCP settings are full, without `~` or `$HOME`, and restart the agent. |
| Something broke in the Codex settings | Copies of the original files are in `~/.codex-memory/backups/<date>/`. |

Background task logs are in `~/.codex-memory/logs/`.

## Uninstalling

```bash
bash ~/.codex-memory/uninstall.sh
```

On Windows (PowerShell):

```powershell
& "$HOME\.codex-memory\uninstall.cmd"
```

The command removes the memory from the Codex settings, the background tasks and the program itself. Your notes and `MEMORY.md` stay. To delete them as well, add `--purge`: on a Mac the data goes to the Trash; on Linux and Windows the folder is renamed, and you delete it yourself. Disconnect the memory from other agents in their own settings: for Claude Code, `claude mcp remove extended-memory --scope user`.

## For the curious

- **Search:** SQLite FTS5 with Porter stemming for English and a light Russian stemmer, plus `bge-m3` vectors through Ollama. The final ranking blends semantic similarity with keyword and heading matches. Each list item is indexed separately, so facts do not blur together.
- **A different model or embedding server:** `ollama pull qwen3-embedding:8b`, then `memory embeddings on --model qwen3-embedding:8b`. That model is heavier and slower, and with per-item chunking (as here) it was not more accurate — tested; `bge-m3` is the better default. Any OpenAI-compatible `/v1/embeddings` endpoint works too: see the `endpoint` and `apiKeyEnv` fields in `index-roots.json`. After a model change the vectors are recomputed automatically.
- **Dependencies:** only the Python standard library, no pip or npm. If `numpy` is installed, search over a large index is faster.

## Author and contact

Made by Emil Kaimov. Questions and ideas — on Telegram: [t.me/bestforsmart](https://t.me/bestforsmart). Source code and bug reports — on GitHub: [bestforsmart-spec/codex-extended-memory](https://github.com/bestforsmart-spec/codex-extended-memory).
