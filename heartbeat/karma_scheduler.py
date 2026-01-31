"""
Aggressive Karma Maximizer Scheduler for Azoni Moltbook Agent.

Runs multiple jobs to maximize engagement:
1. Post job - every 35 minutes (respects 30-min cooldown with buffer)
2. Comment job - every 15 minutes (engage with feed)
3. Reply job - every 10 minutes (reply to comments on our posts)
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agent import run_agent
from agent.tools import get_moltbook_client
from config.firebase import get_firestore, MOLTBOOK_CONFIG, MOLTBOOK_STATE, MOLTBOOK_ACTIVITY
from config.settings import settings

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from agent.personality import AZONI_IDENTITY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def check_autonomous_mode() -> bool:
    """Check if autonomous mode is enabled in config."""
    try:
        db = get_firestore()
        config_doc = db.collection(MOLTBOOK_CONFIG).document("settings").get()
        if config_doc.exists:
            return config_doc.to_dict().get("autonomous_mode", False)
        return False
    except Exception as e:
        logger.error(f"Error checking autonomous mode: {e}")
        return False


def get_llm():
    """Get the LLM instance."""
    return ChatOpenAI(
        model=settings.default_model.split("/")[-1],
        openai_api_key=settings.openrouter_api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://azoni.ai",
            "X-Title": "Azoni Moltbook Agent"
        }
    )


def can_post() -> bool:
    """Check if we can post (30 min cooldown)."""
    try:
        db = get_firestore()
        # Get last post
        posts = list(db.collection(MOLTBOOK_ACTIVITY)
            .where("action", "==", "post")
            .order_by("timestamp", direction="DESCENDING")
            .limit(1)
            .get())
        
        if not posts:
            return True
        
        last_post_time = posts[0].to_dict().get("timestamp")
        if last_post_time:
            # Handle Firestore timestamp
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
    """
    Job to create new posts.
    Runs every 35 minutes but only posts if cooldown has passed.
    """
    logger.info(f"Post job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping post job")
        return
    
    if not can_post():
        logger.info("Post cooldown active, skipping")
        return
    
    logger.info("Creating new post...")
    
    try:
        result = run_agent(
            trigger="heartbeat",
            trigger_context="Create a new post. Share something interesting about AI development, your projects, or engage with a trending topic. Be authentic and add value."
        )
        
        logger.info(f"Post job completed: {result.get('decision', {}).get('action')}, executed={result.get('executed')}")
        
    except Exception as e:
        logger.error(f"Post job failed: {e}")


def comment_job():
    """
    Job to comment on interesting posts.
    Runs every 15 minutes.
    """
    logger.info(f"Comment job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping comment job")
        return
    
    logger.info("Looking for posts to comment on...")
    
    try:
        result = run_agent(
            trigger="heartbeat",
            trigger_context="Find an interesting post to comment on. Look for discussions about AI, coding, or building products. Add a thoughtful comment that contributes to the conversation. Do NOT create a new post - only comment."
        )
        
        logger.info(f"Comment job completed: {result.get('decision', {}).get('action')}, executed={result.get('executed')}")
        
    except Exception as e:
        logger.error(f"Comment job failed: {e}")


def reply_job():
    """
    Job to reply to comments on our posts.
    This is key for engagement and karma!
    """
    logger.info(f"Reply job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping reply job")
        return
    
    try:
        client = get_moltbook_client()
        db = get_firestore()
        llm = get_llm()
        
        # Get our recent posts from activity log
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
            
            logger.info(f"Checking comments on post {post_id}")
            
            try:
                # Get comments on this post
                comments = client.get_comments(post_id)
                
                for comment in comments:
                    comment_id = comment.get("id")
                    comment_author = comment.get("author")
                    comment_content = comment.get("content", "")
                    
                    # Handle author object
                    if isinstance(comment_author, dict):
                        author_name = comment_author.get("name", "unknown")
                    else:
                        author_name = comment_author or "unknown"
                    
                    # Skip our own comments
                    if author_name.lower() in ["azoni-ai", "azoni", "azoniai"]:
                        continue
                    
                    # Check if we already replied to this comment
                    existing_replies = list(db.collection(MOLTBOOK_ACTIVITY)
                        .where("action", "==", "comment")
                        .where("decision.target_comment_id", "==", comment_id)
                        .limit(1)
                        .get())
                    
                    if existing_replies:
                        continue
                    
                    logger.info(f"Generating reply to comment by {author_name}")
                    
                    # Generate a reply
                    prompt = f"""Someone commented on your Moltbook post. Write a brief, friendly reply.

Their comment: "{comment_content}"
Author: {author_name}

Guidelines:
- Be conversational and genuine
- Thank them if appropriate
- Add something to the discussion
- Keep it short (1-3 sentences)
- Don't be sycophantic

Write only the reply, nothing else."""

                    messages = [
                        SystemMessage(content=AZONI_IDENTITY),
                        HumanMessage(content=prompt)
                    ]
                    
                    response = llm.invoke(messages)
                    reply_content = response.content.strip()
                    
                    # Post the reply
                    result = client.create_comment(
                        post_id=post_id,
                        content=reply_content,
                        parent_id=comment_id
                    )
                    
                    # Log it
                    db.collection(MOLTBOOK_ACTIVITY).add({
                        "action": "comment",
                        "timestamp": datetime.now(),
                        "date": datetime.now().date().isoformat(),
                        "draft": {"content": reply_content},
                        "decision": {
                            "action": "comment",
                            "reason": f"Reply to {author_name}'s comment",
                            "target_post_id": post_id,
                            "target_comment_id": comment_id
                        },
                        "result": result,
                        "trigger": "reply_job"
                    })
                    
                    logger.info(f"Replied to {author_name}'s comment")
                    
                    # Only reply to one comment per run to avoid spam
                    return
                    
            except Exception as e:
                logger.error(f"Error processing post {post_id}: {e}")
                continue
        
        logger.info("No new comments to reply to")
        
    except Exception as e:
        logger.error(f"Reply job failed: {e}")


def upvote_job():
    """
    Job to upvote good content.
    Helps build community goodwill.
    """
    logger.info(f"Upvote job triggered at {datetime.now()}")
    
    if not check_autonomous_mode():
        logger.info("Autonomous mode disabled, skipping upvote job")
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
                
                logger.info(f"Upvoted post: {post.get('title', 'Unknown')[:50]}")
                
                # Only upvote one per run
                return
                
            except Exception as e:
                logger.error(f"Failed to upvote {post_id}: {e}")
                continue
        
    except Exception as e:
        logger.error(f"Upvote job failed: {e}")


def run_scheduler():
    """
    Start the aggressive karma scheduler.
    """
    scheduler = BlockingScheduler()
    
    logger.info("Starting AGGRESSIVE karma scheduler")
    logger.info("- Post job: every 35 minutes")
    logger.info("- Comment job: every 15 minutes") 
    logger.info("- Reply job: every 10 minutes")
    logger.info("- Upvote job: every 20 minutes")
    
    # Post job - every 35 minutes (30 min cooldown + 5 min buffer)
    scheduler.add_job(
        post_job,
        trigger=IntervalTrigger(minutes=35),
        id="post_job",
        name="Post Job",
        replace_existing=True
    )
    
    # Comment job - every 15 minutes
    scheduler.add_job(
        comment_job,
        trigger=IntervalTrigger(minutes=15),
        id="comment_job",
        name="Comment Job",
        replace_existing=True
    )
    
    # Reply job - every 10 minutes
    scheduler.add_job(
        reply_job,
        trigger=IntervalTrigger(minutes=10),
        id="reply_job",
        name="Reply Job",
        replace_existing=True
    )
    
    # Upvote job - every 20 minutes
    scheduler.add_job(
        upvote_job,
        trigger=IntervalTrigger(minutes=20),
        id="upvote_job",
        name="Upvote Job",
        replace_existing=True
    )
    
    # Run all jobs once on startup
    scheduler.add_job(post_job, trigger="date", run_date=datetime.now(), id="startup_post")
    scheduler.add_job(comment_job, trigger="date", run_date=datetime.now() + timedelta(seconds=30), id="startup_comment")
    scheduler.add_job(reply_job, trigger="date", run_date=datetime.now() + timedelta(seconds=60), id="startup_reply")
    scheduler.add_job(upvote_job, trigger="date", run_date=datetime.now() + timedelta(seconds=90), id="startup_upvote")
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    run_scheduler()