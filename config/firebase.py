"""
Firebase/Firestore connection for Azoni Moltbook Agent
"""
import firebase_admin
from firebase_admin import credentials, firestore
from config.settings import settings

_db = None


def get_firestore():
    """Get Firestore client, initializing if needed."""
    global _db
    
    if _db is not None:
        return _db
    
    if not firebase_admin._apps:
        cred = credentials.Certificate({
            "type": "service_account",
            "project_id": settings.firebase_project_id,
            "client_email": settings.firebase_client_email,
            "private_key": settings.firebase_private_key.replace("\\n", "\n"),
        })
        firebase_admin.initialize_app(cred)
    
    _db = firestore.client()
    return _db


# Collection names
MOLTBOOK_CONFIG = "moltbook_config"
MOLTBOOK_ACTIVITY = "moltbook_activity"
MOLTBOOK_STATE = "moltbook_state"
