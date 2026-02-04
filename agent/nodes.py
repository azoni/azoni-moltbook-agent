"""
LangGraph nodes for Azoni Moltbook Agent.

Each node is a step in the agent's workflow.
"""
import json
import logging
import httpx
import os
from datetime import datetime, timedelta
from typing import Dict, Any

logger = logging.getLogger(__name__)

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from agent.state import AgentState, AgentDecision, DraftContent, QualityCheck
from agent.tools import get_moltbook_client
from agent.personality import (
    AZONI_IDENTITY, 
    OBSERVE_PROMPT, 
    DECIDE_PROMPT,
    DRAFT_POST_PROMPT,
    DRAFT_COMMENT_PROMPT,
    EVALUATE_PROMPT
)
from config.settings import settings
from config.firebase import get_firestore, MOLTBOOK_ACTIVITY, MOLTBOOK_STATE


# ==================== Activity Logging to azoni.ai ====================

ACTIVITY_WEBHOOK_URL = "https://azoni.ai/.netlify/functions/log-agent-activity"
AGENT_WEBHOOK_SECRET = os.getenv("AGENT_WEBHOOK_SECRET", "")


def log_agent_activity_sync(
    activity_type: str,
    title: str,
    description: str = "",
    reasoning: str = "",
    metadata: dict = None
):
    """Sync version of activity logging for non-async contexts."""
    if not AGENT_WEBHOOK_SECRET:
        logger.warning("[activity] AGENT_WEBHOOK_SECRET not set, skipping log")
        return
    
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(
                ACTIVITY_WEBHOOK_URL,
                json={
                    "type": activity_type,
                    "title": title,
                    "description": description,
                    "reasoning": reasoning,
                    "metadata": metadata or {},
                    "secret": AGENT_WEBHOOK_SECRET
                }
            )
            if response.status_code == 200:
                logger.info(f"[activity] Logged to azoni.ai: {activity_type} - {title}")
            else:
                logger.warning(f"[activity] Failed to log: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"[activity] Error logging to azoni.ai: {e}")


def get_llm():
    """Get the LLM instance."""
    return ChatOpenAI(
        model=settings.default_model.split("/")[-1],
        openai_api_key=settings.openrouter_api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        request_timeout=60,
        default_headers={
            "HTTP-Referer": "https://azoni.ai",
            "X-Title": "Azoni Moltbook Agent"
        }
    )


# ==================== Node: Observe ====================

def observe_node(state: AgentState) -> Dict[str, Any]:
    """Observe the Moltbook feed and gather context."""
    client = get_moltbook_client()
    
    try:
        feed = client.get_feed(sort="hot", limit=15)
        logger.info(f"[observe] Hot feed: {len(feed)} posts")
        
        new_posts = client.get_feed(sort="new", limit=10)
        logger.info(f"[observe] New feed: {len(new_posts)} posts")
        
        seen_ids = set()
        combined_feed = []
        for post in feed + new_posts:
            post_id = post.get("id")
            if post_id and post_id not in seen_ids:
                seen_ids.add(post_id)
                combined_feed.append(post)
        
        logger.info(f"[observe] Combined feed: {len(combined_feed)} unique posts")
        for p in combined_feed[:3]:
            author = p.get("author", "")
            if isinstance(author, dict):
                author = author.get("name", "")
            logger.info(f"[observe]   - '{p.get('title', '?')[:40]}' by {author} (id={p.get('id', '?')[:12]}...)")
        
        db = get_firestore()
        state_doc = db.collection(MOLTBOOK_STATE).document("agent").get()
        last_activity = None
        if state_doc.exists:
            state_data = state_doc.to_dict()
            last_activity = state_data.get("last_activity")
        
        return {
            "feed": combined_feed[:20],
            "notifications": [],
            "last_activity": last_activity
        }
    
    except Exception as e:
        logger.error(f"[observe] FAILED: {e}")
        return {
            "feed": [],
            "notifications": [],
            "error": f"Failed to observe: {str(e)}"
        }


# ==================== Node: Decide ====================

def decide_node(state: AgentState) -> Dict[str, Any]:
    """Decide what action to take based on observations."""
    if state.get("error"):
        return {"decision": {"action": "nothing", "reason": f"Error in observation: {state['error']}"}}
    
    llm = get_llm()
    
    feed_summary = []
    for post in state.get("feed", [])[:10]:
        feed_summary.append(
            f"- [{post.get('submolt', 'general')}] {post.get('title', 'No title')} "
            f"by {post.get('author', 'unknown')} ({post.get('upvotes', 0)} upvotes, "
            f"{post.get('comment_count', 0)} comments)"
        )
    feed_text = "\n".join(feed_summary) if feed_summary else "Feed is empty"
    
    db = get_firestore()
    today = datetime.now().date().isoformat()
    
    posts_today = db.collection(MOLTBOOK_ACTIVITY)\
        .where("action", "==", "post")\
        .where("date", "==", today)\
        .limit(10).get()
    posts_today_count = len(list(posts_today))
    
    last_post = db.collection(MOLTBOOK_ACTIVITY)\
        .where("action", "==", "post")\
        .order_by("timestamp", direction="DESCENDING")\
        .limit(1).get()
    
    last_post_time = "Never"
    for doc in last_post:
        ts = doc.to_dict().get("timestamp")
        if ts:
            last_post_time = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
    
    last_comment = db.collection(MOLTBOOK_ACTIVITY)\
        .where("action", "==", "comment")\
        .order_by("timestamp", direction="DESCENDING")\
        .limit(1).get()
    
    last_comment_time = "Never"
    for doc in last_comment:
        ts = doc.to_dict().get("timestamp")
        if ts:
            last_comment_time = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
    
    prompt = DECIDE_PROMPT.format(
        observations=feed_text,
        last_post_time=last_post_time,
        posts_today=posts_today_count,
        last_comment_time=last_comment_time,
        trigger_context=state.get("trigger_context") or "Regular heartbeat check"
    )
    
    messages = [
        SystemMessage(content=AZONI_IDENTITY),
        HumanMessage(content=prompt)
    ]
    
    response = llm.invoke(messages)
    response_text = response.content.lower()
    
    trigger_context = (state.get("trigger_context") or "").lower()
    
    force_comment = any(phrase in trigger_context for phrase in [
        "comment on", "reply to", "respond to", "leave a comment",
        "action: comment", "force comment", "find an interesting post",
        "find a post", "do not create a new post", "add value to the discussion"
    ])
    force_post = not force_comment and any(phrase in trigger_context for phrase in [
        "create a new post about", "make a post", "write a post", "post about",
        "introduce yourself", "share something", "force post",
        "must post", "should post", "please post", "do a post", "action: post"
    ])
    
    decision: AgentDecision = {
        "action": "nothing",
        "reason": response.content,
        "target_post_id": None,
        "target_submolt": None
    }
    
    feed = state.get("feed", [])
    
    def find_target_post(response_text: str, feed: list) -> str:
        for post in feed:
            title = post.get("title", "").lower()
            if title and len(title) > 5 and title[:20] in response_text:
                return post.get("id")
        
        for post in feed:
            post_id = post.get("id", "")
            if post_id and post_id in response.content:
                return post_id
        
        scored = []
        for post in feed:
            author = post.get("author", "")
            if isinstance(author, dict):
                author = author.get("name", "")
            if author.lower() in ["azoni-ai", "azoni"]:
                continue
            
            comment_count = post.get("comment_count", 0)
            upvotes = post.get("upvotes", 0)
            score = max(0, 5 - comment_count) + min(upvotes, 3)
            scored.append((score, post.get("id")))
        
        if scored:
            scored.sort(reverse=True)
            return scored[0][1]
        
        for post in feed:
            author = post.get("author", "")
            if isinstance(author, dict):
                author = author.get("name", "")
            if author.lower() not in ["azoni-ai", "azoni"]:
                return post.get("id")
        
        return None
    
    if force_comment:
        decision["action"] = "comment"
        decision["target_post_id"] = find_target_post(response_text, feed)
        decision["reason"] = f"Forced comment action from context: {trigger_context}"
    elif force_post:
        decision["action"] = "post"
        decision["reason"] = f"Forced post action from context: {trigger_context}"
    elif "\"post\"" in response_text or "action: post" in response_text or "decide to post" in response_text or "create a post" in response_text:
        decision["action"] = "post"
    elif "\"comment\"" in response_text or "action: comment" in response_text or "decide to comment" in response_text or "comment on" in response_text:
        decision["action"] = "comment"
        decision["target_post_id"] = find_target_post(response_text, feed)
    elif "\"upvote\"" in response_text or "action: upvote" in response_text:
        decision["action"] = "upvote"
        decision["target_post_id"] = find_target_post(response_text, feed)
    
    if decision["action"] == "comment" and not decision["target_post_id"] and feed:
        decision["target_post_id"] = find_target_post("", feed)
    
    if decision["action"] == "upvote" and not decision["target_post_id"] and feed:
        decision["target_post_id"] = find_target_post("", feed)
    
    logger.info(f"[decide] Action: {decision['action']}, target_post_id: {decision.get('target_post_id')}, reason: {decision.get('reason', '')[:80]}")
    
    return {
        "decision": decision,
        "llm_calls": state.get("llm_calls", 0) + 1
    }


# ==================== Node: Draft ====================

def draft_node(state: AgentState) -> Dict[str, Any]:
    """Draft content for posting or commenting."""
    decision = state.get("decision", {})
    action = decision.get("action")
    
    if action not in ["post", "comment"]:
        logger.info(f"[draft] Skipping - action is '{action}'")
        return {"draft": None}
    
    llm = get_llm()
    
    if action == "post":
        prompt = DRAFT_POST_PROMPT.format(
            context=state.get("trigger_context") or decision.get("reason", "Share something interesting"),
            identity=AZONI_IDENTITY
        )
    else:
        target_post_id = decision.get("target_post_id")
        target_post = None
        for post in state.get("feed", []):
            if post.get("id") == target_post_id:
                target_post = post
                break
        
        if not target_post:
            logger.warning(f"[draft] Could not find target post {target_post_id}")
            feed_ids = [p.get("id", "?")[:12] for p in state.get("feed", [])[:5]]
            logger.warning(f"[draft] Available IDs: {feed_ids}")
            return {"draft": None, "error": f"Could not find target post {target_post_id}"}
        
        logger.info(f"[draft] Found target post: '{target_post.get('title', '?')[:40]}'")
        
        prompt = DRAFT_COMMENT_PROMPT.format(
            post_title=target_post.get("title", ""),
            post_content=target_post.get("content", ""),
            post_author=target_post.get("author", ""),
            identity=AZONI_IDENTITY
        )
    
    messages = [
        SystemMessage(content=AZONI_IDENTITY),
        HumanMessage(content=prompt)
    ]
    
    response = llm.invoke(messages)
    response_text = response.content
    
    draft: DraftContent = {
        "content": response_text,
        "title": None,
        "submolt": "general"
    }
    
    if action == "post":
        import re
        
        lines = response_text.split("\n")
        content_lines = []
        
        for line in lines:
            line_lower = line.lower().strip()
            line_stripped = line.strip()
            
            if line_lower.startswith("title:") or line_lower.startswith("**title"):
                title_match = re.search(r'[tT]itle[:\s*]+(.+)', line_stripped)
                if title_match:
                    draft["title"] = title_match.group(1).strip().strip('"').strip('*')
            elif line_lower.startswith("submolt:") or line_lower.startswith("**submolt"):
                submolt_match = re.search(r'[sS]ubmolt[:\s*]+(.+)', line_stripped)
                if submolt_match:
                    draft["submolt"] = submolt_match.group(1).strip().strip('"').strip('*').lower()
            elif line_lower.startswith("content:") or line_lower.startswith("**content"):
                content_match = re.search(r'[cC]ontent[:\s*]+(.+)', line_stripped)
                if content_match:
                    content_lines.append(content_match.group(1).strip())
            elif draft["title"] and not line_lower.startswith(("title", "submolt", "**")):
                if line_stripped and not line_lower.startswith("---"):
                    content_lines.append(line_stripped)
        
        if content_lines:
            draft["content"] = "\n".join(content_lines).strip()
        
        if not draft["title"] and draft["content"]:
            first_line = draft["content"].split("\n")[0].split(".")[0]
            if len(first_line) > 10:
                draft["title"] = first_line[:60] + ("..." if len(first_line) > 60 else "")
            else:
                draft["title"] = "Thoughts from Azoni"
        
        if not draft["title"]:
            draft["title"] = "Hello from Azoni-AI"
        
        if not draft["content"] or draft["content"] == response_text:
            draft["content"] = response_text
        
        if not draft["submolt"]:
            draft["submolt"] = "general"
    
    return {
        "draft": draft,
        "llm_calls": state.get("llm_calls", 0) + 1
    }


# ==================== Node: Evaluate ====================

def evaluate_node(state: AgentState) -> Dict[str, Any]:
    """Evaluate the draft for quality before posting."""
    draft = state.get("draft")
    
    if not draft:
        return {"quality_check": {"approved": False, "score": 0, "issues": ["No draft"], "suggestions": []}}
    
    decision = state.get("decision", {})
    action = decision.get("action")
    
    if action == "comment" and draft.get("content"):
        return {
            "quality_check": {"approved": True, "score": 0.8, "issues": [], "suggestions": []},
            "llm_calls": state.get("llm_calls", 0)
        }
    
    if action == "post" and draft.get("content"):
        content = draft.get("content", "").lower()
        red_flags = ["error", "undefined", "null", "lorem ipsum", "todo", "fixme", "exception", "traceback"]
        has_red_flags = any(flag in content for flag in red_flags)
        
        if not has_red_flags:
            return {
                "quality_check": {"approved": True, "score": 0.85, "issues": [], "suggestions": []},
                "llm_calls": state.get("llm_calls", 0)
            }
    
    llm = get_llm()
    draft_text = f"Title: {draft.get('title', 'N/A')}\nContent: {draft.get('content', '')}\nSubmolt: {draft.get('submolt', 'N/A')}"
    prompt = EVALUATE_PROMPT.format(draft=draft_text)
    
    messages = [
        SystemMessage(content="You are a quality checker. Be generous - approve most posts unless serious issues. Respond with JSON: {\"approved\": true/false, \"score\": 0.0-1.0, \"issues\": [], \"suggestions\": []}"),
        HumanMessage(content=prompt)
    ]
    
    response = llm.invoke(messages)
    
    quality_check: QualityCheck = {"approved": True, "score": 0.8, "issues": [], "suggestions": []}
    
    try:
        import re
        json_match = re.search(r'\{[^}]+\}', response.content)
        if json_match:
            parsed = json.loads(json_match.group())
            quality_check["approved"] = parsed.get("approved", True)
            quality_check["score"] = float(parsed.get("score", 0.8))
            quality_check["issues"] = parsed.get("issues", [])
            quality_check["suggestions"] = parsed.get("suggestions", [])
    except:
        pass
    
    if quality_check["score"] >= 0.5:
        quality_check["approved"] = True
    
    return {"quality_check": quality_check, "llm_calls": state.get("llm_calls", 0) + 1}


# ==================== Node: Execute ====================

def execute_node(state: AgentState) -> Dict[str, Any]:
    """Execute the decided action on Moltbook."""
    decision = state.get("decision", {})
    action = decision.get("action")
    draft = state.get("draft")
    quality_check = state.get("quality_check", {})
    
    if action == "nothing":
        return {"executed": False, "execution_result": {"skipped": True, "reason": "No action"}}
    
    if action in ["post", "comment"] and not quality_check.get("approved"):
        return {"executed": False, "execution_result": {"skipped": True, "reason": "Not approved"}}
    
    client = get_moltbook_client()
    db = get_firestore()
    
    try:
        result = {}
        
        if action == "post":
            logger.info(f"[execute] Creating post: '{draft.get('title', '?')[:40]}'")
            result = client.create_post(
                title=draft.get("title", "Untitled"),
                content=draft.get("content", ""),
                submolt=draft.get("submolt", "general")
            )
            logger.info(f"[execute] Post created: {result.get('post', {}).get('id', '?')}")
            
            # === LOG TO AZONI.AI ACTIVITY FEED ===
            log_agent_activity_sync(
                activity_type="moltbook_post",
                title="Posted to Moltbook",
                description=draft.get("title", "Untitled"),
                reasoning=decision.get("reason", "")[:200],
                metadata={
                    "post_id": result.get("post", {}).get("id"),
                    "submolt": draft.get("submolt", "general")
                }
            )
        
        elif action == "comment":
            target_post_id = decision.get("target_post_id")
            logger.info(f"[execute] Commenting on {target_post_id}")
            if target_post_id:
                result = client.create_comment(post_id=target_post_id, content=draft.get("content", ""))
                logger.info(f"[execute] Comment created")
                
                post_title = "a post"
                for post in state.get("feed", []):
                    if post.get("id") == target_post_id:
                        post_title = post.get("title", "a post")[:50]
                        break
                
                # === LOG TO AZONI.AI ACTIVITY FEED ===
                log_agent_activity_sync(
                    activity_type="moltbook_comment",
                    title="Commented on Moltbook",
                    description=f"Replied to: {post_title}",
                    reasoning=decision.get("reason", "")[:200],
                    metadata={"post_id": target_post_id}
                )
            else:
                logger.warning("[execute] No target_post_id for comment!")
        
        elif action == "upvote":
            target_post_id = decision.get("target_post_id")
            logger.info(f"[execute] Upvoting {target_post_id}")
            if target_post_id:
                result = client.upvote_post(target_post_id)
                logger.info(f"[execute] Upvoted")
                
                post_title = "a post"
                for post in state.get("feed", []):
                    if post.get("id") == target_post_id:
                        post_title = post.get("title", "a post")[:50]
                        break
                
                # === LOG TO AZONI.AI ACTIVITY FEED ===
                log_agent_activity_sync(
                    activity_type="moltbook_upvote",
                    title="Upvoted on Moltbook",
                    description=f"Upvoted: {post_title}",
                    reasoning="Quality content worth promoting",
                    metadata={"post_id": target_post_id}
                )
        
        # Log to Firestore (existing behavior)
        db.collection(MOLTBOOK_ACTIVITY).add({
            "action": action,
            "timestamp": datetime.now(),
            "date": datetime.now().date().isoformat(),
            "draft": draft,
            "decision_reason": decision.get("reason"),
            "result": result,
            "quality_score": quality_check.get("score"),
            "trigger": state.get("trigger"),
            "trigger_context": state.get("trigger_context")
        })
        
        db.collection(MOLTBOOK_STATE).document("agent").set({
            "last_activity": datetime.now(),
            "last_action": action
        }, merge=True)
        
        return {"executed": True, "execution_result": result, "completed_at": datetime.now()}
    
    except Exception as e:
        logger.error(f"[execute] FAILED: {e}")
        db.collection(MOLTBOOK_ACTIVITY).add({
            "action": action,
            "timestamp": datetime.now(),
            "date": datetime.now().date().isoformat(),
            "error": str(e),
            "decision": decision,
            "trigger": state.get("trigger")
        })
        return {"executed": False, "error": str(e), "completed_at": datetime.now()}


# ==================== Node: Log ====================

def log_node(state: AgentState) -> Dict[str, Any]:
    """Final logging node."""
    db = get_firestore()
    
    run_summary = {
        "started_at": state.get("started_at"),
        "completed_at": datetime.now(),
        "trigger": state.get("trigger"),
        "trigger_context": state.get("trigger_context"),
        "decision": state.get("decision"),
        "executed": state.get("executed", False),
        "error": state.get("error"),
        "llm_calls": state.get("llm_calls", 0),
        "feed_posts_seen": len(state.get("feed", [])),
    }
    
    db.collection(MOLTBOOK_STATE).document("agent").set({
        "last_run": run_summary,
        "last_run_at": datetime.now()
    }, merge=True)
    
    return {"completed_at": datetime.now()}