# AI Agent Passive Income System
**Version 2.0 — Build Spec: May 2026**

A modular AI agent system that researches Etsy demand, generates original designs, publishes listings automatically, and reports profits weekly. Zero manual work after initial setup.

---

## 4-Day Build Timeline

Build in strict order. Each day has one clear goal. **Do not move to the next day until the current day's goal is confirmed working.**

---

### Day 1 — Prove the Pipeline Works
**Goal: one design on one live Etsy listing. Manual if needed.**
The only thing that matters today is confirming the Etsy + Printify integration actually works end to end.

- [ ] Create Etsy seller account and Printify account
- [ ] Connect Printify to Etsy store in Printify dashboard
- [ ] Get API keys: Etsy (OAuth), Printify (API key), OpenAI, Anthropic
- [ ] Write a single 20-line script that generates one image via GPT-4o and uploads it to Printify on a mug template
- [ ] Manually create one Etsy draft listing from that Printify product
- [ ] Confirm it appears correctly in Etsy seller dashboard

> **Day 1 success = one draft listing visible in Etsy.** Don't touch niche research or agent architecture until this is confirmed.

---

### Day 2 — Automate the Core Loop
**Goal: research agent and design agent running, publisher creating draft listings automatically.**

- [ ] Set up Supabase project, create all five tables (schema in Section 4 of spec)
- [ ] Build `agents/research_agent.py` — scrapes Etsy occupation gift bestsellers, stores brief in Supabase
- [ ] Build `agents/design_agent.py` — reads brief, generates 5 designs using prompt template, logs costs
- [ ] Build `agents/publisher_agent.py` — creates Printify product, writes listing copy, pushes to Etsy as DRAFT
- [ ] Run full pipeline manually end to end, confirm 5 draft listings appear in Etsy

---

### Day 3 — Scheduler, Monitoring, Deploy
**Goal: system running automatically on Railway without your PC.**

- [ ] Build `agents/reporting_agent.py` — Telegram bot sends weekly P&L summary
- [ ] Build `core/spend_monitor.py` — checks monthly API cost against $100 cap before any agent runs
- [ ] Wire all agents into APScheduler with daily/weekly schedule (`scheduler/main.py`)
- [ ] Push to GitHub, deploy to Railway, set all environment variables
- [ ] Confirm Railway runs the full daily cycle overnight without errors

---

### Day 4 — Review, Approve, Hands Off
**Goal: first real listings live, system running autonomously.**

- [ ] Review the draft listings generated on Day 2–3, approve the good ones, publish them
- [ ] Fix any errors from overnight Railway run
- [ ] Confirm Telegram report arrives correctly
- [ ] Walk away. Let it run.

---

## Project Structure

```
/agents
    research_agent.py       # Etsy scraper + Claude brief generator
    design_agent.py         # GPT-4o image generation (max 8/day)
    publisher_agent.py      # Printify + Etsy listing creation
    reporting_agent.py      # Weekly P&L via Telegram

/publishers
    etsy_printify.py        # Etsy + Printify specific logic (Phase 1)
    amazon.py               # Future expansion
    redbubble.py            # Future expansion

/core
    supabase_client.py      # Shared DB read/write helpers
    cost_logger.py          # Logs every API call cost
    spend_monitor.py        # Checks $100 cap before agents run
    error_handler.py        # Retry logic + Telegram alerts

/scheduler
    main.py                 # APScheduler entry point, all cron jobs

.env                        # API keys — NEVER commit to GitHub
.env.template               # Copy this to .env and fill in values
Procfile                    # Railway entry: worker: python scheduler/main.py
requirements.txt            # All Python dependencies
```

---

## Daily Schedule

| Time | Agent | Action |
|------|-------|--------|
| 6:00 AM | Research Agent | Scrapes Etsy bestsellers, stores brief in Supabase |
| 8:00 AM | Design Agent | Reads brief, generates up to 8 designs, logs costs |
| 10:00 AM | Publisher Agent | Creates Printify products, pushes to Etsy as draft/live |
| Sunday 9:00 AM | Reporting Agent | Calculates weekly P&L, sends Telegram summary |
| Before every run | Spend Monitor | Checks monthly spend vs $100 cap — halts if exceeded |

---

## Setup

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd agenticsystems

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Playwright browser
playwright install chromium

# 4. Configure environment
cp .env.template .env
# Edit .env and fill in all API keys

# 5. Run the scheduler (Railway entry point)
python scheduler/main.py
```

---

## Cost Estimate

| Item | Monthly Cost |
|------|-------------|
| Research Agent (Claude API) | $15–25/mo |
| Design Agent (GPT-4o images) | $12–25/mo |
| Publisher Agent (Claude API) | $8–15/mo |
| Reporting Agent (Claude API) | $2–5/mo |
| Railway hosting | Free tier |
| Supabase | Free tier |
| **TOTAL** | **~$37–70/mo (hard capped at $100)** |

**Break-even: 4–6 sales/month** at ~$11.87 net profit per mug sold.

---

## Rules

1. Build in order. Don't skip phases.
2. Day 1 goal only: one live listing. Proves the integration works before building on top of it.
3. One niche for the first 30 days.
4. Don't auto-publish for 2 weeks — flip `DRAFT_MODE=false` after reviewing output quality.
5. Set the spend cap before anything runs.
6. Give it 60 days before any judgment. Month 1 is ramp-up. Month 2 is when real data appears.
