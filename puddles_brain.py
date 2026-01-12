import os
import psycopg2
from flask import Blueprint, request, jsonify
from vertexai.language_models import TextEmbeddingModel
from vertexai.generative_models import GenerativeModel
import vertexai

puddles_bp = Blueprint('puddles', __name__)

# --- Configuration ---
PROJECT_ID = "puddle-jumper-477316"
LOCATION = "us-central1"
# Your Compute Engine internal IP
DB_HOST = "10.128.0.2" 

# Initialize Vertex AI for the embedding & generative models
vertexai.init(project=PROJECT_ID, location=LOCATION)

@puddles_bp.route('/ask_puddles', methods=['POST'])
def ask_puddles():
    """
    RAG-based endpoint using Postgres (pgvector) for maximum cost-efficiency.
    """
    data = request.get_json()
    question = data.get('question', '')

    if not question:
        return jsonify({"error": "Puddles didn't hear a question!"}), 400

    try:
        # 1. Generate embedding for the question
        # text-embedding-004 is highly efficient and cheap to use.
        embed_model = TextEmbeddingModel.from_pretrained("text-embedding-004")
        question_vector = embed_model.get_embeddings([question])[0].values

        # 2. Vector search via Postgres (Internal VPC connection)
        # The <=> operator performs a cosine similarity search on your 928 entries.
        conn = psycopg2.connect(
            host=DB_HOST,
            database="puddlejumper",
            user="atjb",
            password=os.environ.get("DB_PASSWORD") # Password pulled from Cloud Run Secrets
        )
        
        with conn.cursor() as cur:
            cur.execute("""
                SELECT content 
                FROM knowledge_base 
                ORDER BY embedding <=> %s::vector 
                LIMIT 3
            """, (question_vector,))
            results = cur.fetchall()
        conn.close()

        # Combine matching chunks into context
        context = "\n\n".join([row[0] for row in results])

        # 3. Generate answer with Gemini 2.0 Flash
        # Flash is ideal for this because it's fast and has a low cost-per-token.
        gemini = GenerativeModel("gemini-2.0-flash")
        
        prompt = f"""You are Puddles, a friendly mentor for rideshare drivers. 
        Your goal is to help them understand their earnings and the app simply.

        STRICT RULES:
        - NEVER mention technical jargon (SQL, database, vectors, code).
        - Answer in 1-2 sentences maximum.
        - Speak like a supportive peer.

        CONTEXT:
        {context}

        DRIVER QUESTION: 
        {question}"""

        result = gemini.generate_content(prompt)
        
        return jsonify({"answer": result.text.strip()})

    except Exception as e:
        # Log the error to Cloud Run logs for debugging
        print(f"PUDDLES ERROR: {str(e)}")
        return jsonify({"error": "Puddles is having a bit of trouble thinking. Try again!"}), 500