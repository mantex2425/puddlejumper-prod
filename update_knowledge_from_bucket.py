import os
import re
import psycopg
from google.cloud import storage, secretmanager

# --- Configuration ---
PROJECT_ID = "puddle-jumper-477316"
BUCKET_NAME = "puddles-knowledge-base"
PREFIX = "kotlin_src/" 
SECRET_ID = "DB_PASSWORD"
DB_HOST = "10.128.0.3"

def get_secret(secret_id, project_id):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

try:
    DB_PASSWORD = get_secret(SECRET_ID, PROJECT_ID)
    DB_URI = f"postgresql://atjb:{DB_PASSWORD}@{DB_HOST}:5432/puddlejumper"
except Exception as e:
    print(f"❌ Secret error: {e}")
    exit(1)

def chunk_content(filename, text):
    """
    Splits file content into logical chunks.
    Looks for // --- separators or KDoc blocks.
    """
    if not filename.endswith('.kt'):
        return [text]

    # Split by manual separator // --- OR the start of a KDoc /**
    # Uses a lookahead to keep the opening /** in the chunk
    chunks = re.split(r'// ---|(?=/\*\*)', text)
    
    # Filter for chunks that actually contain content and skip package/import headers
    clean_chunks = [c.strip() for c in chunks if c.strip() and not c.startswith('package') and not c.startswith('import')]
    return clean_chunks

def restore_from_bucket():
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blobs = bucket.list_blobs(prefix=PREFIX)

    try:
        with psycopg.connect(DB_URI) as conn:
            with conn.cursor() as cur:
                # WIPE the table before starting to ensure a clean slate
                print("🧹 Wiping old knowledge base...")
                cur.execute("TRUNCATE TABLE knowledge_base")
                
                print(f"📂 Processing source files from gs://{BUCKET_NAME}/{PREFIX}...")
                
                file_count = 0
                row_count = 0
                
                for blob in blobs:
                    if blob.name.endswith(('.kt', '.sql')):
                        raw_text = blob.download_as_text()
                        chunks = chunk_content(blob.name, raw_text)
                        
                        for chunk in chunks:
                            cur.execute(
                                "INSERT INTO knowledge_base (content) VALUES (%s)",
                                (chunk,)
                            )
                            row_count += 1
                            
                        print(f"✅ Processed: {blob.name} ({len(chunks)} chunks)")
                        file_count += 1
                
                conn.commit()
                print(f"\n✨ Success! Processed {file_count} files into {row_count} database rows.")
    except Exception as e:
        print(f"❌ Restore Error: {e}")

if __name__ == "__main__":
    restore_from_bucket()