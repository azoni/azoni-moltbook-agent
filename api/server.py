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
from typing import Optional, List, Dict
import json
import logging

from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agent import run_agent, get_moltbook_client
from agent.tools import MoltbookClient
from config.settings import settings
from config.firebase import get_firestore, MOLTBOOK_CONFIG, MOLTBOOK_ACTIVITY, MOLTBOOK_STATE, MOLTBOOK_JOB_HISTORY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global scheduler
scheduler = AsyncIOScheduler()


# Default intervals (minutes)
DEFAULT_INTERVALS = {"post": 45, "comment": 10, "reply": 8, "upvote": 15, "watcher": 5}


def require_admin(x_admin_key: Optional[str] = Header(None)):
    """Dependency that checks for admin API key on protected endpoints."""
    if not settings.admin_api_key:
        # No key configured = no protection (dev mode)
        return True
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key header")
    return True

# Job name -> function mapping
JOB_FUNCTIONS = {
    "post": ("post_job", None),
    "comment": ("comment_job", None),
    "reply": ("reply_job", None),
    "upvote": ("upvote_job", None),
    "watcher": ("new_post_watcher", None),
}


def get_intervals() -> dict:
    """Get job intervals from Firestore config, with defaults."""
    try:
        db = get_firestore()
        config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
        if config_doc.exists:
            stored = config_doc.to_dict().get("intervals", {})
            merged = {**DEFAULT_INTERVALS, **stored}
            return merged
    except:
        pass
    return DEFAULT_INTERVALS.copy()


def reschedule_jobs():
    """Reschedule all jobs based on current Firestore intervals."""
    intervals = get_intervals()
    
    job_map = {
        "post": post_job,
        "comment": comment_job,
        "reply": reply_job,
        "upvote": upvote_job,
        "watcher": new_post_watcher,
    }
    
    job_id_map = {
        "post": "post_job",
        "comment": "comment_job",
        "reply": "reply_job",
        "upvote": "upvote_job",
        "watcher": "new_post_watcher",
    }
    
    for name, minutes in intervals.items():
        job_id = job_id_map.get(name)
        func = job_map.get(name)
        if job_id and func and minutes > 0:
            scheduler.add_job(func, IntervalTrigger(minutes=minutes), id=job_id, replace_existing=True)
            logger.info(f"Scheduled {job_id} every {minutes}m")
        elif minutes == 0 and job_id:
            # Disable job
            try:
                scheduler.remove_job(job_id)
                logger.info(f"Disabled {job_id}")
            except:
                pass


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


def log_job(job_name: str, status: str, details: dict):
    """Log job execution to Firestore job history. Keeps only last 50."""
    try:
        db = get_firestore()
        db.collection(MOLTBOOK_JOB_HISTORY).add({
            "job": job_name,
            "status": status,  # "success", "fallback_success", "failed", "skipped"
            "timestamp": datetime.now(),
            "details": details
        })
        
        # Cleanup: delete old entries beyond 50
        old_docs = list(db.collection(MOLTBOOK_JOB_HISTORY)
            .order_by("timestamp", direction="DESCENDING")
            .offset(50)
            .limit(20)
            .get())
        for doc in old_docs:
            doc.reference.delete()
    except Exception as e:
        logger.error(f"Failed to log job history: {e}")


def _fallback_post(topic: str) -> dict:
    """Direct post without LangGraph - used as fallback."""
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    from agent.personality import AZONI_IDENTITY
    
    client = get_moltbook_client()
    db = get_firestore()
    
    llm = ChatOpenAI(
        model=settings.default_model.split("/")[-1],
        openai_api_key=settings.openrouter_api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        request_timeout=60,
        default_headers={"HTTP-Referer": "https://azoni.ai", "X-Title": "Azoni Moltbook Agent"}
    )
    
    prompt = f"""Write a post for Moltbook (a social platform for AI agents and developers).

Topic: {topic}

Format your response EXACTLY like this:
TITLE: Your engaging title here
SUBMOLT: general
CONTENT: Your post content here (1-3 paragraphs, conversational, end with a question)

You're Azoni, an AI agent for Charlton Smith, a Seattle software engineer. Be genuine."""

    response = llm.invoke([
        SystemMessage(content=AZONI_IDENTITY),
        HumanMessage(content=prompt)
    ])
    
    text = response.content
    title = "Thoughts from Azoni"
    submolt = "general"
    content = text
    
    for line in text.split("\n"):
        upper = line.strip().upper()
        if upper.startswith("TITLE:"):
            title = line.strip().split(":", 1)[1].strip().strip('"')
        elif upper.startswith("SUBMOLT:"):
            submolt = line.strip().split(":", 1)[1].strip().lower()
        elif upper.startswith("CONTENT:"):
            content = line.strip().split(":", 1)[1].strip()
    
    if content == text and "CONTENT:" in text.upper():
        idx = text.upper().index("CONTENT:") + len("CONTENT:")
        content = text[idx:].strip()
    
    result = client.create_post(title=title, content=content, submolt=submolt)
    
    db.collection(MOLTBOOK_ACTIVITY).add({
        "action": "post",
        "timestamp": datetime.now(),
        "date": datetime.now().date().isoformat(),
        "draft": {"title": title, "content": content[:200], "submolt": submolt},
        "decision": {"action": "post", "reason": f"Direct post about: {topic}"},
        "result": result,
        "trigger": "post_job_direct"
    })
    
    return {"title": title, "method": "direct"}


def _fallback_comment() -> dict:
    """Direct comment without LangGraph - used as fallback."""
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    from agent.personality import AZONI_IDENTITY
    
    client = get_moltbook_client()
    db = get_firestore()
    
    llm = ChatOpenAI(
        model=settings.default_model.split("/")[-1],
        openai_api_key=settings.openrouter_api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        request_timeout=60,
        default_headers={"HTTP-Referer": "https://azoni.ai", "X-Title": "Azoni Moltbook Agent"}
    )
    
    # Get feed
    feed = client.get_feed(sort="hot", limit=15)
    new_posts = client.get_feed(sort="new", limit=10)
    seen_ids = set()
    all_posts = []
    for post in feed + new_posts:
        pid = post.get("id")
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            all_posts.append(post)
    
    if not all_posts:
        return {"error": "Empty feed"}
    
    # Find uncommented post
    target = None
    for post in all_posts:
        author = post.get("author", "")
        if isinstance(author, dict):
            author = author.get("name", "")
        if author.lower() in ["azoni-ai", "azoni"]:
            continue
        
        existing = list(db.collection(MOLTBOOK_ACTIVITY)
            .where("action", "==", "comment")
            .where("decision.target_post_id", "==", post.get("id"))
            .limit(1).get())
        if not existing:
            target = post
            break
    
    if not target:
        return {"error": "Already commented on all visible posts"}
    
    post_author = target.get("author", "")
    if isinstance(post_author, dict):
        post_author = post_author.get("name", "unknown")
    
    prompt = f"""Write a comment for this Moltbook post.

Post title: {target.get('title', '')}
Post content: {target.get('content', '')[:500]}
Author: {post_author}

Write a genuine, helpful comment (2-4 sentences). Just the comment text, no labels."""

    response = llm.invoke([
        SystemMessage(content=AZONI_IDENTITY),
        HumanMessage(content=prompt)
    ])
    
    comment_text = response.content.strip()
    for prefix in ["Comment:", "comment:", "Response:", "response:"]:
        if comment_text.startswith(prefix):
            comment_text = comment_text[len(prefix):].strip()
    
    result = client.create_comment(post_id=target["id"], content=comment_text)
    
    db.collection(MOLTBOOK_ACTIVITY).add({
        "action": "comment",
        "timestamp": datetime.now(),
        "date": datetime.now().date().isoformat(),
        "draft": {"content": comment_text[:200]},
        "decision": {"action": "comment", "reason": f"Comment on '{target.get('title', '')}'", "target_post_id": target["id"]},
        "result": result,
        "trigger": "comment_job_direct"
    })
    
    return {"target": target.get("title", ""), "method": "direct"}


def post_job():
    """Create new posts - LangGraph first, fallback to direct."""
    logger.info(f"Post job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping")
        log_job("post", "skipped", {"reason": "autonomous mode off"})
        return
    
    if not can_post():
        logger.info("Post cooldown active, skipping")
        log_job("post", "skipped", {"reason": "cooldown active"})
        return
    
    topic = get_next_post_topic()
    logger.info(f"Post topic: {topic}")
    
    # Try LangGraph first
    try:
        logger.info("Post job: Trying LangGraph pipeline...")
        result = run_agent(
            trigger="heartbeat",
            trigger_context=f"Create a new post about: {topic}. Be authentic and add value."
        )
        
        decision = result.get("decision", {})
        executed = result.get("executed", False)
        error = result.get("error")
        
        logger.info(f"Post job LangGraph: action={decision.get('action')}, executed={executed}, error={error}")
        
        if executed:
            log_job("post", "success", {
                "method": "langgraph",
                "action": decision.get("action"),
                "reason": decision.get("reason", "")[:100],
                "llm_calls": result.get("llm_calls", 0),
                "feed_posts": result.get("feed_posts_seen", len(result.get("feed", [])))
            })
            return
        
        # LangGraph didn't execute - log why and try direct
        reason = "unknown"
        if decision.get("action") == "nothing":
            reason = f"decided nothing: {decision.get('reason', '')[:80]}"
        elif not result.get("quality_check", {}).get("approved"):
            reason = f"draft rejected: score={result.get('quality_check', {}).get('score')}"
        elif error:
            reason = f"error: {error[:80]}"
        else:
            reason = f"action={decision.get('action')} but executed=false"
        
        logger.warning(f"Post job LangGraph failed to execute: {reason}")
        logger.info("Post job: Falling back to direct...")
        
        direct_result = _fallback_post(topic)
        log_job("post", "fallback_success", {
            "method": "direct_fallback",
            "langgraph_reason": reason,
            "title": direct_result.get("title", "")
        })
        logger.info(f"Post job fallback SUCCESS: {direct_result}")
        
    except Exception as e:
        logger.error(f"Post job LangGraph exception: {e}")
        
        # Fallback to direct
        try:
            logger.info("Post job: Falling back to direct after exception...")
            direct_result = _fallback_post(topic)
            log_job("post", "fallback_success", {
                "method": "direct_fallback",
                "langgraph_error": str(e)[:100],
                "title": direct_result.get("title", "")
            })
            logger.info(f"Post job fallback SUCCESS: {direct_result}")
        except Exception as e2:
            logger.error(f"Post job direct fallback also FAILED: {e2}")
            import traceback
            traceback.print_exc()
            log_job("post", "failed", {
                "langgraph_error": str(e)[:100],
                "direct_error": str(e2)[:100]
            })


def comment_job():
    """Comment on posts - LangGraph first, fallback to direct."""
    logger.info(f"Comment job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping comment")
        log_job("comment", "skipped", {"reason": "autonomous mode off"})
        return
    
    # Try LangGraph first
    try:
        logger.info("Comment job: Trying LangGraph pipeline...")
        result = run_agent(
            trigger="heartbeat",
            trigger_context="Find an interesting post to comment on. Add value to the discussion. Do NOT create a new post."
        )
        
        decision = result.get("decision", {})
        executed = result.get("executed", False)
        error = result.get("error")
        
        logger.info(f"Comment job LangGraph: action={decision.get('action')}, executed={executed}, target={decision.get('target_post_id')}, error={error}")
        
        if executed:
            log_job("comment", "success", {
                "method": "langgraph",
                "action": decision.get("action"),
                "target_post_id": decision.get("target_post_id"),
                "reason": decision.get("reason", "")[:100],
                "llm_calls": result.get("llm_calls", 0)
            })
            return
        
        # LangGraph didn't execute
        reason = "unknown"
        if decision.get("action") == "nothing":
            reason = f"decided nothing: {decision.get('reason', '')[:80]}"
        elif decision.get("action") == "comment" and not decision.get("target_post_id"):
            reason = "chose comment but no target post found"
        elif not result.get("quality_check", {}).get("approved"):
            reason = f"draft rejected: score={result.get('quality_check', {}).get('score')}"
        elif error:
            reason = f"error: {error[:80]}"
        else:
            reason = f"action={decision.get('action')} but executed=false"
        
        logger.warning(f"Comment job LangGraph failed to execute: {reason}")
        logger.info("Comment job: Falling back to direct...")
        
        direct_result = _fallback_comment()
        if direct_result.get("error"):
            log_job("comment", "skipped", {"method": "direct_fallback", "reason": direct_result["error"]})
        else:
            log_job("comment", "fallback_success", {
                "method": "direct_fallback",
                "langgraph_reason": reason,
                "target": direct_result.get("target", "")
            })
        logger.info(f"Comment job fallback: {direct_result}")
        
    except Exception as e:
        logger.error(f"Comment job LangGraph exception: {e}")
        
        try:
            logger.info("Comment job: Falling back to direct after exception...")
            direct_result = _fallback_comment()
            if direct_result.get("error"):
                log_job("comment", "skipped", {"reason": direct_result["error"]})
            else:
                log_job("comment", "fallback_success", {
                    "langgraph_error": str(e)[:100],
                    "target": direct_result.get("target", "")
                })
            logger.info(f"Comment job fallback: {direct_result}")
        except Exception as e2:
            logger.error(f"Comment job direct fallback also FAILED: {e2}")
            import traceback
            traceback.print_exc()
            log_job("comment", "failed", {
                "langgraph_error": str(e)[:100],
                "direct_error": str(e2)[:100]
            })


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
            request_timeout=60,
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
        log_job("reply", "success" if replies_made > 0 else "skipped", {
            "replies_made": replies_made,
            "posts_checked": len(our_posts)
        })
                
    except Exception as e:
        logger.error(f"Reply job failed: {e}")
        log_job("reply", "failed", {"error": str(e)[:100]})


def new_post_watcher():
    """Watch for new posts and comment on them immediately. Runs every 2 min."""
    logger.info(f"New post watcher triggered at {datetime.now()}")
    
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
            request_timeout=60,
        default_headers={"HTTP-Referer": "https://azoni.ai", "X-Title": "Azoni Moltbook Agent"}
        )
        
        # Get newest posts
        new_posts = client.get_feed(sort="new", limit=10)
        
        commented = 0
        max_per_run = 3
        
        for post in new_posts:
            if commented >= max_per_run:
                break
            
            post_id = post.get("id")
            author = post.get("author", "")
            if isinstance(author, dict):
                author = author.get("name", "")
            
            # Skip our own posts
            if author.lower() in ["azoni-ai", "azoni"]:
                continue
            
            # Check if we already commented
            existing = list(db.collection(MOLTBOOK_ACTIVITY)
                .where("action", "==", "comment")
                .where("decision.target_post_id", "==", post_id)
                .limit(1).get())
            
            if existing:
                continue
            
            # This is a new post we haven't commented on — go!
            post_title = post.get("title", "")
            post_content = post.get("content", "")
            post_author = author
            
            logger.info(f"New post watcher: Found new post '{post_title}' by {post_author}")
            
            prompt = f"""Write a comment for this NEW Moltbook post. Being one of the first commenters is great!

Post title: {post_title}
Post content: {post_content[:500]}
Author: {post_author}

Write a genuine, engaging comment (2-4 sentences). Welcome the post, add insight, or ask a good question.
Just write the comment text directly, no labels."""

            response = llm.invoke([
                SystemMessage(content=AZONI_IDENTITY),
                HumanMessage(content=prompt)
            ])
            
            comment_text = response.content.strip()
            for prefix in ["Comment:", "comment:", "Response:", "response:"]:
                if comment_text.startswith(prefix):
                    comment_text = comment_text[len(prefix):].strip()
            
            result = client.create_comment(post_id=post_id, content=comment_text)
            
            db.collection(MOLTBOOK_ACTIVITY).add({
                "action": "comment",
                "timestamp": datetime.now(),
                "date": datetime.now().date().isoformat(),
                "draft": {"content": comment_text[:200]},
                "decision": {"action": "comment", "reason": f"Early comment on '{post_title}'", "target_post_id": post_id},
                "result": result,
                "trigger": "new_post_watcher"
            })
            
            logger.info(f"New post watcher: Commented on '{post_title}'")
            commented += 1
            time.sleep(1)
        
        log_job("watcher", "success" if commented > 0 else "skipped", {
            "commented": commented,
            "posts_checked": len(new_posts),
            "reason": "" if commented > 0 else "no new uncommented posts"
        })
            
    except Exception as e:
        logger.error(f"New post watcher failed: {e}")
        import traceback
        traceback.print_exc()
        log_job("watcher", "failed", {"error": str(e)[:100]})


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
                    log_job("upvote", "success", {"upvoted": upvoted})
                    return
                import time
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Failed to upvote {post_id}: {e}")
                continue
        
        log_job("upvote", "success" if upvoted > 0 else "skipped", {
            "upvoted": upvoted,
            "reason": "no new posts to upvote" if upvoted == 0 else ""
        })
                
    except Exception as e:
        logger.error(f"Upvote job failed: {e}")
        log_job("upvote", "failed", {"error": str(e)[:100]})


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
            request_timeout=60,
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
    
    # Use dynamic intervals from Firestore
    reschedule_jobs()
    
    # Run health check 10 seconds after startup
    from apscheduler.triggers.date import DateTrigger
    scheduler.add_job(startup_check, DateTrigger(run_date=datetime.now() + timedelta(seconds=10)), id="startup_check")
    
    scheduler.start()
    intervals = get_intervals()
    logger.info(f"Scheduler started with dynamic intervals: {intervals}")
    
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
    max_posts_per_day: Optional[int] = None
    post_topics: Optional[List[str]] = None
    intervals: Optional[Dict] = None  # {"post": 45, "comment": 10, "reply": 8, "upvote": 15, "watcher": 5}


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
                decision = data.get("decision") or {}
                
                if isinstance(result, dict):
                    if result.get("post", {}).get("id"):
                        post_id = result["post"]["id"]
                    elif result.get("comment", {}).get("post_id"):
                        post_id = result["comment"]["post_id"]
                    elif result.get("id"):
                        post_id = result["id"]
                
                # Fallback: get post_id from decision (for upvotes, comments)
                if not post_id and isinstance(decision, dict):
                    post_id = decision.get("target_post_id")
                
                link = f"https://www.moltbook.com/post/{post_id}" if post_id else None
                
                # Get title safely - check draft, then decision reason
                draft = data.get("draft") or {}
                title = ""
                if isinstance(draft, dict):
                    title = (draft.get("title") or draft.get("content") or "")[:50]
                if not title and isinstance(decision, dict):
                    title = (decision.get("reason") or "")[:50]
                
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
        
        current_intervals = get_intervals()
        job_details = {
            "post": {"interval": f"{current_intervals.get('post', 45)} min", "desc": "Creates engaging posts on interesting topics", "icon": "📝"},
            "comment": {"interval": f"{current_intervals.get('comment', 10)} min", "desc": "Comments on posts to build relationships", "icon": "💬"},
            "reply": {"interval": f"{current_intervals.get('reply', 8)} min", "desc": "Replies to comments on your posts quickly", "icon": "↩️"},
            "upvote": {"interval": f"{current_intervals.get('upvote', 15)} min", "desc": "Upvotes quality content from the community", "icon": "👍"},
            "new_post_watcher": {"interval": f"{current_intervals.get('watcher', 5)} min", "desc": "Watches for new posts and comments first", "icon": "👀"},
        }
        
        next_jobs = []
        if scheduler.running:
            for job in scheduler.get_jobs():
                if job.next_run_time:
                    # Convert to Seattle time
                    next_seattle = job.next_run_time.astimezone(seattle_tz)
                    job_name = job.id.replace("_job", "")
                    details = job_details.get(job_name, job_details.get(job.id, {"interval": "?", "desc": "Scheduled task", "icon": "⚡"}))
                    
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
        
        # Build interval controls HTML
        current_intervals = get_intervals()
        interval_labels = {
            "post": {"icon": "📝", "name": "Post"},
            "comment": {"icon": "💬", "name": "Comment"},
            "reply": {"icon": "↩️", "name": "Reply"},
            "upvote": {"icon": "👍", "name": "Upvote"},
            "watcher": {"icon": "👀", "name": "Watcher"},
        }
        intervals_html = ""
        for key, meta in interval_labels.items():
            val = current_intervals.get(key, 0)
            intervals_html += f'''
                <div class="interval-row">
                    <span class="interval-label">{meta["icon"]} {meta["name"]}</span>
                    <div>
                        <input type="number" class="interval-input" id="interval-{key}" value="{val}" min="0" max="999">
                        <span class="interval-unit">min</span>
                    </div>
                </div>'''
        
        # Get job history
        job_history = []
        try:
            job_docs = db.collection(MOLTBOOK_JOB_HISTORY)\
                .order_by("timestamp", direction="DESCENDING")\
                .limit(15).get()
            
            for doc in job_docs:
                data = doc.to_dict()
                ts = data.get("timestamp")
                try:
                    if ts:
                        if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                            ts = pytz.utc.localize(ts)
                        ts_seattle = ts.astimezone(seattle_tz)
                        time_str = ts_seattle.strftime("%I:%M %p")
                    else:
                        time_str = "?"
                except:
                    time_str = "?"
                
                details = data.get("details", {})
                job_history.append({
                    "job": data.get("job", "?"),
                    "status": data.get("status", "?"),
                    "time": time_str,
                    "details": details
                })
        except Exception as e:
            logger.error(f"Error fetching job history: {e}")
        
        # Build job history HTML
        job_history_html = ""
        if job_history:
            for jh in job_history:
                status = jh["status"]
                status_class = "jh-success" if status == "success" else "jh-fallback" if "fallback" in status else "jh-failed" if status == "failed" else "jh-skipped"
                status_icon = "✅" if status == "success" else "🔄" if "fallback" in status else "❌" if status == "failed" else "⏭️"
                
                detail_parts = []
                details = jh.get("details", {})
                if details.get("method"):
                    detail_parts.append(details["method"])
                if details.get("reason"):
                    detail_parts.append(details["reason"][:60])
                if details.get("langgraph_reason"):
                    detail_parts.append(f"LG: {details['langgraph_reason'][:50]}")
                if details.get("langgraph_error"):
                    detail_parts.append(f"LG err: {details['langgraph_error'][:50]}")
                if details.get("title"):
                    detail_parts.append(f'"{details["title"][:40]}"')
                if details.get("target"):
                    detail_parts.append(f'→ "{details["target"][:40]}"')
                detail_text = " · ".join(detail_parts) if detail_parts else ""
                
                job_history_html += f'''
                <div class="jh-item">
                    <span class="jh-icon">{status_icon}</span>
                    <span class="jh-job">{jh["job"]}</span>
                    <span class="jh-detail">{detail_text}</span>
                    <span class="jh-time">{jh["time"]}</span>
                </div>
                '''
        else:
            job_history_html = '<div class="empty">No job history yet - jobs will log here after first run</div>'
        
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
                .jh-item {{
                    display: flex;
                    align-items: center;
                    gap: 0.5rem;
                    padding: 0.5rem 0;
                    border-bottom: 1px solid rgba(255,255,255,0.05);
                    font-size: 0.85rem;
                }}
                .jh-item:last-child {{ border-bottom: none; }}
                .jh-icon {{ flex-shrink: 0; }}
                .jh-job {{
                    font-weight: 600;
                    min-width: 65px;
                    color: #ccc;
                }}
                .jh-detail {{
                    flex: 1;
                    color: #888;
                    font-size: 0.8rem;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .jh-time {{
                    color: #666;
                    font-size: 0.8rem;
                    flex-shrink: 0;
                }}
                .footer {{
                    text-align: center;
                    margin-top: 2rem;
                    color: #666;
                    font-size: 0.85rem;
                }}
                .footer a {{ color: #60a5fa; text-decoration: none; }}
                .footer a:hover {{ text-decoration: underline; }}
                .interval-row {{
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    padding: 0.6rem 0;
                    border-bottom: 1px solid rgba(255,255,255,0.05);
                }}
                .interval-row:last-child {{ border-bottom: none; }}
                .interval-label {{
                    display: flex;
                    align-items: center;
                    gap: 0.5rem;
                    font-size: 0.9rem;
                }}
                .interval-input {{
                    width: 60px;
                    background: rgba(255,255,255,0.1);
                    border: 1px solid rgba(255,255,255,0.2);
                    border-radius: 6px;
                    color: #fff;
                    padding: 0.3rem 0.5rem;
                    text-align: center;
                    font-size: 0.9rem;
                }}
                .interval-input:focus {{
                    outline: none;
                    border-color: #60a5fa;
                }}
                .interval-unit {{
                    color: #666;
                    font-size: 0.8rem;
                    margin-left: 0.3rem;
                }}
                .btn {{
                    background: linear-gradient(90deg, #ff6b6b, #ffa500);
                    border: none;
                    color: #fff;
                    padding: 0.5rem 1.25rem;
                    border-radius: 8px;
                    cursor: pointer;
                    font-size: 0.85rem;
                    font-weight: 600;
                    margin-top: 0.75rem;
                    transition: opacity 0.2s;
                }}
                .btn:hover {{ opacity: 0.85; }}
                .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
                .btn-sm {{
                    padding: 0.3rem 0.75rem;
                    font-size: 0.8rem;
                    margin-top: 0;
                }}
                .save-status {{
                    color: #4ade80;
                    font-size: 0.8rem;
                    margin-left: 0.75rem;
                    opacity: 0;
                    transition: opacity 0.3s;
                }}
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
                        <h2>⚙️ Job Intervals</h2>
                        {intervals_html}
                        <div style="display: flex; align-items: center; margin-top: 0.75rem;">
                            <button class="btn" onclick="saveIntervals()">Save</button>
                            <span class="save-status" id="save-status">✓ Saved</span>
                        </div>
                        <div style="font-size: 0.75rem; color: #666; margin-top: 0.5rem;">Set to 0 to disable a job</div>
                    </div>
                </div>
                
                <div class="section">
                    <h2>📝 Post Topics Queue</h2>
                    {topics_html if topics else '<div class="empty">No topics queued - agent will pick interesting topics from feed</div>'}
                    <div style="margin-top: 1rem; font-size: 0.8rem; color: #666;">
                        {len(topics)} topic{"s" if len(topics) != 1 else ""} in queue
                    </div>
                </div>
                
                <div class="section">
                    <h2>📋 Recent Activity</h2>
                    {activity_html}
                </div>
                
                <div class="section">
                    <h2>🔧 Job History</h2>
                    {job_history_html}
                </div>
                
                <div class="footer">
                    Auto-refreshes every 60 seconds • <a href="/status">JSON API</a> • <a href="https://www.moltbook.com/u/Azoni-AI" target="_blank">View Profile ↗</a>
                </div>
            </div>
            <script>
                setTimeout(function() {{ location.reload(); }}, 60000);
                
                let adminKey = sessionStorage.getItem('adminKey') || '';
                
                function getAdminKey() {{
                    if (!adminKey) {{
                        adminKey = prompt('Enter admin key:');
                        if (adminKey) sessionStorage.setItem('adminKey', adminKey);
                    }}
                    return adminKey;
                }}
                
                async function saveIntervals() {{
                    const key = getAdminKey();
                    if (!key) return;
                    
                    const btn = document.querySelector('.btn');
                    const status = document.getElementById('save-status');
                    btn.disabled = true;
                    btn.textContent = 'Saving...';
                    
                    const intervals = {{}};
                    ['post', 'comment', 'reply', 'upvote', 'watcher'].forEach(key => {{
                        const input = document.getElementById('interval-' + key);
                        if (input) intervals[key] = parseInt(input.value) || 0;
                    }});
                    
                    try {{
                        const res = await fetch('/config', {{
                            method: 'PATCH',
                            headers: {{
                                'Content-Type': 'application/json',
                                'X-Admin-Key': adminKey
                            }},
                            body: JSON.stringify({{intervals}})
                        }});
                        
                        if (res.status === 401) {{
                            adminKey = '';
                            sessionStorage.removeItem('adminKey');
                            status.style.opacity = '1';
                            status.style.color = '#f87171';
                            status.textContent = '✗ Bad key';
                            setTimeout(() => {{ status.style.opacity = '0'; status.style.color = '#4ade80'; }}, 3000);
                        }} else {{
                            const data = await res.json();
                            if (data.success) {{
                                status.style.opacity = '1';
                                status.textContent = '✓ Saved & rescheduled';
                                setTimeout(() => {{ status.style.opacity = '0'; }}, 3000);
                            }}
                        }}
                    }} catch(e) {{
                        status.style.opacity = '1';
                        status.style.color = '#f87171';
                        status.textContent = '✗ Failed';
                        setTimeout(() => {{ status.style.opacity = '0'; status.style.color = '#4ade80'; }}, 3000);
                    }}
                    
                    btn.disabled = false;
                    btn.textContent = 'Save';
                }}
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
        "scheduler_running": scheduler.running,
        "scheduler_jobs": scheduler_jobs
    }


@app.post("/register", dependencies=[Depends(require_admin)])
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


@app.post("/run", dependencies=[Depends(require_admin)])
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


@app.post("/run/sync", dependencies=[Depends(require_admin)])
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


@app.post("/post", dependencies=[Depends(require_admin)])
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


@app.post("/comment", dependencies=[Depends(require_admin)])
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


@app.patch("/config", dependencies=[Depends(require_admin)])
async def update_config(request: ConfigUpdate):
    """Update agent configuration."""
    db = get_firestore()
    
    update_data = {}
    if request.autonomous_mode is not None:
        update_data["autonomous_mode"] = request.autonomous_mode
    if request.max_posts_per_day is not None:
        update_data["max_posts_per_day"] = request.max_posts_per_day
    if request.post_topics is not None:
        update_data["post_topics"] = request.post_topics
    if request.intervals is not None:
        update_data["intervals"] = request.intervals
    
    if update_data:
        update_data["updated_at"] = datetime.now()
        db.collection(MOLTBOOK_CONFIG).document("settings").set(update_data, merge=True)
    
    # Reschedule if intervals changed
    if request.intervals is not None:
        reschedule_jobs()
    
    return {"success": True, "updated": update_data}


@app.get("/config")
async def get_config():
    """Get current configuration."""
    db = get_firestore()
    
    config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
    config_data = config_doc.to_dict() if config_doc.exists else {}
    
    return {
        "autonomous_mode": config_data.get("autonomous_mode", False),
        "max_posts_per_day": config_data.get("max_posts_per_day", 6),
        "post_topics": config_data.get("post_topics", []),
        "intervals": {**DEFAULT_INTERVALS, **config_data.get("intervals", {})}
    }


# ==================== Post Topics Queue ====================

@app.get("/topics")
async def get_topics():
    """Get the post topics queue."""
    db = get_firestore()
    config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
    config_data = config_doc.to_dict() if config_doc.exists else {}
    return {"topics": config_data.get("post_topics", [])}


@app.post("/topics", dependencies=[Depends(require_admin)])
async def add_topic(topic: str):
    """Add a topic to the queue."""
    db = get_firestore()
    from google.cloud.firestore import ArrayUnion
    db.collection(MOLTBOOK_CONFIG).document("settings").set({
        "post_topics": ArrayUnion([topic])
    }, merge=True)
    return {"success": True, "added": topic}


@app.delete("/topics/{index}", dependencies=[Depends(require_admin)])
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


# ==================== Debug & Job History ====================

@app.get("/jobs")
async def get_job_history():
    """Get recent job execution history."""
    db = get_firestore()
    try:
        docs = list(db.collection(MOLTBOOK_JOB_HISTORY)
            .order_by("timestamp", direction="DESCENDING")
            .limit(20).get())
        
        jobs = []
        for doc in docs:
            data = doc.to_dict()
            ts = data.get("timestamp")
            jobs.append({
                "job": data.get("job"),
                "status": data.get("status"),
                "timestamp": ts.isoformat() if ts else None,
                "details": data.get("details", {})
            })
        return {"jobs": jobs}
    except Exception as e:
        return {"error": str(e)}


@app.post("/debug/comment", dependencies=[Depends(require_admin)])
async def debug_comment():
    """Debug: Run comment job directly and return verbose output."""
    import io
    
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    
    try:
        comment_job()
    except Exception as e:
        log_capture.write(f"\nEXCEPTION: {str(e)}\n")
        import traceback
        traceback.print_exc(file=log_capture)
    
    logger.removeHandler(handler)
    
    return {"logs": log_capture.getvalue()}


@app.post("/debug/post", dependencies=[Depends(require_admin)])
async def debug_post():
    """Debug: Run post job directly and return verbose output."""
    import io
    
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    
    try:
        post_job()
    except Exception as e:
        log_capture.write(f"\nEXCEPTION: {str(e)}\n")
        import traceback
        traceback.print_exc(file=log_capture)
    
    logger.removeHandler(handler)
    
    return {"logs": log_capture.getvalue()}


# ==================== Run Server ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)