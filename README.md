# Hermes Mattermost Enhancer Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Hermes](https://img.shields.io/badge/Hermes-%E2%89%A5%20v2026.9-blue)](https://github.com/nousresearch/hermes-agent)
[![Release](https://img.shields.io/github/v/release/colin-chang/hermes-plugin-mattermost-enhancer?label=release)](https://github.com/colin-chang/hermes-plugin-mattermost-enhancer/releases)

English Version | [中文版本](./README.zh-CN.md)

Makes your Hermes AI assistant smarter, safer, and easier to use inside Mattermost.

---

## 😵‍💫 What Is This?

**In one sentence:** If you use Hermes in Mattermost, this plugin makes everything work better.

Hermes is an AI assistant you chat with in Mattermost to get things done. But the vanilla Hermes has some rough edges — it runs dangerous commands without asking, switching AI models requires editing config files, the WebSocket keeps disconnecting…

This plugin "retrofits" these capabilities onto Hermes so everything just feels right.

---

## ✨ What Can It Do?

### 🛡️ 1. Dangerous Command Approval (DM Card Confirmation)

**Scenario:** You ask Hermes to run a command like `rm -rf some-folder` or `DROP TABLE`. Once executed, there's no undo — if the AI misunderstood you, the consequences are real.

**Before:** Hermes runs it instantly. You only see the result in chat — too late to stop it 😱

**Now:** When Hermes is about to execute a dangerous command, it won't act immediately. It sends you a **private DM confirmation card** with 4 buttons:

| Button | Effect |
|--------|--------|
| **Allow Once** | Approve this time only; ask again next time |
| **Allow This Session** | Approve for the rest of this conversation |
| **Always Allow** | Never require approval for this command again |
| **Deny** | Refuse — don't run it |

![Approval card effect](images/approve.webp)

Click any button and it takes effect immediately — all within Mattermost, no window switching.

---

### 🧠 2. Switch AI Models (`/model` Command)

**Scenario:** You have multiple AI models available — some excel at coding, some at conversation, some are cheaper, some faster. You want to pick the right model for each task.

**Before:** You had to edit `~/.hermes/config.yaml` and restart the Gateway — tedious 💀

**Now:** Type `/model` in any Thread and a dropdown card appears:

![Model switching card effect](images/model.webp)

The dropdown lists all your available models. Pick one — this Thread immediately switches to the new model, **other Threads are unaffected**.

Thread A uses Model X for coding; Thread B uses Model Y for chatting. No interference.

> 💡 **Tip: Channel → Thread Model Inheritance**
>
> If you first run `/model` in the **channel's main timeline** to pick a model, then hit Reply to create a new Thread, that Thread will **automatically inherit** your chosen model — no need to switch again.
>
> Example: you type `/model` in `#dev` channel and select `deepseek-v4-pro`, then reply to any message (creating a Thread). That new Thread automatically uses `deepseek-v4-pro`.
>
> **Doesn't affect existing Threads** — if a Thread already uses a different model, inheritance won't overwrite it. Each Thread can still independently `/model` back to any model.

---

### 🔄 3. Reset Conversation (`/new` Command)

**Scenario:** The conversation has gone off track and the AI keeps fixating on an earlier topic. You want a fresh start.

**Before:** No way out — either start a new Thread or endure the AI's "memory" 💀

**Now:** Type `/new` and a confirmation card appears:

![New session card effect](images/new.webp)

After confirming:
- ✅ The Thread's model override is cleared (back to default)
- ✅ Hermes' "memory" is wiped (like a brand-new conversation)
- ✅ Session state is reset

---

### ⌨️ 4. Typing Indicator

**Scenario:** You're waiting for Hermes to reply in a Thread and want to know it's thinking.

**Before:** The "typing..." indicator appeared at the **channel** level, not in the Thread you're watching. You thought it was stuck 😕

**Now:** The typing indicator correctly appears in your current Thread — you know it's processing your request ✅

![Typing indicator example](images/typing.webp)

---

### ❓ 5. AI Asks You Questions (Interactive Cards)

**Scenario:** Hermes hits a decision point during a complex task — "This file has two processing approaches: A is fast but rough, B is slow but precise. Which one?" Or an open-ended question like "What approach would you prefer?"

**Before:** Hermes drops a plain-text line "Please reply: 1. A 2. B" into the Thread, easily missed in the conversation flow. You don't see it → Hermes hangs waiting 💀

**Now:** When Hermes asks a question, it posts a **prominent interactive card** with clickable buttons for each option. A "✍️ Other" button lets you type a free-form answer:

![AI question interactive card](images/clarify.webp)

- Click an option button → takes effect immediately; the card updates in place, keeping the original question and all options (✅ marks your pick) so history stays reviewable
- Click "✍️ Other" → card prompts "Type your answer below" — just type in the chat
- Open-ended questions (no choices) → shows the question text, just type your answer

Everything happens inside Mattermost — no window switching, no commands to memorize.

---

### 🏷️ 6. Reply Footer (Model & Context)

**Scenario:** You have multiple Threads open, each potentially using a different AI model. Mid-conversation you think: "Wait, which model is this Thread using?"

**Before:** No such feature. You had to type `/model` and check the dropdown to know 💀

**Now:** Every Hermes reply automatically includes a one-line footnote showing the current model and context usage:

![Model footer effect](images/footer.webp)

Gray monospace footnote — subtle, glanceable. **Especially useful with multiple Threads** — each Thread's footer shows its own model, no confusion.

> 💡 **How to Enable**
>
> This feature builds on Hermes' built-in `runtime_footer`. Add this to the `display` section of `~/.hermes/config.yaml`:
>
> ```yaml
> display:
>   runtime_footer:
>     enabled: true
>     fields:
>       - model
>       - context_pct
> ```
>
> Then restart the Gateway. Only `model` (model name) and `context_pct` (context usage %) are included — clean and concise.
>
> If you're using Hermes CLI (not Gateway), you can also toggle it with `/footer on`.

---

---

## 🐛 What Bugs Are Fixed?

Below are 11 bugs fixed by this project (plugin + companion shell script). Each includes the **real-world impact** so you can tell if you've encountered them.

| # | Bug Description | Real-World Impact | After Fix | Implementation |
|---|----------------|-------------------|-----------|---------------|
| **1** | Typing indicator at wrong level: the "typing..." indicator appears at the channel while Hermes is thinking in a Thread | You wait in a Thread with no typing feedback | Typing correctly appears in the current Thread | Adapter Override |
| **2** | Missing file spam: Hermes posts long error messages when an image/file can't be found | Chat flooded with `File not found: /tmp/xxx.png`, disrupting conversation | Silently skipped — no noise | Adapter Override |
| **3** | AI questions too subtle: Hermes asks a question (multiple choice or open-ended) as plain text, easily missed in the conversation flow | You miss the question → no response → AI times out → also triggers a session split 💀 | Questions rendered as interactive cards with buttons — prominent and clickable | Adapter Override |
| **4** | WebSocket frequent disconnects: Mattermost WebSocket disconnects every ~50s (close 258) | Brief message loss, duplicate messages, reply lag | Heartbeat optimized to 15s, connection stable | Adapter Override |
| **5** | Media not routed to Thread: generated images/audio/video/documents appear in the main channel instead of the current Thread | You ask for an image in a Thread → image pops up in the channel, breaking the conversation flow 💀 | All media (images, audio, video, documents) correctly appear in the current Thread | ~~Adapter Override~~ Fixed upstream (v2026.7.30+) |
| **6** | DM approval missing user_id: Hermes can't determine which user to send the approval DM to | Approval cards may not arrive; dangerous commands may execute without approval | user_id properly passed; cards delivered on time | Adapter Override |
| **7** | Tool progress not routed to Thread: multi-step task progress ("Searching...", "Reading file...") only appears in the main channel | You wait in a Thread with zero visibility into progress — result just pops out at the end 💀 | Progress messages correctly appear in the current Thread; you see every step in real time | ~~Shell Patch~~ Fixed upstream |
| **8** | Session split (AI amnesia): When AI is waiting for your clarify reply, your next message starts a new conversation — AI forgets everything | You're chatting fine in Thread A, then suddenly AI doesn't recognize you and gives random answers | Messages correctly delivered to the waiting AI; no new session created | Shell Patch (E-P2) |
| **9** | Clarify concurrency guard: during clarify waiting, simultaneously arriving messages can bypass the session guard and create duplicate sessions | Two AI agents start responding to the same Thread — confusing duplicate replies | Intercepted at session guard before a new session spawns | ~~Shell Patch~~ Fixed upstream |
| **10** | Auto-resume session leaking: after Gateway restart, multiple Thread sessions in the same channel auto-resume simultaneously, responses cross between Threads | You see unrelated AI responses appearing in the wrong Thread after a restart | Deduplicates to most recent session per channel; Threads stay isolated | Shell Patch (E-P4) |
| **11** | Response fragmentation: AI replies split into multiple separate messages (commentary and body sent separately) | One reply arrives as 3-5 messages, poor reading experience | Commentary merged into stream, one message does the job | Shell Patch (main script) |

> 💡 **Bug #11** is fixed by the main `hermes-patches.sh` script (P50 commentary merge), not this plugin's companion script. See the main script in `~/.hermes/scripts/hermes-patches.sh`.
>
> 💡 There are also two additional upstream bugs fixed via the main script: ghost empty code fences in long code blocks (P53), and stream fallback messages not routed to Threads (P55). These are uncommon but included in the same `hermes-patches.sh`.

---

---

## 🧱 Plugin vs. Companion Script — How to Understand It?

You may have noticed this project contains both a **plugin** and a **companion shell script** (`scripts/hermes-mattermost-enhancer.sh`). Here's a plain-language explanation.

### How Hermes Works

Think of Hermes as an **intelligent robot** 👤:

```
You ──→ Mattermost ──→ Hermes Gateway (robot hub) ──→ AI Brain
                              │
                              ├── Plugin: adds new skills to the robot
                              └── Source code: the robot's "skeleton" — can't change
```

### What the Plugin Can (and Can't) Do

A plugin is like installing an app on your phone — it adds features and improves the experience, but can't modify the phone's operating system.

- ✅ **What the plugin can change:** How the robot "replies to you" (adapter methods) — Bugs #1-6 are all implemented via adapter overrides and take effect automatically when the plugin is installed
- ❌ **What the plugin can't touch:** How the robot "gets woken up" (caller-side code) — this is deep in Hermes' source code

Bugs marked **Shell Patch** in the table above (#7-10) are exactly in that untouchable caller-side code.

### What the Companion Script Does

It fixes those plugin-unreachable bugs by modifying Hermes' source files with minimal changes:

- **Companion script** (`scripts/hermes-mattermost-enhancer.sh`): Gateway-level patches specific to Mattermost interaction — clarify session handling (E-P2), auto-resume dedup (E-P4). Retired with upstream releases: E-P1 (progress in Thread), E-P3 (concurrency guard), E-P5 (status routing)
- **Main script** (`~/.hermes/scripts/hermes-patches.sh`): Platform-agnostic Gateway fixes — commentary merge, ghost code fences, stream fallback reply routing, plus CLI-level fixes (custom provider, model whitelist, cron encoding)

> ⚠️ **Status:** These patches are maintained as local fixes. Some have been submitted upstream but are not yet merged into official Hermes releases. Running `check` after each Hermes upgrade is recommended — once upstream merges them, the scripts will report "already applied" and you can skip them.
>
> ⚠️ **The companion script does not restart the Gateway for you:** it contains no restart call (restarting from inside the gateway session is blocked by Hermes' safety hook, which would kill the whole script). After `apply`, restart the Gateway manually from an external terminal.

![Patch script output](images/patch.webp)

### Which One Do I Need?

**All three.** Install the plugin (adapter overrides + features), then run both scripts for the low-level bug fixes:

```bash
# 1. Plugin (adapter overrides — #1-6)
hermes plugins install colin-chang/hermes-plugin-mattermost-enhancer --enable

# 2. Companion script (Mattermost Gateway patches — #7-10)
cd ~/.hermes/plugins/mattermost-enhancer
./scripts/hermes-mattermost-enhancer.sh apply

# 3. Main script (platform-agnostic fixes — #11 + CLI fixes)
~/.hermes/scripts/hermes-patches.sh apply
```

> 💡 In the future, Hermes upstream may merge these fixes in, making the scripts unnecessary. Running `check` will then show "already applied" and you can ignore them.

---

---

## 🚀 Quick Start (4 Steps)

### Prerequisites

- ✅ Running [Hermes Agent](https://github.com/nousresearch/hermes-agent) (v2026.9+)
- ✅ Mattermost server with Bot account configured
- ✅ Python 3.11+

---

### Step 1: Install the Plugin

```bash
hermes plugins install colin-chang/hermes-plugin-mattermost-enhancer --enable
```

### Step 2: Register Mattermost Slash Commands

In **Mattermost System Console → Integrations → Slash Commands**, add two:

| Command | Request URL | Purpose |
|---------|-------------|---------|
| `/model` | `http://<your-hermes-host>:18065/mm-command` | Switch AI model |
| `/new` | `http://<your-hermes-host>:18065/mm-command` | Reset session |

> 🔧 If Mattermost and Hermes are on the same machine (Docker deployment), use `http://host.docker.internal:18065/mm-command`

### Step 3: Configure Environment Variables

Open `~/.hermes/.env` and add:

```bash
# ═══ Required ═══
# Callback server bind address and port
MATTERMOST_CALLBACK_BIND=0.0.0.0
MATTERMOST_CALLBACK_PORT=18065

# Callback URL — Mattermost uses this to send button clicks / dropdown selections back to Hermes
# 🔧 Docker deployment (Mattermost in container): MUST use host.docker.internal
MATTERMOST_CALLBACK_URL=http://host.docker.internal:18065/mattermost/callback
# 💻 Local deployment (Mattermost + Hermes on same machine, no Docker):
#    Can leave blank — plugin auto-falls-back to http://127.0.0.1:18065/mattermost/callback

# ═══ Optional ═══
# HMAC signature verification (skips verification if left empty)
# MATTERMOST_CALLBACK_SECRET=your-secret
```

> ⚠️ If you're like most self-hosting users with Mattermost running in Docker, **`MATTERMOST_CALLBACK_URL` must be set**. Without it, the Docker container can't reach Hermes on the host machine.

### Step 4: Run Companion Script + Restart

**When do you need to run this?**
- ✅ **First install**: required
- ✅ **After Hermes upgrade**: upgrades may overwrite source fixes — run `check` to confirm status
- ✅ **When features act up**: approval cards not arriving, progress messages not in Thread → run `check` to diagnose
- ❌ **During normal use**: no need to re-run

```bash
cd ~/.hermes/plugins/mattermost-enhancer

# First, check current status (see if all patches are applied)
./scripts/hermes-mattermost-enhancer.sh check
```

If `check` shows patches not applied, run:

```bash
# Apply patches (will automatically ask if you want to restart immediately after)
./scripts/hermes-mattermost-enhancer.sh apply
```

> 💡 Don't forget the main script too: `~/.hermes/scripts/hermes-patches.sh apply` for the platform-agnostic fixes.

🎉 **Done!** Now go to Mattermost and try `/model` or run a dangerous command to see the approval card.

---

---

## 📖 Usage Guide

### Switching AI Models

1. Type `/model` in any Thread and send
2. A dropdown card appears listing all available models
3. Select the model you want from the dropdown
4. The current Thread switches immediately — your next question uses the new model

> 💡 Switching only affects the current Thread. Other Threads keep their original model. Want to switch back? Just `/model` again.

### Resetting a Conversation

1. Type `/new` and send
2. A confirmation card appears
3. Click confirm — everything resets

> 💡 `/new` doesn't delete chat history — it just makes the AI "forget". Previous messages remain in the Thread for viewing.

### Approving Dangerous Commands

This is automatic — no manual trigger needed.

When Hermes is about to run a dangerous command:

1. You receive an approval card in your **private DM**
2. Choose one of the buttons:
   - **Allow Once** — approve this one time
   - **Allow This Session** — valid for this conversation
   - **Always Allow** — permanently approve this command
   - **Deny** — refuse
3. The button disappears instantly; Hermes receives your decision and acts on it

### AI Asking You Questions

This is also automatic. When Hermes needs you to make a decision:

1. A prominent interactive card appears in the current Thread
2. Click the option button you want → takes effect immediately
3. Or click "✍️ Other" → type your answer directly in the chat box
4. For open-ended questions → no buttons shown, just type your answer

### Viewing the Current Model

This is also automatic — no manual trigger needed.

Once enabled, every Hermes reply ends with a footer showing the current model and context usage, formatted as `── deepseek-v4-pro 34% ──`. A gray monospace footnote that doesn't disrupt reading.

Want to turn it off? Set `display.runtime_footer.enabled` to `false` in `config.yaml` and restart.

---

---

## ❓ FAQ

**Q: Do I need to install the plugin and both scripts?**

A: Yes. The plugin is the "feature pack"; the scripts are the "bug-fix packs". You need all three components. Scripts only need to run once (`apply`), though you may need to re-run after upgrading Hermes.

**Q: What should I do after upgrading Hermes?**

A: Major Hermes upgrades may overwrite the source fixes. It's recommended to run both `check` commands to verify status:
```bash
~/.hermes/scripts/hermes-patches.sh check
./scripts/hermes-mattermost-enhancer.sh check
```

**Q: Will the scripts mess up my Hermes?**

A: No. They make minimal changes. You can check status anytime with `check`. To revert, simply reinstall Hermes.

**Q: What if I skip the scripts?**

A: Several bugs remain unfixed (those marked "Shell Patch" above: #8, #10):
- Clarify session split: new messages during clarify waiting create separate sessions (AI forgets everything)
- After Gateway restart, auto-resumed sessions in the same channel can leak between Threads

The other issues (#7 progress in Thread, #9 concurrency guard) are fixed upstream — no script needed. All adapter-override fixes (#1-6) work normally without the scripts.

**Q: Why doesn't `apply` restart the Gateway automatically?**

A: By design. Hermes' safety hook refuses gateway restarts issued from inside the gateway process; the old script's embedded restart call made even `check` unusable from a gateway session. The new script only applies patches and prints a hint — restart manually from an external terminal.

---

## 📁 Project Structure

```
mattermost-enhancer/
├── plugin.yaml              # Plugin metadata
├── __init__.py              # Plugin entry point
├── adapter.py               # Core logic (50+ methods)
├── cards.py                 # Interactive card templates
├── models.py                # Model list resolver
├── callback_server.py       # Callback server
├── scripts/
│   └── hermes-mattermost-enhancer.sh   # Companion shell script (Mattermost Gateway patches)
├── references/
│   └── api-contracts.md     # Mattermost API contract docs
├── README.md                # This document
├── README.zh-CN.md          # Chinese documentation
└── LICENSE                  # MIT
```

---

> 💡 **Docker Self-Hosting Tips** — If you run Mattermost in Docker, these will save you some headaches:
>
> - **Messages not live-updating?** Set `AllowCorsFrom` to `http://127.0.0.1:8065` in `config.json` and restart the container. The browser WebSocket is being blocked by CORS.
> - **`/model` not responding?** `MATTERMOST_CALLBACK_URL` in `.env` must use `http://host.docker.internal:18065/mattermost/callback`. Inside a container, `127.0.0.1` points to the container itself, not the host.
> - **Images showing as broken?** Make `SiteURL` match the URL in your browser's address bar. Local = `127.0.0.1`, remote = your domain — don't mix them.
> - **Random disconnects?** Give the container at least 2GB of memory. Run `docker stats mm-app` to check current usage.

## 📄 License

MIT — see [LICENSE](LICENSE)
