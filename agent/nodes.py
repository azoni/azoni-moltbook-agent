"""
LangGraph nodes for Azoni Moltbook Agent.

Each node is a step in the agent's workflow.
"""
import json
from datetime import datetime, timedelta
from typing import Dict, Any

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


def get_llm():
    """Get the LLM instance."""
    return ChatOpenAI(
        model=settings.default_model.split("/")[-1],  # Extract model name
        openai_api_key=settings.openrouter_api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://azoni.ai",
            "X-Title": "Azoni Moltbook Agent"
        }
    )


# ==================== Node: Observe ====================

def observe_node(state: AgentState) -> Dict[str, Any]:
    """
    Observe the Moltbook feed and gather context.
    """
    client = get_moltbook_client()
    
    try:
        # Fetch the feed
        feed = client.get_feed(sort="hot", limit=15)
        
        # Also get new posts
        new_posts = client.get_feed(sort="new", limit=10)
        
        # Combine and dedupe
        seen_ids = set()
        combined_feed = []
        for post in feed + new_posts:
            post_id = post.get("id")
            if post_id and post_id not in seen_ids:
                seen_ids.add(post_id)
                combined_feed.append(post)
        
        # Get last activity from Firestore
        db = get_firestore()
        state_doc = db.collection(MOLTBOOK_STATE).document("agent").get()
        last_activity = None
        if state_doc.exists:
            state_data = state_doc.to_dict()
            last_activity = state_data.get("last_activity")
        
        return {
            "feed": combined_feed[:20],  # Keep top 20
            "notifications": [],  # TODO: Add notifications endpoint if available
            "last_activity": last_activity
        }
    
    except Exception as e:
        return {
            "feed": [],
            "notifications": [],
            "error": f"Failed to observe: {str(e)}"
        }


# ==================== Node: Decide ====================

def decide_node(state: AgentState) -> Dict[str, Any]:
    """
    Decide what action to take based on observations.
    """
    if state.get("error"):
        return {"decision": {"action": "nothing", "reason": f"Error in observation: {state['error']}"}}
    
    llm = get_llm()
    
    # Format feed for prompt
    feed_summary = []
    for post in state.get("feed", [])[:10]:
        feed_summary.append(
            f"- [{post.get('submolt', 'general')}] {post.get('title', 'No title')} "
            f"by {post.get('author', 'unknown')} ({post.get('upvotes', 0)} upvotes, "
            f"{post.get('comment_count', 0)} comments)"
        )
    feed_text = "\n".join(feed_summary) if feed_summary else "Feed is empty"
    
    # Get activity stats from Firestore
    db = get_firestore()
    today = datetime.now().date().isoformat()
    
    # Count today's posts
    posts_today = db.collection(MOLTBOOK_ACTIVITY)\
        .where("action", "==", "post")\
        .where("date", "==", today)\
        .limit(10).get()
    posts_today_count = len(list(posts_today))
    
    # Get last post time
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
    
    # Build the prompt
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
    
    # Check if the trigger context is forcing a specific action
    trigger_context = (state.get("trigger_context") or "").lower()
    force_post = any(phrase in trigger_context for phrase in [
        "create a new post", "make a post", "write a post", "post about",
        "introduce yourself", "share something"
    ])
    force_comment = "comment on" in trigger_context or "reply to" in trigger_context
    
    # Parse the decision
    decision: AgentDecision = {
        "action": "nothing",
        "reason": response.content,
        "target_post_id": None,
        "target_submolt": None
    }
    
    # If context is forcing an action, respect that
    if force_post:
        decision["action"] = "post"
    elif force_comment:
        decision["action"] = "comment"
        for post in state.get("feed", []):
            if post.get("title", "").lower() in response_text or post.get("id", "") in response.content:
                decision["target_post_id"] = post.get("id")
                break
    elif "\"post\"" in response_text or "action: post" in response_text or "decide to post" in response_text or "create a post" in response_text:
        decision["action"] = "post"
    elif "\"comment\"" in response_text or "action: comment" in response_text or "decide to comment" in response_text:
        decision["action"] = "comment"
        # Try to extract target post
        for post in state.get("feed", []):
            if post.get("title", "").lower() in response_text or post.get("id", "") in response.content:
                decision["target_post_id"] = post.get("id")
                break
    elif "\"upvote\"" in response_text or "action: upvote" in response_text:
        decision["action"] = "upvote"
        for post in state.get("feed", []):
            if post.get("title", "").lower() in response_text or post.get("id", "") in response.content:
                decision["target_post_id"] = post.get("id")
                break
    
    return {
        "decision": decision,
        "llm_calls": state.get("llm_calls", 0) + 1
    }


# ==================== Node: Draft ====================

def draft_node(state: AgentState) -> Dict[str, Any]:
    """
    Draft content for posting or commenting.
    """
    decision = state.get("decision", {})
    action = decision.get("action")
    
    if action not in ["post", "comment"]:
        return {"draft": None}
    
    llm = get_llm()
    
    if action == "post":
        prompt = DRAFT_POST_PROMPT.format(
            context=state.get("trigger_context") or decision.get("reason", "Share something interesting"),
            identity=AZONI_IDENTITY
        )
    else:  # comment
        # Get the target post details
        target_post_id = decision.get("target_post_id")
        target_post = None
        for post in state.get("feed", []):
            if post.get("id") == target_post_id:
                target_post = post
                break
        
        if not target_post:
            return {"draft": None, "error": "Could not find target post for comment"}
        
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
    
    # Parse the draft
    draft: DraftContent = {
        "content": response.content,
        "title": None,
        "submolt": None
    }
    
    # Try to extract structured fields for posts
    if action == "post":
        lines = response.content.split("\n")
        for line in lines:
            line_lower = line.lower()
            if line_lower.startswith("title:"):
                draft["title"] = line.split(":", 1)[1].strip().strip('"')
            elif line_lower.startswith("content:"):
                draft["content"] = line.split(":", 1)[1].strip()
            elif line_lower.startswith("submolt:"):
                draft["submolt"] = line.split(":", 1)[1].strip().lower()
        
        # Default submolt
        if not draft["submolt"]:
            draft["submolt"] = "general"
    
    return {
        "draft": draft,
        "llm_calls": state.get("llm_calls", 0) + 1
    }


# ==================== Node: Evaluate ====================

def evaluate_node(state: AgentState) -> Dict[str, Any]:
    """
    Evaluate the draft for quality before posting.
    """
    draft = state.get("draft")
    
    if not draft:
        return {"quality_check": {"approved": False, "score": 0, "issues": ["No draft to evaluate"], "suggestions": []}}
    
    # Skip evaluation for simple actions - just approve them
    decision = state.get("decision", {})
    action = decision.get("action")
    
    # For comments, be more lenient - if we have content, approve it
    if action == "comment" and draft.get("content"):
        return {
            "quality_check": {
                "approved": True,
                "score": 0.8,
                "issues": [],
                "suggestions": []
            },
            "llm_calls": state.get("llm_calls", 0)
        }
    
    llm = get_llm()
    
    draft_text = f"Title: {draft.get('title', 'N/A')}\nContent: {draft.get('content', '')}\nSubmolt: {draft.get('submolt', 'N/A')}"
    
    prompt = EVALUATE_PROMPT.format(draft=draft_text)
    
    messages = [
        SystemMessage(content="You are a quality checker for social media posts. Be critical but fair. Respond with JSON: {\"approved\": true/false, \"score\": 0.0-1.0, \"issues\": [], \"suggestions\": []}"),
        HumanMessage(content=prompt)
    ]
    
    response = llm.invoke(messages)
    response_text = response.content.lower()
    
    # Parse the evaluation
    quality_check: QualityCheck = {
        "approved": False,
        "score": 0.5,
        "issues": [],
        "suggestions": []
    }
    
    # Try to parse as JSON first
    import json
    try:
        # Find JSON in response
        import re
        json_match = re.search(r'\{[^}]+\}', response.content)
        if json_match:
            parsed = json.loads(json_match.group())
            quality_check["approved"] = parsed.get("approved", False)
            quality_check["score"] = float(parsed.get("score", 0.5))
            quality_check["issues"] = parsed.get("issues", [])
            quality_check["suggestions"] = parsed.get("suggestions", [])
    except:
        pass
    
    # Fallback: look for approval signals in text
    if not quality_check["approved"]:
        approval_signals = ["approved: true", "approved\":true", "approve this", "looks good", 
                          "i approve", "approved!", "quality is good", "passes", "✓", "yes"]
        if any(signal in response_text for signal in approval_signals):
            quality_check["approved"] = True
    
    # Fallback: Try to extract score
    if quality_check["score"] == 0.5:
        score_match = re.search(r'score["\s:]+([0-9.]+)', response_text)
        if score_match:
            try:
                quality_check["score"] = float(score_match.group(1))
            except:
                pass
    
    # Auto-approve if score is high enough
    if quality_check["score"] >= 0.7:
        quality_check["approved"] = True
    
    # Be lenient for posts too - if content exists and no major red flags
    if not quality_check["approved"] and draft.get("content") and draft.get("title"):
        # Check for obvious issues
        content = draft.get("content", "").lower()
        red_flags = ["error", "undefined", "null", "lorem ipsum", "todo", "fixme"]
        if not any(flag in content for flag in red_flags):
            quality_check["approved"] = True
            quality_check["score"] = 0.75
    
    return {
        "quality_check": quality_check,
        "llm_calls": state.get("llm_calls", 0) + 1
    }


# ==================== Node: Execute ====================

def execute_node(state: AgentState) -> Dict[str, Any]:
    """
    Execute the decided action on Moltbook.
    """
    decision = state.get("decision", {})
    action = decision.get("action")
    draft = state.get("draft")
    quality_check = state.get("quality_check", {})
    
    # Skip if no action or not approved
    if action == "nothing":
        return {"executed": False, "execution_result": {"skipped": True, "reason": "No action decided"}}
    
    if action in ["post", "comment"] and not quality_check.get("approved"):
        return {"executed": False, "execution_result": {"skipped": True, "reason": "Draft not approved"}}
    
    client = get_moltbook_client()
    db = get_firestore()
    
    try:
        result = {}
        
        if action == "post":
            result = client.create_post(
                title=draft.get("title", "Untitled"),
                content=draft.get("content", ""),
                submolt=draft.get("submolt", "general")
            )
        
        elif action == "comment":
            target_post_id = decision.get("target_post_id")
            if target_post_id:
                result = client.create_comment(
                    post_id=target_post_id,
                    content=draft.get("content", "")
                )
        
        elif action == "upvote":
            target_post_id = decision.get("target_post_id")
            if target_post_id:
                result = client.upvote_post(target_post_id)
        
        # Log to Firestore
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
        
        # Update last activity
        db.collection(MOLTBOOK_STATE).document("agent").set({
            "last_activity": datetime.now(),
            "last_action": action
        }, merge=True)
        
        return {
            "executed": True,
            "execution_result": result,
            "completed_at": datetime.now()
        }
    
    except Exception as e:
        # Log error
        db.collection(MOLTBOOK_ACTIVITY).add({
            "action": action,
            "timestamp": datetime.now(),
            "date": datetime.now().date().isoformat(),
            "error": str(e),
            "trigger": state.get("trigger")
        })
        
        return {
            "executed": False,
            "error": str(e),
            "completed_at": datetime.now()
        }


# ==================== Node: Log ====================

def log_node(state: AgentState) -> Dict[str, Any]:
    """
    Final logging node - record the complete run.
    """
    db = get_firestore()
    
    # Create a run summary
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
    
    # Store run log
    db.collection(MOLTBOOK_STATE).document("agent").set({
        "last_run": run_summary,
        "last_run_at": datetime.now()
    }, merge=True)
    
    return {"completed_at": datetime.now()}