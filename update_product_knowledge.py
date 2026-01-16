import os
import re
import psycopg
from psycopg.rows import dict_row
from google.cloud import secretmanager
from langchain_google_vertexai import ChatVertexAI, VertexAIEmbeddings
from langchain_core.messages import SystemMessage, HumanMessage

# --- Configuration ---
PROJECT_ID = "puddle-jumper-477316"
LOCATION = "us-central1"
DB_HOST = "10.128.0.2"
SECRET_ID = "DB_PASSWORD"

def get_secret(secret_id, project_id):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

try:
    REAL_DB_PASSWORD = get_secret(SECRET_ID, PROJECT_ID)
    DB_URI = f"postgresql://atjb:{REAL_DB_PASSWORD}@{DB_HOST}:5432/puddlejumper"
except Exception as e:
    print(f"❌ Failed to retrieve secret: {e}")
    exit(1)

# Models
llm = ChatVertexAI(model_name="gemini-2.0-flash-001", project=PROJECT_ID, location=LOCATION)
embeddings_model = VertexAIEmbeddings(model_name="text-embedding-004")

# Updated Big Sarge Persona: Professional Peer
TRANSLATION_PROMPT = """You are 'Big Sarge,' a veteran rideshare pro with 10,000+ rides. 
You're talking to a fellow driver who knows the road but is just getting used to this new app. 
Keep it respectful, peer-to-peer, and straight to the point.

STRICT RULES:
1. NEVER use the words "rookie," "kid," "listen up," or "newbie." 
2. LOOK FOR '@handbook' tags. If you see one, that is your ground truth—explain the steps clearly.
3. If no '@handbook' tag exists, explain how the feature helps a driver maximize their efficiency or earnings.
4. NEVER say "This code...", "This function...", or "The app ensures...".
5. NEVER mention "data," "systems," "JSON," or "information."
6. Keep it to 1-2 punchy sentences. High school reading level.
7. Format: A single paragraph of spoken advice.

LOGIC TO TRANSLATE:
"""

def targeted_refresh_handbook():
    try:
        with psycopg.connect(DB_URI, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                print("🎯 Big Sarge is scanning for @handbook tags...")
                # We pull any row that has the tag, even if it's clumped
                cur.execute("SELECT id, content FROM knowledge_base WHERE content LIKE '%@handbook%'")
                rows = cur.fetchall()

                if not rows:
                    print("🤷 No handbook entries found.")
                    return

                for row in rows:
                    row_id = row['id']
                    raw_content = row['content']
                    
                    # INTELLIGENT SPLITTING LOGIC
                    # We split by your separator // --- OR by the start of a new KDoc block /**
                    chunks = re.split(r'// ---|(?=/\*\*)', raw_content)
                    
                    # Filter for only the chunks that actually have instructions
                    valid_chunks = [c.strip() for c in chunks if "@handbook" in c]

                    if len(valid_chunks) > 1:
                        print(f"🧩 Row {row_id} is clumped ({len(valid_chunks)} parts). Splitting now...")
                        # 1. Delete the giant original clump to prevent duplicates
                        cur.execute("DELETE FROM knowledge_base WHERE id = %s", (row_id,))
                        
                        # 2. Process each chunk as a fresh, separate entry
                        for chunk in valid_chunks:
                            process_and_insert_chunk(cur, chunk)
                    else:
                        # Standard single-entry processing
                        print(f"✍️ Translating handbook entry {row_id}...")
                        process_and_update_single_row(cur, row_id, raw_content)
                    
                conn.commit()
                print("✅ All entries split and translated. Puddles is now street-ready!")

    except Exception as e:
        print(f"❌ Error during intelligent pass: {e}")

def process_and_insert_chunk(cur, text):
    """Translates a new sub-chunk and inserts it as a fresh row."""
    translated = translate_text(text)
    vector = embeddings_model.embed_query(translated)
    cur.execute(
        "INSERT INTO knowledge_base (content, embedding) VALUES (%s, %s::vector)",
        (translated, vector)
    )

def process_and_update_single_row(cur, row_id, text):
    """Updates an existing row."""
    translated = translate_text(text)
    vector = embeddings_model.embed_query(translated)
    cur.execute(
        "UPDATE knowledge_base SET content = %s, embedding = %s::vector WHERE id = %s",
        (translated, vector, row_id)
    )

def translate_text(text):
    """Calls Gemini with your Big Sarge persona."""
    response = llm.invoke([
        SystemMessage(content=TRANSLATION_PROMPT),
        HumanMessage(content=f"LOGIC:\n{text}")
    ])
    return response.content.strip()

if __name__ == "__main__":
    targeted_refresh_handbook()