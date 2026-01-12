import os
import firebase_admin
from firebase_admin import credentials

def get_firebase_app():
    # 1. If it's already initialized, just return it
    if firebase_admin._apps:
        return firebase_admin.get_app()

    # 2. Try Local Credentials first (for your VM/Local Dev)
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        return firebase_admin.initialize_app(cred)

    # 3. Use Application Default (for Cloud Run)
    return firebase_admin.initialize_app()

# ADD THIS LINE for compatibility with utils.py:
firebase_app = get_firebase_app()