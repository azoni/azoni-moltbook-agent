# Azoni Moltbook Agent

A LangGraph-powered AI agent that represents Charlton Smith on [Moltbook](https://moltbook.com), the social network for AI agents.

## What is This?

Azoni is an autonomous AI agent that:
- Observes the Moltbook feed
- Decides whether to post, comment, upvote, or do nothing
- Drafts content based on Charlton's background and interests
- Evaluates quality before posting
- Executes actions on Moltbook
- Logs all activity for monitoring

Built with **LangGraph** for proper agentic workflows with state management and conditional logic.

## Architecture

```
┌─────────────┐
│  HEARTBEAT  │ (every 4 hours) or manual trigger
└──────┬──────┘
       ▼
┌─────────────┐
│   OBSERVE   │ ── Fetch Moltbook feed
└──────┬──────┘
       ▼
┌─────────────┐
│   DECIDE    │ ── LLM decides: post? comment? upvote? nothing?
└──────┬──────┘
       ▼
┌─────────────┐
│   DRAFT     │ ── Generate content (if posting/commenting)
└──────┬──────┘
       ▼
┌─────────────┐
│  EVALUATE   │ ── Quality check before posting
└──────┬──────┘
       ▼
┌─────────────┐
│   EXECUTE   │ ── Call Moltbook API
└──────┬──────┘
       ▼
┌─────────────┐
│    LOG      │ ── Save to Firestore
└─────────────┘
```

## Setup

### 1. Clone and Install

```bash
git clone <repo>
cd azoni-moltbook-agent
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

Required variables:
- `OPENROUTER_API_KEY` - For LLM access
- `FIREBASE_PROJECT_ID` - Your Firebase project
- `FIREBASE_CLIENT_EMAIL` - Service account email  
- `FIREBASE_PRIVATE_KEY` - Service account key

### 3. Register on Moltbook

Start the API server:

```bash
uvicorn api.server:app --reload
```

Then register Azoni:

```bash
curl -X POST http://localhost:8000/register \
  -H "Content-Type: application/json" \
  -d '{"name": "Azoni", "description": "AI assistant for Charlton Smith"}'
```

This returns a `claim_url`. Tweet the verification code to claim your agent.

### 4. Add Moltbook API Key

After claiming, add the API key to your `.env`:

```
MOLTBOOK_API_KEY=moltbook_xxx
```

## Usage

### API Server

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

Endpoints:
- `GET /status` - Agent status
- `POST /run` - Trigger manual run (async)
- `POST /run/sync` - Trigger manual run (sync)
- `POST /post` - Direct post
- `POST /comment` - Direct comment
- `GET /feed` - View Moltbook feed
- `GET /activity` - View agent activity log
- `GET /config` - View configuration
- `PATCH /config` - Update configuration

### Heartbeat Scheduler

For autonomous operation:

```bash
python -m heartbeat.scheduler
```

This checks Moltbook every 4 hours (configurable) and engages if appropriate.

### Toggle Autonomous Mode

```bash
# Enable
curl -X PATCH http://localhost:8000/config \
  -H "Content-Type: application/json" \
  -d '{"autonomous_mode": true}'

# Disable
curl -X PATCH http://localhost:8000/config \
  -H "Content-Type: application/json" \
  -d '{"autonomous_mode": false}'
```

## Deployment

### Render

1. Create a new Web Service
2. Connect your repo
3. Set environment variables
4. Deploy

For heartbeat, create a Background Worker with:
```
python -m heartbeat.scheduler
```

### Docker

```bash
docker build -t azoni-moltbook .

# Run API
docker run -p 8000:8000 --env-file .env azoni-moltbook

# Run heartbeat
docker run --env-file .env azoni-moltbook python -m heartbeat.scheduler
```

## Monitoring

All activity is logged to Firestore:
- `moltbook_activity` - Posts, comments, upvotes
- `moltbook_state` - Last run, errors
- `moltbook_config` - Settings

View in Firebase Console or via `/activity` endpoint.

## Safety Features

- **Autonomous mode off by default** - Must explicitly enable
- **Quality evaluation** - LLM checks content before posting
- **Rate limits** - Respects Moltbook's 30-min post cooldown
- **Activity logging** - Full audit trail
- **Max posts per day** - Configurable limit

## Customization

Edit `agent/personality.py` to adjust:
- Azoni's identity and background
- Communication style
- Topics of interest
- Decision-making prompts

## License

MIT
