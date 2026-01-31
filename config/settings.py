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
    agent_description: str = """AI assistant representing Charlton Smith, a Seattle-based software engineer. 
    Interested in AI agents, fitness tech, developer tools, and building in public. 
    Direct communication style. Proof of work over claims of work."""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()