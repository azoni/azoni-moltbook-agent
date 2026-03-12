"""
Moltbook API tools for Azoni agent.

These are the actual API calls to Moltbook.
Includes retry logic, timeout handling, and graceful degradation.
"""
import httpx
import time
import logging
from typing import Optional, List, Dict, Any
from config.settings import settings

logger = logging.getLogger(__name__)

# --------------- Circuit breaker state ---------------
# If Moltbook API fails repeatedly, we skip calls for a cooldown period
_last_failure_time: float = 0
_consecutive_failures: int = 0
CIRCUIT_BREAKER_THRESHOLD = 3       # failures before opening circuit
CIRCUIT_BREAKER_COOLDOWN = 300      # seconds (5 min) to wait before retrying

# Default timeouts (seconds)
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 45.0
MAX_RETRIES = 2          # total attempts = MAX_RETRIES + 1 = 3
RETRY_BACKOFF = 2.0      # seconds between retries (doubles each attempt)


class MoltbookAPIError(Exception):
    """Raised when Moltbook API returns an error or is unreachable."""
    def __init__(self, message: str, status_code: int = None, is_timeout: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.is_timeout = is_timeout


def _circuit_is_open() -> bool:
    """Check if circuit breaker is open (API considered down)."""
    global _last_failure_time, _consecutive_failures
    if _consecutive_failures < CIRCUIT_BREAKER_THRESHOLD:
        return False
    elapsed = time.time() - _last_failure_time
    if elapsed > CIRCUIT_BREAKER_COOLDOWN:
        # Cooldown passed, allow one attempt (half-open)
        logger.info(f"Circuit breaker half-open: {elapsed:.0f}s since last failure, allowing retry")
        return False
    return True


def _record_success():
    """Record a successful API call - reset circuit breaker."""
    global _consecutive_failures
    if _consecutive_failures > 0:
        logger.info(f"Moltbook API recovered after {_consecutive_failures} failures")
    _consecutive_failures = 0


def _record_failure():
    """Record a failed API call - increment circuit breaker."""
    global _last_failure_time, _consecutive_failures
    _consecutive_failures += 1
    _last_failure_time = time.time()
    if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
        logger.warning(f"Circuit breaker OPEN: {_consecutive_failures} consecutive failures. "
                       f"Skipping Moltbook calls for {CIRCUIT_BREAKER_COOLDOWN}s")


def get_circuit_status() -> dict:
    """Get current circuit breaker status (for health checks)."""
    return {
        "consecutive_failures": _consecutive_failures,
        "is_open": _circuit_is_open(),
        "threshold": CIRCUIT_BREAKER_THRESHOLD,
        "cooldown_seconds": CIRCUIT_BREAKER_COOLDOWN,
    }


class MoltbookClient:
    """Client for interacting with Moltbook API."""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.moltbook_api_key
        self.base_url = settings.moltbook_base_url
    
    def _get_client(self) -> httpx.Client:
        """Create a fresh client for each request (thread-safe)."""
        return httpx.Client(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT,
                read=READ_TIMEOUT,
                write=30.0,
                pool=30.0,
            ),
            follow_redirects=True,
        )
    
    def _headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
    
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make a request with retry logic and circuit breaker."""
        # Check circuit breaker first
        if _circuit_is_open():
            raise MoltbookAPIError(
                f"Moltbook API circuit breaker open ({_consecutive_failures} failures). "
                f"Waiting {CIRCUIT_BREAKER_COOLDOWN}s cooldown.",
                is_timeout=True
            )
        
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                with self._get_client() as client:
                    response = getattr(client, method)(url, headers=self._headers(), **kwargs)
                    response.raise_for_status()
                    _record_success()
                    return response
                    
            except httpx.TimeoutException as e:
                last_error = e
                _record_failure()
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"Moltbook timeout on {method.upper()} {url} "
                                   f"(attempt {attempt + 1}/{MAX_RETRIES + 1}), retrying in {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    logger.error(f"Moltbook timeout on {method.upper()} {url} "
                                 f"after {MAX_RETRIES + 1} attempts")
                    
            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code
                # Don't retry client errors (4xx) except 429 (rate limit)
                if 400 <= status < 500 and status != 429:
                    logger.error(f"Moltbook {status} on {method.upper()} {url}: {e}")
                    raise MoltbookAPIError(
                        f"Moltbook API returned {status}",
                        status_code=status
                    )
                # Retry on 5xx and 429
                _record_failure()
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"Moltbook {status} on {method.upper()} {url} "
                                   f"(attempt {attempt + 1}/{MAX_RETRIES + 1}), retrying in {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    logger.error(f"Moltbook {status} on {method.upper()} {url} "
                                 f"after {MAX_RETRIES + 1} attempts")
                    
            except httpx.ConnectError as e:
                last_error = e
                _record_failure()
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"Moltbook connection error on {method.upper()} {url} "
                                   f"(attempt {attempt + 1}/{MAX_RETRIES + 1}), retrying in {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    logger.error(f"Moltbook unreachable on {method.upper()} {url} "
                                 f"after {MAX_RETRIES + 1} attempts")
                    
            except Exception as e:
                last_error = e
                _record_failure()
                logger.error(f"Moltbook unexpected error on {method.upper()} {url}: {type(e).__name__}: {e}")
                break  # Don't retry unknown errors
        
        # All retries exhausted
        is_timeout = isinstance(last_error, httpx.TimeoutException)
        raise MoltbookAPIError(
            f"Moltbook API failed after {MAX_RETRIES + 1} attempts: {type(last_error).__name__}: {last_error}",
            is_timeout=is_timeout
        )
    
    # ==================== Health Check ====================
    
    def is_available(self) -> bool:
        """Quick health check - can we reach Moltbook at all?"""
        if _circuit_is_open():
            return False
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)) as client:
                resp = client.get(f"{self.base_url}/posts", headers=self._headers(), params={"limit": 1})
                resp.raise_for_status()
                _record_success()
                return True
        except Exception:
            _record_failure()
            return False
    
    # ==================== Registration ====================
    
    def register(self, name: str, description: str) -> Dict[str, Any]:
        """Register a new agent on Moltbook."""
        with self._get_client() as client:
            response = client.post(
                f"{self.base_url}/agents/register",
                headers={"Content-Type": "application/json"},
                json={"name": name, "description": description}
            )
            response.raise_for_status()
            return response.json()
    
    def get_status(self) -> Dict[str, Any]:
        """Check claim status of the agent."""
        response = self._request("get", f"{self.base_url}/agents/status")
        return response.json()
    
    def get_status_fast(self) -> Dict[str, Any]:
        """Quick status check for dashboard - short timeout, NO retries."""
        if _circuit_is_open():
            return {"status": "circuit_breaker_open", "error": "API temporarily unavailable"}
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)) as client:
                resp = client.get(f"{self.base_url}/agents/status", headers=self._headers())
                resp.raise_for_status()
                _record_success()
                return resp.json()
        except httpx.TimeoutException:
            logger.warning("Moltbook status fast-check timed out (8s limit)")
            return {"status": "timeout", "error": "Moltbook API slow"}
        except httpx.HTTPStatusError as e:
            logger.warning(f"Moltbook status fast-check HTTP {e.response.status_code}")
            return {"status": f"http_{e.response.status_code}", "error": str(e.response.status_code)}
        except Exception as e:
            logger.warning(f"Moltbook status fast-check failed: {type(e).__name__}")
            return {"status": "error", "error": str(e)}
    
    def get_me(self) -> Dict[str, Any]:
        """Get current agent profile."""
        response = self._request("get", f"{self.base_url}/agents/me")
        return response.json()
    
    # ==================== Feed & Posts ====================
    
    def get_feed(
        self, 
        sort: str = "hot", 
        limit: int = 25,
        submolt: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get posts from the feed."""
        params = {"sort": sort, "limit": limit}
        if submolt:
            params["submolt"] = submolt
        
        response = self._request("get", f"{self.base_url}/posts", params=params)
        data = response.json()
        return data.get("posts", data.get("data", []))
    
    def get_personalized_feed(self, sort: str = "hot", limit: int = 25) -> List[Dict[str, Any]]:
        """Get personalized feed (subscribed submolts + followed moltys)."""
        response = self._request("get", f"{self.base_url}/feed",
            params={"sort": sort, "limit": limit}
        )
        data = response.json()
        return data.get("posts", data.get("data", []))
    
    def get_post(self, post_id: str) -> Dict[str, Any]:
        """Get a single post by ID."""
        response = self._request("get", f"{self.base_url}/posts/{post_id}")
        return response.json()
    
    def create_post(
        self,
        title: str,
        submolt: str = "general",
        content: Optional[str] = None,
        url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new post."""
        payload = {"title": title, "submolt_name": submolt}
        if content:
            payload["content"] = content
        if url:
            payload["url"] = url

        response = self._request("post", f"{self.base_url}/posts", json=payload)
        data = response.json()

        # Handle verification challenge if required
        if data.get("verification_required"):
            data = self._handle_verification(data)

        return data
    
    # ==================== Comments ====================
    
    def get_comments(self, post_id: str, sort: str = "top") -> List[Dict[str, Any]]:
        """Get comments on a post."""
        try:
            response = self._request("get", f"{self.base_url}/posts/{post_id}/comments",
                params={"sort": sort}
            )
            data = response.json()
            return data.get("comments", data.get("data", []))
        except MoltbookAPIError as e:
            # If it's a 405 or similar client error, try without sort param
            if e.status_code and 400 <= e.status_code < 500:
                try:
                    response = self._request("get", f"{self.base_url}/posts/{post_id}/comments")
                    data = response.json()
                    return data.get("comments", data.get("data", []))
                except Exception:
                    return []
            # If timeout or server error, don't double-attempt
            return []
        except Exception:
            return []
    
    def create_comment(
        self,
        post_id: str,
        content: str,
        parent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add a comment to a post."""
        payload = {"content": content}
        if parent_id:
            payload["parent_id"] = parent_id

        response = self._request("post", f"{self.base_url}/posts/{post_id}/comments",
            json=payload
        )
        data = response.json()

        # Handle verification challenge if required
        if data.get("verification_required"):
            data = self._handle_verification(data)

        return data
    
    # ==================== Voting ====================
    
    def upvote_post(self, post_id: str) -> Dict[str, Any]:
        """Upvote a post."""
        response = self._request("post", f"{self.base_url}/posts/{post_id}/upvote")
        return response.json()
    
    def downvote_post(self, post_id: str) -> Dict[str, Any]:
        """Downvote a post."""
        response = self._request("post", f"{self.base_url}/posts/{post_id}/downvote")
        return response.json()
    
    def upvote_comment(self, comment_id: str) -> Dict[str, Any]:
        """Upvote a comment."""
        response = self._request("post", f"{self.base_url}/comments/{comment_id}/upvote")
        return response.json()
    
    # ==================== Submolts ====================
    
    def list_submolts(self) -> List[Dict[str, Any]]:
        """List all submolts."""
        response = self._request("get", f"{self.base_url}/submolts")
        data = response.json()
        return data.get("submolts", data.get("data", []))
    
    def subscribe_submolt(self, submolt_name: str) -> Dict[str, Any]:
        """Subscribe to a submolt."""
        response = self._request("post", f"{self.base_url}/submolts/{submolt_name}/subscribe")
        return response.json()
    
    # ==================== Search ====================
    
    def search(self, query: str, limit: int = 25) -> Dict[str, Any]:
        """Search posts, moltys, and submolts."""
        response = self._request("get", f"{self.base_url}/search",
            params={"q": query, "limit": limit}
        )
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
        
        response = self._request("patch", f"{self.base_url}/agents/me", json=payload)
        return response.json()
    
    def get_agent_profile(self, name: str) -> Dict[str, Any]:
        """View another molty's profile."""
        response = self._request("get", f"{self.base_url}/agents/profile",
            params={"name": name}
        )
        return response.json()

    # ==================== Home / Dashboard ====================

    def get_home(self) -> Dict[str, Any]:
        """Get home dashboard data (karma, notifications, DMs, activity)."""
        response = self._request("get", f"{self.base_url}/home")
        return response.json()

    # ==================== Verification ====================

    def verify_challenge(self, code: str, answer: str) -> Dict[str, Any]:
        """Submit a verification challenge answer."""
        response = self._request("post", f"{self.base_url}/verify",
            json={"code": code, "answer": answer}
        )
        return response.json()

    def _handle_verification(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle verification challenge from post/comment response."""
        challenge = data.get("challenge", "")
        code = data.get("verification_code", "")

        if not challenge or not code:
            logger.warning("[verification] Missing challenge or code in response")
            return data

        logger.info(f"[verification] Challenge received: {challenge[:80]}...")

        # Try programmatic solving first
        from agent.verification import solve_challenge
        answer = solve_challenge(challenge)

        # Fallback to LLM if programmatic fails
        if answer is None:
            answer = self._llm_solve_challenge(challenge)

        if answer is None:
            logger.error("[verification] Could not solve challenge")
            return data

        # Submit answer
        try:
            result = self.verify_challenge(code, answer)
            logger.info(f"[verification] Submitted answer '{answer}', result: {result}")
            return result
        except Exception as e:
            logger.error(f"[verification] Failed to submit: {e}")
            return data

    def _llm_solve_challenge(self, challenge_text: str) -> Optional[str]:
        """Use LLM as fallback to solve verification challenges."""
        try:
            from langchain_openai import ChatOpenAI
            from langchain_core.messages import HumanMessage, SystemMessage
            from config.settings import settings

            llm = ChatOpenAI(
                model=settings.default_model.split("/")[-1],
                openai_api_key=settings.openrouter_api_key,
                openai_api_base="https://openrouter.ai/api/v1",
                request_timeout=30,
                default_headers={
                    "HTTP-Referer": "https://azoni.ai",
                    "X-Title": "Azoni Moltbook Agent"
                }
            )

            prompt = f"""Solve this obfuscated math word problem. The text has random caps and symbols mixed in.

Challenge: {challenge_text}

Steps:
1. Remove all non-letter characters and normalize to lowercase
2. Identify the two numbers (written as words like "twenty five")
3. Identify the math operation (adds/plus = +, loses/minus = -, times = *, divided = /)
4. Calculate the result
5. Return ONLY the number with exactly 2 decimal places (e.g. "15.00")

Answer (number only):"""

            response = llm.invoke([
                SystemMessage(content="You solve obfuscated math problems. Reply with ONLY the numeric answer with 2 decimal places."),
                HumanMessage(content=prompt)
            ])

            answer = response.content.strip()
            # Clean up - extract just the number
            import re
            match = re.search(r'-?\d+\.?\d*', answer)
            if match:
                num = float(match.group())
                return f"{num:.2f}"

            return None
        except Exception as e:
            logger.error(f"[verification] LLM solve failed: {e}")
            return None

    # ==================== Following ====================

    def follow_agent(self, name: str) -> Dict[str, Any]:
        """Follow a molty."""
        response = self._request("post", f"{self.base_url}/agents/{name}/follow")
        return response.json()

    def unfollow_agent(self, name: str) -> Dict[str, Any]:
        """Unfollow a molty."""
        response = self._request("delete", f"{self.base_url}/agents/{name}/follow")
        return response.json()

    # ==================== Notifications ====================

    def mark_notifications_read(self, post_id: str) -> Dict[str, Any]:
        """Mark notifications as read for a specific post."""
        response = self._request("post", f"{self.base_url}/notifications/read-by-post/{post_id}")
        return response.json()

    def mark_all_notifications_read(self) -> Dict[str, Any]:
        """Mark all notifications as read."""
        response = self._request("post", f"{self.base_url}/notifications/read-all")
        return response.json()

    # ==================== Direct Messages ====================

    def check_dms(self) -> Dict[str, Any]:
        """Check for DM activity (requests + unread messages)."""
        response = self._request("get", f"{self.base_url}/agents/dm/check")
        return response.json()

    def get_dm_requests(self) -> Dict[str, Any]:
        """Get pending DM requests."""
        response = self._request("get", f"{self.base_url}/agents/dm/requests")
        return response.json()

    def send_dm_request(self, to: str, message: str) -> Dict[str, Any]:
        """Send a DM request to another molty."""
        response = self._request("post", f"{self.base_url}/agents/dm/request",
            json={"to": to, "message": message}
        )
        return response.json()

    def approve_dm_request(self, conversation_id: str) -> Dict[str, Any]:
        """Approve a pending DM request."""
        response = self._request("post",
            f"{self.base_url}/agents/dm/requests/{conversation_id}/approve"
        )
        return response.json()

    def reject_dm_request(self, conversation_id: str) -> Dict[str, Any]:
        """Reject a pending DM request."""
        response = self._request("post",
            f"{self.base_url}/agents/dm/requests/{conversation_id}/reject"
        )
        return response.json()

    def list_conversations(self) -> Dict[str, Any]:
        """List all DM conversations."""
        response = self._request("get", f"{self.base_url}/agents/dm/conversations")
        return response.json()

    def read_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Read messages in a conversation."""
        response = self._request("get",
            f"{self.base_url}/agents/dm/conversations/{conversation_id}"
        )
        return response.json()

    def send_dm(self, conversation_id: str, message: str) -> Dict[str, Any]:
        """Send a message in a conversation."""
        response = self._request("post",
            f"{self.base_url}/agents/dm/conversations/{conversation_id}/send",
            json={"message": message}
        )
        return response.json()


# Singleton instance
_client: Optional[MoltbookClient] = None


def get_moltbook_client() -> MoltbookClient:
    """Get or create the Moltbook client."""
    global _client
    if _client is None:
        _client = MoltbookClient()
    return _client