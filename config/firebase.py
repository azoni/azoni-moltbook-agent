"""
Firebase/Firestore connection for Azoni Moltbook Agent
"""
import json
import os
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
        # Option 1: Full JSON credentials (preferred)
        creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
        if creds_json:
            try:
                cred_dict = json.loads(creds_json)
                cred = credentials.Certificate(cred_dict)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid FIREBASE_CREDENTIALS_JSON: {e}")
        else:
            # Option 2: Individual env vars
            private_key = settings.firebase_private_key
            if private_key:
                # Handle different formats of the private key
                private_key = private_key.replace("\\n", "\n")
                # Remove surrounding quotes if present
                if private_key.startswith('"') and private_key.endswith('"'):
                    private_key = private_key[1:-1]
                if private_key.startswith("'") and private_key.endswith("'"):
                    private_key = private_key[1:-1]
            
            cred = credentials.Certificate({
                "type": "service_account",
                "project_id": settings.firebase_project_id,
                "private_key_id": os.environ.get("FIREBASE_PRIVATE_KEY_ID", ""),
                "private_key": private_key,
                "client_email": settings.firebase_client_email,
                "client_id": os.environ.get("FIREBASE_CLIENT_ID", ""),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_x509_cert_url": f"https://www.googleapis.com/robot/v1/metadata/x509/{settings.firebase_client_email.replace('@', '%40')}" if settings.firebase_client_email else ""
            })
        
        firebase_admin.initialize_app(cred)
    
    _db = firestore.client()
    return _db


# Collection names
MOLTBOOK_CONFIG = "moltbook_config"
MOLTBOOK_ACTIVITY = "moltbook_activity"
MOLTBOOK_STATE = "moltbook_state"
MOLTBOOK_JOB_HISTORY = "moltbook_job_history"