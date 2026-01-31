"""
FastAPI server for Azoni Moltbook Agent.

Provides endpoints for:
- Manual triggers
- Status checks
- Activity history
- Configuration management
- Built-in background scheduler for autonomous mode
"""
from datetime import datetime, timedelta
from typing import Optional, List
import json
import logging

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agent import run_agent, get_moltbook_client
from agent.tools import MoltbookClient
from config.settings import settings
from config.firebase import get_firestore, MOLTBOOK_CONFIG, MOLTBOOK_ACTIVITY, MOLTBOOK_STATE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global scheduler
scheduler = AsyncIOScheduler()


# ==================== Scheduler Jobs ====================

def check_autonomous_mode() -> bool:
    """Check if autonomous mode is enabled."""
    try:
        db = get_firestore()
        config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
        if config_doc.exists:
            mode = config_doc.to_dict().get("autonomous_mode", False)
            logger.info(f"Autonomous mode check: {mode}")
            return mode
        logger.warning("No config document found - autonomous mode OFF")
        return False
    except Exception as e:
        logger.error(f"Error checking autonomous mode: {e}")
        return False


def can_post() -> bool:
    """Check if we can post (30 min cooldown)."""
    try:
        db = get_firestore()
        posts = list(db.collection(MOLTBOOK_ACTIVITY)
            .where("action", "==", "post")
            .order_by("timestamp", direction="DESCENDING")
            .limit(1)
            .get())
        
        if not posts:
            return True
        
        last_post_time = posts[0].to_dict().get("timestamp")
        if last_post_time:
            if hasattr(last_post_time, 'timestamp'):
                last_post_time = datetime.fromtimestamp(last_post_time.timestamp())
            elif isinstance(last_post_time, str):
                last_post_time = datetime.fromisoformat(last_post_time.replace('Z', '+00:00'))
            
            time_since = datetime.now() - last_post_time.replace(tzinfo=None)
            return time_since > timedelta(minutes=30)
        return True
    except Exception as e:
        logger.error(f"Error checking post cooldown: {e}")
        return False


def get_next_post_topic() -> str:
    """Get and consume the next topic from the queue, or return default."""
    try:
        db = get_firestore()
        config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
        config_data = config_doc.to_dict() if config_doc.exists else {}
        topics = config_data.get("post_topics", [])
        
        if topics:
            # Pop the first topic
            next_topic = topics.pop(0)
            # Update the queue
            db.collection(MOLTBOOK_CONFIG).document("settings").set({
                "post_topics": topics
            }, merge=True)
            return next_topic
        
        # Default topics if queue is empty
        import random
        default_topics = [
            "Share something interesting you learned while building AI applications",
            "Discuss a challenge you faced recently and how you solved it",
            "Share thoughts on the current AI agent ecosystem",
            "Talk about a useful tool or technique you've been using",
            "Reflect on building in public and shipping real products",
        ]
        return random.choice(default_topics)
    except Exception as e:
        logger.error(f"Error getting post topic: {e}")
        return "Share something interesting about AI, coding, or your projects"


def post_job():
    """Create new posts every 35 minutes."""
    logger.info(f"Post job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping")
        return
    
    if not can_post():
        logger.info("Post cooldown active, skipping")
        return
    
    # Get the next topic
    topic = get_next_post_topic()
    logger.info(f"Post topic: {topic}")
    
    try:
        result = run_agent(
            trigger="heartbeat",
            trigger_context=f"Create a new post about: {topic}. Be authentic and add value."
        )
        logger.info(f"Post job: {result.get('decision', {}).get('action')}, executed={result.get('executed')}")
    except Exception as e:
        logger.error(f"Post job failed: {e}")


def comment_job():
    """Comment on posts every 15 minutes."""
    logger.info(f"Comment job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        return
    
    try:
        result = run_agent(
            trigger="heartbeat",
            trigger_context="Find an interesting post to comment on. Add value to the discussion. Do NOT create a new post."
        )
        logger.info(f"Comment job: {result.get('decision', {}).get('action')}, executed={result.get('executed')}")
    except Exception as e:
        logger.error(f"Comment job failed: {e}")


def reply_job():
    """Reply to comments on our posts every 10 minutes."""
    logger.info(f"Reply job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        return
    
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage
        from agent.personality import AZONI_IDENTITY
        import time
        
        client = get_moltbook_client()
        db = get_firestore()
        
        llm = ChatOpenAI(
            model=settings.default_model.split("/")[-1],
            openai_api_key=settings.openrouter_api_key,
            openai_api_base="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer": "https://azoni.ai", "X-Title": "Azoni Moltbook Agent"}
        )
        
        # Get our recent posts
        our_posts = list(db.collection(MOLTBOOK_ACTIVITY)
            .where("action", "==", "post")
            .order_by("timestamp", direction="DESCENDING")
            .limit(5)
            .get())
        
        replies_made = 0
        max_replies_per_run = 5  # Limit to avoid rate limits
        
        for post_doc in our_posts:
            if replies_made >= max_replies_per_run:
                break
                
            post_data = post_doc.to_dict()
            post_id = post_data.get("result", {}).get("post", {}).get("id")
            
            if not post_id:
                continue
            
            try:
                comments = client.get_comments(post_id)
                
                for comment in comments:
                    if replies_made >= max_replies_per_run:
                        break
                        
                    comment_id = comment.get("id")
                    comment_author = comment.get("author")
                    comment_content = comment.get("content", "")
                    
                    if isinstance(comment_author, dict):
                        author_name = comment_author.get("name", "unknown")
                    else:
                        author_name = comment_author or "unknown"
                    
                    if author_name.lower() in ["azoni-ai", "azoni"]:
                        continue
                    
                    # Check if already replied
                    existing = list(db.collection(MOLTBOOK_ACTIVITY)
                        .where("action", "==", "comment")
                        .where("decision.target_comment_id", "==", comment_id)
                        .limit(1)
                        .get())
                    
                    if existing:
                        continue
                    
                    # Generate reply
                    prompt = f'''Someone commented on your post. Write a brief, friendly reply.
Their comment: "{comment_content}"
Author: {author_name}
Keep it short (1-3 sentences). Be genuine.'''

                    response = llm.invoke([
                        SystemMessage(content=AZONI_IDENTITY),
                        HumanMessage(content=prompt)
                    ])
                    reply_content = response.content.strip()
                    
                    result = client.create_comment(post_id=post_id, content=reply_content, parent_id=comment_id)
                    
                    db.collection(MOLTBOOK_ACTIVITY).add({
                        "action": "comment",
                        "timestamp": datetime.now(),
                        "date": datetime.now().date().isoformat(),
                        "draft": {"content": reply_content},
                        "decision": {"action": "comment", "reason": f"Reply to {author_name}", "target_post_id": post_id, "target_comment_id": comment_id},
                        "result": result,
                        "trigger": "reply_job"
                    })
                    
                    logger.info(f"Replied to {author_name} ({replies_made + 1}/{max_replies_per_run})")
                    replies_made += 1
                    
                    # Small delay between replies to avoid rate limits
                    time.sleep(2)
                    
            except Exception as e:
                logger.error(f"Error on post {post_id}: {e}")
                continue
        
        logger.info(f"Reply job complete. Made {replies_made} replies.")
                
    except Exception as e:
        logger.error(f"Reply job failed: {e}")


def upvote_job():
    """Upvote good content every 20 minutes."""
    logger.info(f"Upvote job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        return
    
    try:
        client = get_moltbook_client()
        db = get_firestore()
        
        # Get hot posts
        feed = client.get_feed(sort="hot", limit=10)
        upvoted = 0
        
        for post in feed:
            post_id = post.get("id")
            
            # Check if we already upvoted
            existing = list(db.collection(MOLTBOOK_ACTIVITY)
                .where("action", "==", "upvote")
                .where("decision.target_post_id", "==", post_id)
                .limit(1)
                .get())
            
            if existing:
                continue
            
            # Upvote it
            try:
                result = client.upvote_post(post_id)
                
                db.collection(MOLTBOOK_ACTIVITY).add({
                    "action": "upvote",
                    "timestamp": datetime.now(),
                    "date": datetime.now().date().isoformat(),
                    "decision": {
                        "action": "upvote",
                        "target_post_id": post_id,
                        "reason": f"Upvoted '{post.get('title', 'Unknown')[:50]}'"
                    },
                    "result": result,
                    "trigger": "upvote_job"
                })
                
                logger.info(f"Upvoted: {post.get('title', 'Unknown')[:50]}")
                upvoted += 1
                if upvoted >= 3:  # Upvote up to 3 per run
                    return
                import time
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Failed to upvote {post_id}: {e}")
                continue
                
    except Exception as e:
        logger.error(f"Upvote job failed: {e}")


# ==================== App Lifecycle ====================

def startup_check():
    """Run once on startup to verify everything works."""
    logger.info("=" * 50)
    logger.info("STARTUP HEALTH CHECK")
    logger.info("=" * 50)
    
    # Check Firestore
    try:
        db = get_firestore()
        config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
        if config_doc.exists:
            config = config_doc.to_dict()
            logger.info(f"  Firestore: OK")
            logger.info(f"  Autonomous mode: {config.get('autonomous_mode', False)}")
            logger.info(f"  Topics in queue: {len(config.get('post_topics', []))}")
        else:
            logger.warning(f"  Firestore: No config doc! Creating default...")
            db.collection(MOLTBOOK_CONFIG).document("settings").set({
                "autonomous_mode": False,
                "post_topics": []
            })
    except Exception as e:
        logger.error(f"  Firestore: FAILED - {e}")
    
    # Check Moltbook
    try:
        client = get_moltbook_client()
        status = client.get_status()
        logger.info(f"  Moltbook API: {status.get('status', 'unknown')}")
    except Exception as e:
        logger.error(f"  Moltbook API: FAILED - {e}")
    
    # Check OpenRouter / LLM
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
        llm = ChatOpenAI(
            model=settings.default_model.split("/")[-1],
            openai_api_key=settings.openrouter_api_key,
            openai_api_base="https://openrouter.ai/api/v1",
            default_headers={"HTTP-Referer": "https://azoni.ai", "X-Title": "Azoni Moltbook Agent"}
        )
        resp = llm.invoke([HumanMessage(content="Say 'ok' in one word.")])
        logger.info(f"  LLM (OpenRouter): OK - {resp.content[:20]}")
    except Exception as e:
        logger.error(f"  LLM (OpenRouter): FAILED - {e}")
    
    # List scheduled jobs
    logger.info(f"  Scheduler running: {scheduler.running}")
    for job in scheduler.get_jobs():
        logger.info(f"  Job: {job.id} -> next run: {job.next_run_time}")
    
    logger.info("=" * 50)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting scheduler...")
    # Karma-optimized intervals:
    # - Posts less frequently (quality over quantity)
    # - Comments more frequently (builds relationships)
    # - Replies quickly (shows engagement)
    # - Upvotes regularly (community participation)
    scheduler.add_job(post_job, IntervalTrigger(minutes=45), id="post_job", replace_existing=True)
    scheduler.add_job(comment_job, IntervalTrigger(minutes=12), id="comment_job", replace_existing=True)
    scheduler.add_job(reply_job, IntervalTrigger(minutes=8), id="reply_job", replace_existing=True)
    scheduler.add_job(upvote_job, IntervalTrigger(minutes=15), id="upvote_job", replace_existing=True)
    
    # Run health check 10 seconds after startup
    from apscheduler.triggers.date import DateTrigger
    scheduler.add_job(startup_check, DateTrigger(run_date=datetime.now() + timedelta(seconds=10)), id="startup_check")
    
    scheduler.start()
    logger.info("Scheduler started with post(45m), comment(12m), reply(8m), upvote(15m) jobs")
    
    yield
    
    # Shutdown
    logger.info("Shutting down scheduler...")
    scheduler.shutdown()


app = FastAPI(
    title="Azoni Moltbook Agent",
    description="API for controlling the Azoni Moltbook agent",
    version="1.0.0",
    lifespan=lifespan
)

# CORS for admin panel
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://azoni.ai", "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== Models ====================

class RegisterRequest(BaseModel):
    name: str = "Azoni"
    description: str = settings.agent_description


class ManualRunRequest(BaseModel):
    context: Optional[str] = None


class PostRequest(BaseModel):
    title: str
    content: str
    submolt: str = "general"


class CommentRequest(BaseModel):
    post_id: str
    content: str


class ConfigUpdate(BaseModel):
    autonomous_mode: Optional[bool] = None
    heartbeat_interval_hours: Optional[int] = None
    max_posts_per_day: Optional[int] = None
    post_topics: Optional[List[str]] = None


# ==================== Endpoints ====================

@app.get("/", response_class=HTMLResponse)
async def root():
    """Beautiful status dashboard."""
    try:
        import pytz
        seattle_tz = pytz.timezone('America/Los_Angeles')
        now_seattle = datetime.now(seattle_tz)
        
        db = get_firestore()
        
        # Get config (with fallback)
        try:
            config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
            config_data = config_doc.to_dict() if config_doc.exists else {}
        except:
            config_data = {}
        
        # Check Moltbook connection
        moltbook_status = "Not connected"
        moltbook_class = "status-offline"
        moltbook_username = ""
        if settings.moltbook_api_key:
            try:
                client = get_moltbook_client()
                status_response = client.get_status()
                if status_response.get("status") == "claimed":
                    moltbook_status = "Connected"
                    moltbook_class = "status-online"
                    moltbook_username = status_response.get("agent", {}).get("name", "Azoni-AI")
                else:
                    moltbook_status = status_response.get("status", "Unknown")
            except Exception as e:
                moltbook_status = f"Error"
        
        # Get recent activity (with fallback)
        activities = []
        try:
            activity_docs = db.collection(MOLTBOOK_ACTIVITY)\
                .order_by("timestamp", direction="DESCENDING")\
                .limit(10).get()
            
            for doc in activity_docs:
                data = doc.to_dict()
                ts = data.get("timestamp")
                try:
                    # Convert to Seattle time
                    if ts:
                        if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                            ts = pytz.utc.localize(ts)
                        ts_seattle = ts.astimezone(seattle_tz)
                        time_str = ts_seattle.strftime("%I:%M %p")
                        date_str = ts_seattle.strftime("%b %d")
                    else:
                        time_str = "Unknown"
                        date_str = ""
                except:
                    time_str = "Unknown"
                    date_str = ""
                
                # Get link safely
                post_id = None
                result = data.get("result") or {}
                if isinstance(result, dict):
                    if result.get("post", {}).get("id"):
                        post_id = result["post"]["id"]
                    elif result.get("comment", {}).get("post_id"):
                        post_id = result["comment"]["post_id"]
                    elif result.get("id"):
                        post_id = result["id"]
                
                link = f"https://www.moltbook.com/post/{post_id}" if post_id else None
                
                # Get title safely
                draft = data.get("draft") or {}
                title = ""
                if isinstance(draft, dict):
                    title = (draft.get("title") or draft.get("content") or "")[:50]
                
                # Get trigger info
                trigger = data.get("trigger", "manual")
                
                activities.append({
                    "action": data.get("action", "unknown"),
                    "time": time_str,
                    "date": date_str,
                    "title": title,
                    "error": data.get("error"),
                    "link": link,
                    "trigger": trigger
                })
        except Exception as e:
            logger.error(f"Error fetching activity: {e}")
        
        # Count today's activity (with fallback)
        posts_today = 0
        comments_today = 0
        upvotes_today = 0
        try:
            today = datetime.now().date().isoformat()
            posts_today = len(list(db.collection(MOLTBOOK_ACTIVITY)
                .where("action", "==", "post")
                .where("date", "==", today)
                .limit(50).get()))
            comments_today = len(list(db.collection(MOLTBOOK_ACTIVITY)
                .where("action", "==", "comment")
                .where("date", "==", today)
                .limit(50).get()))
            upvotes_today = len(list(db.collection(MOLTBOOK_ACTIVITY)
                .where("action", "==", "upvote")
                .where("date", "==", today)
                .limit(50).get()))
        except:
            pass
        
        # Scheduler info with more details
        scheduler_status = "Running" if scheduler.running else "Stopped"
        scheduler_class = "status-online" if scheduler.running else "status-offline"
        
        job_details = {
            "post": {"interval": "45 min", "desc": "Creates engaging posts on interesting topics", "icon": "📝"},
            "comment": {"interval": "12 min", "desc": "Comments on posts to build relationships", "icon": "💬"},
            "reply": {"interval": "8 min", "desc": "Replies to comments on your posts quickly", "icon": "↩️"},
            "upvote": {"interval": "15 min", "desc": "Upvotes quality content from the community", "icon": "👍"},
        }
        
        next_jobs = []
        if scheduler.running:
            for job in scheduler.get_jobs():
                if job.next_run_time:
                    # Convert to Seattle time
                    next_seattle = job.next_run_time.astimezone(seattle_tz)
                    job_name = job.id.replace("_job", "")
                    details = job_details.get(job_name, {"interval": "?", "desc": "Scheduled task", "icon": "⚡"})
                    
                    # Calculate time until
                    time_until = job.next_run_time - datetime.now(pytz.utc)
                    mins_until = int(time_until.total_seconds() / 60)
                    
                    next_jobs.append({
                        "id": job_name.title(),
                        "next": next_seattle.strftime("%I:%M %p"),
                        "until": f"{mins_until}m" if mins_until < 60 else f"{mins_until//60}h {mins_until%60}m",
                        "interval": details["interval"],
                        "desc": details["desc"],
                        "icon": details["icon"]
                    })
        
        # Autonomous mode
        auto_mode = config_data.get("autonomous_mode", False)
        auto_class = "status-online" if auto_mode else "status-offline"
        auto_text = "Enabled" if auto_mode else "Disabled"
        
        # Topics queue
        topics = config_data.get("post_topics", [])
        
        # Current time in Seattle
        current_time = now_seattle.strftime("%I:%M %p PST")
        current_date = now_seattle.strftime("%A, %B %d")
        
        # Build activity HTML
        activity_html = ""
        if activities:
            for a in activities:
                action_class = "error" if a.get("error") else a["action"]
                action_text = "ERROR" if a.get("error") else a["action"].upper()
                title_text = a.get("title") or (a.get("error", "")[:40] if a.get("error") else "Activity")
                link_html = f'<a href="{a["link"]}" target="_blank" class="activity-link">View ↗</a>' if a.get("link") else ""
                trigger_badge = f'<span class="trigger-badge">{a.get("trigger", "manual")}</span>'
                
                activity_html += f'''
                <div class="activity-item">
                    <span class="activity-action {action_class}">{action_text}</span>
                    <div class="activity-details">
                        <div class="activity-title">{title_text}</div>
                        <div class="activity-meta">
                            <span class="activity-time">{a["date"]} at {a["time"]}</span>
                            {trigger_badge}
                        </div>
                    </div>
                    {link_html}
                </div>
                '''
        else:
            activity_html = '<div class="empty">No activity yet. Enable autonomous mode or trigger manually.</div>'
        
        # Build schedule HTML with expanded details
        schedule_html = ""
        if not scheduler.running:
            schedule_html = '<div class="empty">Scheduler not running - server may have just started</div>'
        elif next_jobs:
            for j in next_jobs:
                schedule_html += f'''
                <div class="job-card">
                    <div class="job-header">
                        <span class="job-icon">{j["icon"]}</span>
                        <span class="job-name">{j["id"]}</span>
                        <span class="job-interval">every {j["interval"]}</span>
                    </div>
                    <div class="job-desc">{j["desc"]}</div>
                    <div class="job-next">
                        <span>Next run:</span>
                        <span class="job-time">{j["next"]}</span>
                        <span class="job-until">({j["until"]})</span>
                    </div>
                </div>
                '''
        else:
            schedule_html = '<div class="empty">No jobs scheduled</div>'
        
        # Build topics HTML
        topics_html = ""
        if topics:
            for i, t in enumerate(topics[:5]):
                topic_text = t[:70] + ("..." if len(t) > 70 else "")
                topics_html += f'<div class="topic-item"><span class="topic-num">{i+1}</span>{topic_text}</div>'
        else:
            topics_html = '<div class="empty">No topics queued - agent will pick interesting topics from feed</div>'
        
        # SVG Lobster favicon
        lobster_svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
            <defs><linearGradient id="lg" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#ff6b6b"/><stop offset="100%" style="stop-color:#ee5a5a"/>
            </linearGradient></defs>
            <ellipse cx="32" cy="38" rx="14" ry="18" fill="url(#lg)"/>
            <ellipse cx="32" cy="22" rx="10" ry="8" fill="url(#lg)"/>
            <circle cx="28" cy="20" r="2" fill="#1a1a2e"/><circle cx="36" cy="20" r="2" fill="#1a1a2e"/>
            <path d="M22 22 Q14 14 8 18" stroke="#ff6b6b" stroke-width="3" fill="none" stroke-linecap="round"/>
            <path d="M42 22 Q50 14 56 18" stroke="#ff6b6b" stroke-width="3" fill="none" stroke-linecap="round"/>
            <path d="M18 36 Q8 32 4 38 Q8 36 12 40" stroke="#ff6b6b" stroke-width="4" fill="none" stroke-linecap="round"/>
            <path d="M46 36 Q56 32 60 38 Q56 36 52 40" stroke="#ff6b6b" stroke-width="4" fill="none" stroke-linecap="round"/>
            <ellipse cx="6" cy="40" rx="4" ry="6" fill="url(#lg)"/>
            <ellipse cx="58" cy="40" rx="4" ry="6" fill="url(#lg)"/>
            <path d="M26 56 Q28 62 32 58 Q36 62 38 56" stroke="#ff6b6b" stroke-width="2" fill="none"/>
        </svg>'''
        
        # Inline logo version (constrained size)
        lobster_logo = lobster_svg.replace('width="64" height="64"', 'width="48" height="48" class="header-logo"')
        
        favicon_base64 = "data:image/svg+xml;base64," + __import__('base64').b64encode(lobster_svg.encode()).decode()
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Azoni-AI | Moltbook Agent</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="icon" type="image/svg+xml" href="{favicon_base64}">
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                    min-height: 100vh;
                    color: #e0e0e0;
                    padding: 2rem;
                }}
                .container {{ max-width: 1000px; margin: 0 auto; }}
                .header {{
                    text-align: center;
                    margin-bottom: 2rem;
                    padding: 2rem;
                    background: rgba(255,255,255,0.05);
                    border-radius: 16px;
                    border: 1px solid rgba(255,255,255,0.1);
                }}
                .header-logo {{
                    width: 64px;
                    height: 64px;
                    margin-bottom: 1rem;
                }}
                .header h1 {{
                    font-size: 2.5rem;
                    background: linear-gradient(90deg, #ff6b6b, #ffa500);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    background-clip: text;
                    margin-bottom: 0.25rem;
                }}
                .header .subtitle {{ color: #888; margin-bottom: 0.5rem; }}
                .header .time {{ color: #4ade80; font-size: 0.9rem; }}
                .header .date {{ color: #666; font-size: 0.85rem; }}
                .header .username {{ color: #60a5fa; font-size: 0.9rem; margin-top: 0.5rem; }}
                .grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                    gap: 1rem;
                    margin-bottom: 2rem;
                }}
                .card {{
                    background: rgba(255,255,255,0.05);
                    border-radius: 12px;
                    padding: 1.25rem;
                    border: 1px solid rgba(255,255,255,0.1);
                    text-align: center;
                }}
                .card h3 {{
                    font-size: 0.75rem;
                    color: #888;
                    text-transform: uppercase;
                    letter-spacing: 1px;
                    margin-bottom: 0.5rem;
                }}
                .card .value {{
                    font-size: 1.5rem;
                    font-weight: 600;
                }}
                .status-online {{ color: #4ade80; }}
                .status-offline {{ color: #f87171; }}
                .section {{
                    background: rgba(255,255,255,0.05);
                    border-radius: 12px;
                    padding: 1.5rem;
                    border: 1px solid rgba(255,255,255,0.1);
                    margin-bottom: 1.5rem;
                }}
                .section h2 {{
                    margin-bottom: 1rem;
                    font-size: 1.1rem;
                    display: flex;
                    align-items: center;
                    gap: 0.5rem;
                }}
                .activity-item {{
                    display: flex;
                    align-items: center;
                    padding: 0.75rem 0;
                    border-bottom: 1px solid rgba(255,255,255,0.05);
                }}
                .activity-item:last-child {{ border-bottom: none; }}
                .activity-action {{
                    background: rgba(255,107,107,0.2);
                    color: #ff6b6b;
                    padding: 0.25rem 0.75rem;
                    border-radius: 20px;
                    font-size: 0.75rem;
                    font-weight: 600;
                    min-width: 75px;
                    text-align: center;
                }}
                .activity-action.comment {{ background: rgba(74,222,128,0.2); color: #4ade80; }}
                .activity-action.upvote {{ background: rgba(96,165,250,0.2); color: #60a5fa; }}
                .activity-action.error {{ background: rgba(248,113,113,0.3); color: #f87171; }}
                .activity-details {{ flex: 1; margin-left: 1rem; }}
                .activity-title {{ font-weight: 500; font-size: 0.95rem; }}
                .activity-meta {{ display: flex; align-items: center; gap: 0.75rem; margin-top: 0.25rem; }}
                .activity-time {{ color: #666; font-size: 0.8rem; }}
                .trigger-badge {{
                    background: rgba(255,255,255,0.1);
                    padding: 0.1rem 0.5rem;
                    border-radius: 10px;
                    font-size: 0.7rem;
                    color: #888;
                }}
                .activity-link {{
                    color: #60a5fa;
                    text-decoration: none;
                    font-size: 0.85rem;
                    padding: 0.25rem 0.75rem;
                    border: 1px solid rgba(96,165,250,0.3);
                    border-radius: 6px;
                    transition: all 0.2s;
                }}
                .activity-link:hover {{ background: rgba(96,165,250,0.1); }}
                .job-card {{
                    background: rgba(255,255,255,0.03);
                    border-radius: 10px;
                    padding: 1rem;
                    margin-bottom: 0.75rem;
                    border: 1px solid rgba(255,255,255,0.05);
                }}
                .job-card:last-child {{ margin-bottom: 0; }}
                .job-header {{
                    display: flex;
                    align-items: center;
                    gap: 0.5rem;
                    margin-bottom: 0.5rem;
                }}
                .job-icon {{ font-size: 1.25rem; }}
                .job-name {{ font-weight: 600; font-size: 1rem; }}
                .job-interval {{
                    margin-left: auto;
                    background: rgba(74,222,128,0.15);
                    color: #4ade80;
                    padding: 0.2rem 0.5rem;
                    border-radius: 6px;
                    font-size: 0.75rem;
                }}
                .job-desc {{ color: #888; font-size: 0.85rem; margin-bottom: 0.5rem; }}
                .job-next {{
                    display: flex;
                    align-items: center;
                    gap: 0.5rem;
                    font-size: 0.85rem;
                }}
                .job-next > span:first-child {{ color: #666; }}
                .job-time {{ color: #ffa500; font-weight: 600; }}
                .job-until {{ color: #888; }}
                .topics {{ margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid rgba(255,255,255,0.1); }}
                .topics h3 {{ font-size: 0.9rem; color: #888; margin-bottom: 0.75rem; display: flex; align-items: center; gap: 0.5rem; }}
                .topic-item {{
                    background: rgba(255,255,255,0.03);
                    padding: 0.6rem 0.75rem;
                    border-radius: 8px;
                    margin-bottom: 0.5rem;
                    font-size: 0.9rem;
                    display: flex;
                    align-items: flex-start;
                    gap: 0.75rem;
                }}
                .topic-num {{
                    background: rgba(255,107,107,0.2);
                    color: #ff6b6b;
                    width: 22px;
                    height: 22px;
                    border-radius: 50%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    font-size: 0.75rem;
                    font-weight: 600;
                    flex-shrink: 0;
                }}
                .empty {{ color: #666; font-style: italic; padding: 0.5rem 0; }}
                .footer {{
                    text-align: center;
                    margin-top: 2rem;
                    color: #666;
                    font-size: 0.85rem;
                }}
                .footer a {{ color: #60a5fa; text-decoration: none; }}
                .footer a:hover {{ text-decoration: underline; }}
                .two-col {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 1.5rem;
                }}
                @media (max-width: 768px) {{
                    .two-col {{ grid-template-columns: 1fr; }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    {lobster_logo}
                    <h1>Azoni-AI</h1>
                    <p class="subtitle">Autonomous Moltbook Agent</p>
                    <p class="time">{current_time}</p>
                    <p class="date">{current_date}</p>
                    {f'<p class="username">@{moltbook_username}</p>' if moltbook_username else ''}
                </div>
                
                <div class="grid">
                    <div class="card">
                        <h3>Moltbook</h3>
                        <div class="value {moltbook_class}">{moltbook_status}</div>
                    </div>
                    <div class="card">
                        <h3>Scheduler</h3>
                        <div class="value {scheduler_class}">{scheduler_status}</div>
                    </div>
                    <div class="card">
                        <h3>Autonomous</h3>
                        <div class="value {auto_class}">{auto_text}</div>
                    </div>
                    <div class="card">
                        <h3>Posts</h3>
                        <div class="value">{posts_today}</div>
                    </div>
                    <div class="card">
                        <h3>Comments</h3>
                        <div class="value">{comments_today}</div>
                    </div>
                    <div class="card">
                        <h3>Upvotes</h3>
                        <div class="value">{upvotes_today}</div>
                    </div>
                </div>
                
                <div class="two-col">
                    <div class="section">
                        <h2>⏰ Scheduled Jobs</h2>
                        {schedule_html}
                    </div>
                    
                    <div class="section">
                        <h2>📝 Post Topics Queue</h2>
                        {topics_html if topics else '<div class="empty">No topics queued - agent will pick interesting topics from feed</div>'}
                        <div style="margin-top: 1rem; font-size: 0.8rem; color: #666;">
                            {len(topics)} topic{"s" if len(topics) != 1 else ""} in queue
                        </div>
                    </div>
                </div>
                
                <div class="section">
                    <h2>📋 Recent Activity</h2>
                    {activity_html}
                </div>
                
                <div class="footer">
                    Auto-refreshes every 60 seconds • <a href="/status">JSON API</a> • <a href="https://www.moltbook.com/u/Azoni-AI" target="_blank">View Profile ↗</a>
                </div>
            </div>
            <script>
                setTimeout(function() {{ location.reload(); }}, 60000);
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html)
        
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        import traceback
        traceback.print_exc()
        # Return a simple error page
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html>
        <head><title>Azoni-AI | Error</title></head>
        <body style="font-family: sans-serif; padding: 2rem; background: #1a1a2e; color: #e0e0e0;">
            <h1>🦞 Azoni-AI</h1>
            <p>Dashboard temporarily unavailable.</p>
            <p style="color: #f87171;">Error: {str(e)}</p>
            <p><a href="/status" style="color: #60a5fa;">Try JSON API</a></p>
        </body>
        </html>
        """, status_code=200)
@app.get("/status")
async def get_status():
    """Get current agent status."""
    db = get_firestore()
    
    # Get state
    state_doc = db.collection(MOLTBOOK_STATE).document("agent").get()
    state_data = state_doc.to_dict() if state_doc.exists else {}
    
    # Get config
    config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
    config_data = config_doc.to_dict() if config_doc.exists else {}
    
    # Check if registered with Moltbook
    moltbook_registered = bool(settings.moltbook_api_key)
    moltbook_status = None
    
    if moltbook_registered:
        try:
            client = get_moltbook_client()
            status_response = client.get_status()
            moltbook_status = status_response.get("status")
        except Exception as e:
            moltbook_status = f"error: {str(e)}"
    
    # Count recent activity
    today = datetime.now().date().isoformat()
    posts_today = list(db.collection(MOLTBOOK_ACTIVITY)
        .where("action", "==", "post")
        .where("date", "==", today)
        .limit(10).get())
    
    # Get scheduler info
    scheduler_jobs = []
    if scheduler.running:
        for job in scheduler.get_jobs():
            next_run = job.next_run_time.isoformat() if job.next_run_time else None
            scheduler_jobs.append({
                "id": job.id,
                "next_run": next_run
            })
    
    return {
        "registered": moltbook_registered,
        "moltbook_status": moltbook_status,
        "autonomous_mode": config_data.get("autonomous_mode", False),
        "last_run": state_data.get("last_run"),
        "last_run_at": state_data.get("last_run_at"),
        "last_activity": state_data.get("last_activity"),
        "posts_today": len(posts_today),
        "heartbeat_interval_hours": config_data.get("heartbeat_interval_hours", 4),
        "scheduler_running": scheduler.running,
        "scheduler_jobs": scheduler_jobs
    }


@app.post("/register")
async def register_agent(request: RegisterRequest):
    """
    Register Azoni on Moltbook.
    
    Returns claim URL that needs to be verified via tweet.
    """
    client = MoltbookClient(api_key=None)  # No key yet
    
    try:
        result = client.register(
            name=request.name,
            description=request.description
        )
        
        # Store the API key in Firestore (you should move this to env var after)
        db = get_firestore()
        db.collection(MOLTBOOK_CONFIG).document("credentials").set({
            "api_key": result.get("agent", {}).get("api_key"),
            "claim_url": result.get("agent", {}).get("claim_url"),
            "verification_code": result.get("agent", {}).get("verification_code"),
            "registered_at": datetime.now(),
            "claimed": False
        })
        
        return {
            "success": True,
            "claim_url": result.get("agent", {}).get("claim_url"),
            "verification_code": result.get("agent", {}).get("verification_code"),
            "important": "Tweet to verify, then add MOLTBOOK_API_KEY to your environment variables!"
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/run")
async def manual_run(request: ManualRunRequest, background_tasks: BackgroundTasks):
    """
    Manually trigger an agent run.
    
    This runs in the background and returns immediately.
    """
    def run_in_background(context: str):
        try:
            result = run_agent(trigger="manual", trigger_context=context)
            print(f"Manual run completed: {result.get('decision')}")
        except Exception as e:
            print(f"Manual run error: {e}")
    
    background_tasks.add_task(run_in_background, request.context)
    
    return {
        "status": "started",
        "message": "Agent run started in background",
        "context": request.context
    }


@app.post("/run/sync")
async def manual_run_sync(request: ManualRunRequest):
    """
    Manually trigger an agent run (synchronous - waits for completion).
    """
    try:
        result = run_agent(trigger="manual", trigger_context=request.context)
        
        return {
            "status": "completed",
            "decision": result.get("decision"),
            "executed": result.get("executed"),
            "execution_result": result.get("execution_result"),
            "error": result.get("error"),
            "llm_calls": result.get("llm_calls")
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/post")
async def direct_post(request: PostRequest):
    """
    Directly post to Moltbook (bypasses agent decision-making).
    """
    client = get_moltbook_client()
    
    try:
        result = client.create_post(
            title=request.title,
            content=request.content,
            submolt=request.submolt
        )
        
        # Log it
        db = get_firestore()
        db.collection(MOLTBOOK_ACTIVITY).add({
            "action": "post",
            "timestamp": datetime.now(),
            "date": datetime.now().date().isoformat(),
            "draft": {"title": request.title, "content": request.content, "submolt": request.submolt},
            "decision_reason": "Direct post via API",
            "result": result,
            "trigger": "manual"
        })
        
        return {"success": True, "result": result}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/comment")
async def direct_comment(request: CommentRequest):
    """
    Directly comment on a post (bypasses agent decision-making).
    """
    client = get_moltbook_client()
    
    try:
        result = client.create_comment(
            post_id=request.post_id,
            content=request.content
        )
        
        # Log it
        db = get_firestore()
        db.collection(MOLTBOOK_ACTIVITY).add({
            "action": "comment",
            "timestamp": datetime.now(),
            "date": datetime.now().date().isoformat(),
            "draft": {"content": request.content, "post_id": request.post_id},
            "decision_reason": "Direct comment via API",
            "result": result,
            "trigger": "manual"
        })
        
        return {"success": True, "result": result}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/feed")
async def get_feed(sort: str = "hot", limit: int = 20):
    """Get current Moltbook feed."""
    client = get_moltbook_client()
    
    try:
        feed = client.get_feed(sort=sort, limit=limit)
        return {"posts": feed}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/activity")
async def get_activity(limit: int = 50):
    """Get recent agent activity."""
    db = get_firestore()
    
    activity_docs = db.collection(MOLTBOOK_ACTIVITY)\
        .order_by("timestamp", direction="DESCENDING")\
        .limit(limit)\
        .get()
    
    activity = []
    for doc in activity_docs:
        data = doc.to_dict()
        # Convert timestamp to string for JSON
        if data.get("timestamp"):
            data["timestamp"] = data["timestamp"].isoformat() if hasattr(data["timestamp"], "isoformat") else str(data["timestamp"])
        activity.append({"id": doc.id, **data})
    
    return {"activity": activity}


@app.patch("/config")
async def update_config(request: ConfigUpdate):
    """Update agent configuration."""
    db = get_firestore()
    
    update_data = {}
    if request.autonomous_mode is not None:
        update_data["autonomous_mode"] = request.autonomous_mode
    if request.heartbeat_interval_hours is not None:
        update_data["heartbeat_interval_hours"] = request.heartbeat_interval_hours
    if request.max_posts_per_day is not None:
        update_data["max_posts_per_day"] = request.max_posts_per_day
    if request.post_topics is not None:
        update_data["post_topics"] = request.post_topics
    
    if update_data:
        update_data["updated_at"] = datetime.now()
        db.collection(MOLTBOOK_CONFIG).document("settings").set(update_data, merge=True)
    
    return {"success": True, "updated": update_data}


@app.get("/config")
async def get_config():
    """Get current configuration."""
    db = get_firestore()
    
    config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
    config_data = config_doc.to_dict() if config_doc.exists else {}
    
    return {
        "autonomous_mode": config_data.get("autonomous_mode", False),
        "heartbeat_interval_hours": config_data.get("heartbeat_interval_hours", 4),
        "max_posts_per_day": config_data.get("max_posts_per_day", 6),
        "post_topics": config_data.get("post_topics", [])
    }


# ==================== Post Topics Queue ====================

@app.get("/topics")
async def get_topics():
    """Get the post topics queue."""
    db = get_firestore()
    config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
    config_data = config_doc.to_dict() if config_doc.exists else {}
    return {"topics": config_data.get("post_topics", [])}


@app.post("/topics")
async def add_topic(topic: str):
    """Add a topic to the queue."""
    db = get_firestore()
    from google.cloud.firestore import ArrayUnion
    db.collection(MOLTBOOK_CONFIG).document("settings").set({
        "post_topics": ArrayUnion([topic])
    }, merge=True)
    return {"success": True, "added": topic}


@app.delete("/topics/{index}")
async def remove_topic(index: int):
    """Remove a topic by index (0-based)."""
    db = get_firestore()
    config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
    config_data = config_doc.to_dict() if config_doc.exists else {}
    topics = config_data.get("post_topics", [])
    
    if 0 <= index < len(topics):
        removed = topics.pop(index)
        db.collection(MOLTBOOK_CONFIG).document("settings").set({
            "post_topics": topics
        }, merge=True)
        return {"success": True, "removed": removed}
    else:
        raise HTTPException(status_code=404, detail="Topic index not found")


@app.get("/profile")
async def get_profile():
    """Get Azoni's Moltbook profile."""
    client = get_moltbook_client()
    
    try:
        profile = client.get_me()
        return profile
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Run Server ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)