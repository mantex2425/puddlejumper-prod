"""
Knowledge and information retrieval tools for Puddles Brain.
Handles internal product knowledge and web search fallback.
"""
import os
import logging
import requests
from langchain_core.tools import tool

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

# Lazy-load embeddings model to avoid requiring API key at import time
_embeddings_model = None

def get_embeddings_model():
    """Lazy-load the embeddings model only when needed."""
    global _embeddings_model
    if _embeddings_model is None:
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        _embeddings_model = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    return _embeddings_model


@tool
def check_internal_knowledge(query: str):
    """
    Search PuddleJumper's internal knowledge base for product info and rules.
    
    Use FIRST when driver asks about:
    - App features: "What are hex codes?", "How does auto-accept work?"
    - PuddleJumper rules: "What's a red zone?", "How do I earn more?"
    - Technical procedures: "How do I report a bug?", "Where are my stats?"
    
    Do NOT use for:
    - Navigation questions (use navigation tools)
    - Finding places (use find_nearby_places)
    - General knowledge (use web_search)
    
    Returns: Specific answer from PuddleJumper documentation.
    """
    try:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
        
        # Import DB connection from parent scope
        DB_HOST = os.environ.get("DB_HOST", "10.128.0.3")
        DB_PASSWORD = os.environ.get("DB_PASSWORD")
        DB_URI = f"postgresql://atjb:{DB_PASSWORD}@{DB_HOST}:5432/puddlejumper"
        
        pool = ConnectionPool(conninfo=DB_URI, max_size=10, open=True, kwargs={"row_factory": dict_row})
        
        embeddings_model = get_embeddings_model()
        query_vector = embeddings_model.embed_query(query)
        
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT content FROM knowledge_base 
                    ORDER BY embedding <=> %s::vector LIMIT 1
                """, (query_vector,))
                row = cur.fetchone()
                return row["content"] if row else "No specific internal rule found."
    except Exception as e:
        logging.error(f"Knowledge base error: {e}", exc_info=True)
        return f"Knowledge base temporarily unavailable: {e}"


@tool
def web_search(query: str):
    """
    Search the web for general information, news, or weather.
    
    Use as fallback when:
    - Driver asks about current events: "What's the news?", "Weather today?"
    - General knowledge not in internal docs: "Who won the game?", "Stock price?"
    
    Do NOT use for:
    - PuddleJumper app questions (use check_internal_knowledge)
    - Navigation or places (use specific tools)
    
    Returns: Summary of web search results.
    """
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_API_KEY, "query": query},
            timeout=6
        ).json()
        
        results = res.get('results', [])
        if not results:
            return "No web results found."
        
        # Combine top 3 results
        summary = " ".join([r.get('content', '')[:300] for r in results[:3]])
        return summary if summary else "Search offline."
        
    except Exception as e:
        logging.error(f"Web search error: {e}", exc_info=True)
        return "Search offline."