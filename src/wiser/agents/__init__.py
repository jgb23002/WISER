"""Agent modules for the WISER multi-agent analytics system.

Available agents
----------------
DataRetrievalProcessingAgent  – CSV ingestion and database bootstrap
StormWeatherAnalyticsAgent    – SQL-based outage and county analytics
SimilarityQueryAgent          – Multi-dimensional Z-score storm similarity
RAGKnowledgeAgent             – Local RAG index (docs/code/schema) + optional
                                external NOAA/NHC/NCEI web and PDF context with
                                OpenAI or local extractive summarization
SqlAiAgent                    – LLM-powered natural language to PostgreSQL query agent
TaskRoutingAgent              – Priority-ordered keyword/parameter routing
"""

from wiser.agents.data_retrieval_processing_agent import DataRetrievalProcessingAgent
from wiser.agents.rag_knowledge_agent import RAGKnowledgeAgent
from wiser.agents.similarity_query_agent import SimilarityQueryAgent
from wiser.agents.sql_ai_agent import SqlAiAgent, SqlValidationError
from wiser.agents.storm_weather_analytics_agent import StormWeatherAnalyticsAgent
from wiser.agents.task_routing_agent import Route, RouteDecision, TaskRoutingAgent

__all__ = [
    "DataRetrievalProcessingAgent",
    "RAGKnowledgeAgent",
    "SimilarityQueryAgent",
    "SqlAiAgent",
    "SqlValidationError",
    "StormWeatherAnalyticsAgent",
    "Route",
    "RouteDecision",
    "TaskRoutingAgent",
]
