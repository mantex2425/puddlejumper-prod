import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_db():
    """
    Connects to self-hosted Postgres on Compute Engine VM.
    Uses Public IP and Environment Variables.
    """
    # Use the VM Public IP as the default
    db_host = os.getenv("DB_HOST", "34.133.145.22") 
    
    # Debugging logs for Cloud Run console
    print(f"DEBUG: Connecting to DB_HOST: {db_host}")
    print(f"DEBUG: Using DB_USER: {os.getenv('DB_USER')}")

    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "puddlejumper"),
        user=os.getenv("DB_USER", "atjb"),
        password=os.environ.get("DB_PASSWORD"), # Secured via Secret Manager
        host=db_host,
        port=os.getenv("DB_PORT", "5432"),
        cursor_factory=RealDictCursor,
        connect_timeout=10,
        sslmode='disable' # Default for simple self-hosted setups; change to 'require' if you set up SSL
    )

def get_db_connection():
    return get_db()