import os
import psycopg2
from google.cloud import storage
from vertexai.language_models import TextEmbeddingModel
import vertexai

# Configuration
PROJECT_ID = "puddle-jumper-477316"
BUCKET_NAME = "puddles-knowledge-base"
DB_PARAMS = {
    "host": "localhost", # Your internal IP
    "database": "puddlejumper",
    "user": "postgres",
    "password": "ATJBueue2425" # MAKE SURE TO CHANGE THIS
}

vertexai.init(project=PROJECT_ID, location="us-central1")
model = TextEmbeddingModel.from_pretrained("text-embedding-004")

def migrate_to_postgres():
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blobs = bucket.list_blobs() # Adjusted to list all blobs

    print(f"Starting migration from {BUCKET_NAME}...")

    for blob in blobs:
        if blob.name.endswith(".txt"):
            content = blob.download_as_text()
            embedding = model.get_embeddings([content])[0].values
            
            cur.execute(
                "INSERT INTO knowledge_base (content, embedding) VALUES (%s, %s)",
                (content, embedding)
            )
            print(f"Migrated: {blob.name}")

    conn.commit()
    cur.close()
    conn.close()
    print("Migration complete!")

if __name__ == "__main__":
    migrate_to_postgres()
