"""
Moltbook API tools for Azoni agent.

These are the actual API calls to Moltbook.
"""
import httpx
from typing import Optional, List, Dict, Any
from config.settings import settings


class MoltbookClient:
    """Client for interacting with Moltbook API."""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.moltbook_api_key
        self.base_url = settings.moltbook_base_url
        self.client = httpx.Client(timeout=30.0)
    
    def _headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    # ==================== Registration ====================
    
    def register(self, name: str, description: str) -> Dict[str, Any]:
        """
        Register a new agent on Moltbook.
        
        Returns:
            {
                "agent": {
                    "api_key": "moltbook_xxx",
                    "claim_url": "https://www.moltbook.com/claim/moltbook_claim_xxx",
                    "verification_code": "reef-X4B2"
                },
                "important": "⚠️ SAVE YOUR API KEY!"
            }
        """
        response = self.client.post(
            f"{self.base_url}/agents/register",
            headers={"Content-Type": "application/json"},
            json={"name": name, "description": description}
        )
        response.raise_for_status()
        return response.json()
    
    def get_status(self) -> Dict[str, Any]:
        """Check claim status of the agent."""
        response = self.client.get(
            f"{self.base_url}/agents/status",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()
    
    def get_me(self) -> Dict[str, Any]:
        """Get current agent profile."""
        response = self.client.get(
            f"{self.base_url}/agents/me",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()
    
    # ==================== Feed & Posts ====================
    
    def get_feed(
        self, 
        sort: str = "hot", 
        limit: int = 25,
        submolt: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get posts from the feed.
        
        Args:
            sort: "hot", "new", "top", "rising"
            limit: Number of posts to fetch
            submolt: Optional submolt to filter by
        """
        params = {"sort": sort, "limit": limit}
        if submolt:
            params["submolt"] = submolt
        
        response = self.client.get(
            f"{self.base_url}/posts",
            headers=self._headers(),
            params=params
        )
        response.raise_for_status()
        data = response.json()
        return data.get("posts", data.get("data", []))
    
    def get_personalized_feed(self, sort: str = "hot", limit: int = 25) -> List[Dict[str, Any]]:
        """Get personalized feed (subscribed submolts + followed moltys)."""
        response = self.client.get(
            f"{self.base_url}/feed",
            headers=self._headers(),
            params={"sort": sort, "limit": limit}
        )
        response.raise_for_status()
        data = response.json()
        return data.get("posts", data.get("data", []))
    
    def get_post(self, post_id: str) -> Dict[str, Any]:
        """Get a single post by ID."""
        response = self.client.get(
            f"{self.base_url}/posts/{post_id}",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()
    
    def create_post(
        self,
        title: str,
        submolt: str = "general",
        content: Optional[str] = None,
        url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a new post.
        
        Args:
            title: Post title
            submolt: Submolt to post in
            content: Text content (for text posts)
            url: URL (for link posts)
        """
        payload = {"title": title, "submolt": submolt}
        if content:
            payload["content"] = content
        if url:
            payload["url"] = url
        
        response = self.client.post(
            f"{self.base_url}/posts",
            headers=self._headers(),
            json=payload
        )
        response.raise_for_status()
        return response.json()
    
    # ==================== Comments ====================
    
    def get_comments(self, post_id: str, sort: str = "top") -> List[Dict[str, Any]]:
        """Get comments on a post."""
        response = self.client.get(
            f"{self.base_url}/posts/{post_id}/comments",
            headers=self._headers(),
            params={"sort": sort}
        )
        response.raise_for_status()
        data = response.json()
        return data.get("comments", data.get("data", []))
    
    def create_comment(
        self,
        post_id: str,
        content: str,
        parent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Add a comment to a post.
        
        Args:
            post_id: ID of the post to comment on
            content: Comment text
            parent_id: Optional parent comment ID for replies
        """
        payload = {"content": content}
        if parent_id:
            payload["parent_id"] = parent_id
        
        response = self.client.post(
            f"{self.base_url}/posts/{post_id}/comments",
            headers=self._headers(),
            json=payload
        )
        response.raise_for_status()
        return response.json()
    
    # ==================== Voting ====================
    
    def upvote_post(self, post_id: str) -> Dict[str, Any]:
        """Upvote a post."""
        response = self.client.post(
            f"{self.base_url}/posts/{post_id}/upvote",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()
    
    def downvote_post(self, post_id: str) -> Dict[str, Any]:
        """Downvote a post."""
        response = self.client.post(
            f"{self.base_url}/posts/{post_id}/downvote",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()
    
    def upvote_comment(self, comment_id: str) -> Dict[str, Any]:
        """Upvote a comment."""
        response = self.client.post(
            f"{self.base_url}/comments/{comment_id}/upvote",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()
    
    # ==================== Submolts ====================
    
    def list_submolts(self) -> List[Dict[str, Any]]:
        """List all submolts."""
        response = self.client.get(
            f"{self.base_url}/submolts",
            headers=self._headers()
        )
        response.raise_for_status()
        data = response.json()
        return data.get("submolts", data.get("data", []))
    
    def subscribe_submolt(self, submolt_name: str) -> Dict[str, Any]:
        """Subscribe to a submolt."""
        response = self.client.post(
            f"{self.base_url}/submolts/{submolt_name}/subscribe",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()
    
    # ==================== Search ====================
    
    def search(self, query: str, limit: int = 25) -> Dict[str, Any]:
        """Search posts, moltys, and submolts."""
        response = self.client.get(
            f"{self.base_url}/search",
            headers=self._headers(),
            params={"q": query, "limit": limit}
        )
        response.raise_for_status()
        return response.json()
    
    # ==================== Profile ====================
    
    def update_profile(
        self,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Update agent profile."""
        payload = {}
        if description:
            payload["description"] = description
        if metadata:
            payload["metadata"] = metadata
        
        response = self.client.patch(
            f"{self.base_url}/agents/me",
            headers=self._headers(),
            json=payload
        )
        response.raise_for_status()
        return response.json()
    
    def get_agent_profile(self, name: str) -> Dict[str, Any]:
        """View another molty's profile."""
        response = self.client.get(
            f"{self.base_url}/agents/profile",
            headers=self._headers(),
            params={"name": name}
        )
        response.raise_for_status()
        return response.json()


# Singleton instance
_client: Optional[MoltbookClient] = None


def get_moltbook_client() -> MoltbookClient:
    """Get or create the Moltbook client."""
    global _client
    if _client is None:
        _client = MoltbookClient()
    return _client
