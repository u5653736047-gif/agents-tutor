"""把知识检索服务封装为 Agent 可调用的工具。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator

from .service import KnowledgeService


class _SearchKnowledgeInput(BaseModel):
    """Validate tool inputs before execution so errors are classified correctly."""

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value


def create_search_knowledge_tool(service: KnowledgeService) -> BaseTool:
    """为指定服务创建工具，避免使用全局知识库实例。"""

    @tool("search_knowledge", args_schema=_SearchKnowledgeInput)
    def search_knowledge(query: str, top_k: int = 5) -> dict[str, Any]:
        """检索可引用的知识片段。"""
        hits = service.search(query, top_k)
        if not hits:
            return {
                "found": False,
                "message": "未找到可引用的知识片段",
                "hits": [],
            }
        return {
            "found": True,
            "hits": [
                {
                    "content": hit.chunk.content,
                    "score": hit.score,
                    "citation": hit.citation.model_dump(mode="json"),
                }
                for hit in hits
            ],
        }

    return search_knowledge


__all__ = ["create_search_knowledge_tool"]
