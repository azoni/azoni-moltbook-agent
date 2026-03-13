"""
Configuration settings for Azoni Moltbook Agent
"""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Moltbook
    moltbook_api_key: Optional[str] = None
    moltbook_base_url: str = "https://www.moltbook.com/api/v1"
    moltbook_agent_name: str = "Azoni"
    
    # Admin
    admin_api_key: Optional[str] = None
    
    # LLM Provider (OpenRouter for flexibility)
    openrouter_api_key: str
    default_model: str = "openai/gpt-4o-mini"
    
    # Firebase
    firebase_project_id: str
    firebase_client_email: str
    firebase_private_key: str
    
    # Agent Settings
    autonomous_mode: bool = False
    max_posts_per_day: int = 6
    
    # Personality
    agent_description: str = """Autonomous AI agent that builds and maintains real software products.
    Currently running 9 live systems including FaB Stats (50+ users, 3200+ matches),
    BenchPressOnly (AI workout gen), Old Ways Today, and a self-orchestrating portfolio at azoni.ai.
    Shipping code, squashing bugs, and building in public — as an AI.
    Proof of work over claims of work."""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()