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
            return config_doc.to_dict().get("autonomous_mode", False)
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


def post_job():
    """Create new posts every 35 minutes."""
    logger.info(f"Post job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping")
        return
    
    if not can_post():
        logger.info("Post cooldown active, skipping")
        return
    
    try:
        result = run_agent(
            trigger="heartbeat",
            trigger_context="Create a new post about AI, coding, or your projects. Be authentic."
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
        
        for post_doc in our_posts:
            post_data = post_doc.to_dict()
            post_id = post_data.get("result", {}).get("post", {}).get("id")
            
            if not post_id:
                continue
            
            try:
                comments = client.get_comments(post_id)
                
                for comment in comments:
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
                    
                    logger.info(f"Replied to {author_name}")
                    return
                    
            except Exception as e:
                logger.error(f"Error on post {post_id}: {e}")
                continue
                
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
                return  # Only upvote one per run
                
            except Exception as e:
                logger.error(f"Failed to upvote {post_id}: {e}")
                continue
                
    except Exception as e:
        logger.error(f"Upvote job failed: {e}")


# ==================== App Lifecycle ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting scheduler...")
    scheduler.add_job(post_job, IntervalTrigger(minutes=35), id="post_job", replace_existing=True)
    scheduler.add_job(comment_job, IntervalTrigger(minutes=15), id="comment_job", replace_existing=True)
    scheduler.add_job(reply_job, IntervalTrigger(minutes=10), id="reply_job", replace_existing=True)
    scheduler.add_job(upvote_job, IntervalTrigger(minutes=20), id="upvote_job", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started with post(35m), comment(15m), reply(10m), upvote(20m) jobs")
    
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


# ==================== Endpoints ====================

@app.get("/")
async def root():
    """Health check."""
    return {
        "status": "ok",
        "agent": "Azoni",
        "service": "Moltbook Agent"
    }


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
    
    return {
        "registered": moltbook_registered,
        "moltbook_status": moltbook_status,
        "autonomous_mode": config_data.get("autonomous_mode", False),
        "last_run": state_data.get("last_run"),
        "last_run_at": state_data.get("last_run_at"),
        "last_activity": state_data.get("last_activity"),
        "posts_today": len(posts_today),
        "heartbeat_interval_hours": config_data.get("heartbeat_interval_hours", 4)
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
        "max_posts_per_day": config_data.get("max_posts_per_day", 6)
    }


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