"""News ingestion pipeline.

Deliberately isolated from src/core so the trading engine stays free of any
news or LLM dependency. Nothing in this package may import from src.core
beyond plain data models, and nothing here may place orders.
"""
