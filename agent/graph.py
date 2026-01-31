"""
LangGraph workflow definition for Azoni Moltbook Agent.

This defines the graph structure - how nodes connect and flow.
"""
from datetime import datetime
from typing import Literal

from langgraph.graph import StateGraph, END

from agent.state import AgentState
from agent.nodes import (
    observe_node,
    decide_node,
    draft_node,
    evaluate_node,
    execute_node,
    log_node
)


def should_draft(state: AgentState) -> Literal["draft", "execute"]:
    """
    Conditional edge: Decide if we need to draft content.
    """
    decision = state.get("decision", {})
    action = decision.get("action", "nothing")
    
    if action in ["post", "comment"]:
        return "draft"
    else:
        # Skip draft and evaluate for upvote/nothing
        return "execute"


def should_execute(state: AgentState) -> Literal["execute", "log"]:
    """
    Conditional edge: Decide if we should execute based on evaluation.
    """
    quality_check = state.get("quality_check", {})
    decision = state.get("decision", {})
    action = decision.get("action", "nothing")
    
    # Always execute for upvote/nothing (nothing just logs)
    if action not in ["post", "comment"]:
        return "execute"
    
    # For post/comment, check if approved
    if quality_check.get("approved", False):
        return "execute"
    else:
        # Skip execution, go straight to log
        return "log"


def build_agent_graph() -> StateGraph:
    """
    Build and return the LangGraph for the Moltbook agent.
    
    Flow:
    START -> observe -> decide -> [draft if needed] -> evaluate -> execute -> log -> END
    """
    # Create the graph with our state schema
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("observe", observe_node)
    workflow.add_node("decide", decide_node)
    workflow.add_node("draft", draft_node)
    workflow.add_node("evaluate", evaluate_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("log", log_node)
    
    # Set entry point
    workflow.set_entry_point("observe")
    
    # Add edges
    workflow.add_edge("observe", "decide")
    
    # Conditional: decide -> draft or execute
    workflow.add_conditional_edges(
        "decide",
        should_draft,
        {
            "draft": "draft",
            "execute": "execute"
        }
    )
    
    # draft -> evaluate
    workflow.add_edge("draft", "evaluate")
    
    # Conditional: evaluate -> execute or log
    workflow.add_conditional_edges(
        "evaluate",
        should_execute,
        {
            "execute": "execute",
            "log": "log"
        }
    )
    
    # execute -> log
    workflow.add_edge("execute", "log")
    
    # log -> END
    workflow.add_edge("log", END)
    
    return workflow


def create_agent():
    """
    Create and compile the agent graph.
    """
    workflow = build_agent_graph()
    return workflow.compile()


def run_agent(
    trigger: Literal["heartbeat", "manual", "command"] = "manual",
    trigger_context: str = None
) -> AgentState:
    """
    Run the agent with the given trigger.
    
    Args:
        trigger: What triggered this run
        trigger_context: Optional context (e.g., "post about X")
    
    Returns:
        Final state after execution
    """
    agent = create_agent()
    
    initial_state: AgentState = {
        "trigger": trigger,
        "trigger_context": trigger_context,
        "feed": [],
        "notifications": [],
        "last_activity": None,
        "decision": None,
        "draft": None,
        "quality_check": None,
        "executed": False,
        "execution_result": None,
        "error": None,
        "started_at": datetime.now(),
        "completed_at": None,
        "llm_calls": 0,
        "tokens_used": 0
    }
    
    # Run the graph
    final_state = agent.invoke(initial_state)
    
    return final_state


# Quick test function
if __name__ == "__main__":
    print("Testing Azoni Moltbook Agent graph...")
    
    # This will only work with proper env vars set
    try:
        result = run_agent(trigger="manual", trigger_context="Test run - just observe, don't post")
        print(f"Agent run completed!")
        print(f"Decision: {result.get('decision')}")
        print(f"Executed: {result.get('executed')}")
        print(f"LLM calls: {result.get('llm_calls')}")
    except Exception as e:
        print(f"Error: {e}")
