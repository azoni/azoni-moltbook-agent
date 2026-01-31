"""
State schema for Azoni Moltbook Agent

The state flows through the graph and gets updated at each node.
"""
from typing import TypedDict, Literal, Optional, List, Any
from datetime import datetime


class MoltbookPost(TypedDict):
    """A post from Moltbook feed."""
    id: str
    title: str
    content: Optional[str]
    url: Optional[str]
    author: str
    submolt: str
    upvotes: int
    comment_count: int
    created_at: str


class MoltbookComment(TypedDict):
    """A comment from Moltbook."""
    id: str
    post_id: str
    content: str
    author: str
    upvotes: int
    created_at: str


class AgentDecision(TypedDict):
    """The decision made by the agent."""
    action: Literal["post", "comment", "upvote", "nothing"]
    reason: str
    target_post_id: Optional[str]  # For comment/upvote
    target_submolt: Optional[str]  # For posting


class DraftContent(TypedDict):
    """Draft content to be posted or commented."""
    content: str
    title: Optional[str]  # Only for posts
    submolt: Optional[str]  # Only for posts


class QualityCheck(TypedDict):
    """Result of quality evaluation."""
    approved: bool
    score: float  # 0-1
    issues: List[str]
    suggestions: List[str]


class AgentState(TypedDict):
    """
    The main state object that flows through the LangGraph.
    
    Each node reads from and writes to this state.
    """
    # Trigger info
    trigger: Literal["heartbeat", "manual", "command"]
    trigger_context: Optional[str]  # e.g., "post about X" for manual triggers
    
    # Observation phase
    feed: List[MoltbookPost]
    notifications: List[Any]
    last_activity: Optional[datetime]
    
    # Decision phase
    decision: Optional[AgentDecision]
    
    # Draft phase  
    draft: Optional[DraftContent]
    
    # Evaluation phase
    quality_check: Optional[QualityCheck]
    
    # Execution phase
    executed: bool
    execution_result: Optional[dict]
    error: Optional[str]
    
    # Metadata
    started_at: datetime
    completed_at: Optional[datetime]
    llm_calls: int
    tokens_used: int
