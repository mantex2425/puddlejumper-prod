#!/bin/bash

# 1. Pull fresh chunks from the bucket (and wipe old data)
echo "📥 Step 1: Restoring and chunking from GCS..."
python3 update_knowledge_from_bucket.py

# 2. Run the Big Sarge translation on all @handbook tags
echo "🧠 Step 2: Translating to Big Sarge persona..."
python3 update_product_knowledge.py

echo "✨ Update Complete! Puddles is now street-ready."
