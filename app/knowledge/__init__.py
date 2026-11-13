"""Local, company-scoped document retrieval; documents remain untrusted data."""

from .index import IndexStats, KnowledgeHit, KnowledgeIndex

__all__ = ["IndexStats", "KnowledgeHit", "KnowledgeIndex"]
