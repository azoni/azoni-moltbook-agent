"""
FastAPI server for Azoni Moltbook Agent.

Provides endpoints for:
- Manual triggers
- Status checks
- Activity history
- Configuration management
"""
from datetime import datetime, timedelta
from typing import Optional, List
import json

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import run_agent, get_moltbook_client
from agent.tools import MoltbookClient
from config.settings import settings
from config.firebase import get_firestore, MOLTBOOK_CONFIG, MOLTBOOK_ACTIVITY, MOLTBOOK_STATE


app = FastAPI(
    title="Azoni Moltbook Agent",
    description="API for controlling the Azoni Moltbook agent",
    version="1.0.0"
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
