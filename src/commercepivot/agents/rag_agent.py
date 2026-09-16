"""知识库 RAG Agent（架构文档 §3.3）。

职责：商品知识、平台规则的检索问答。核心价值在于**可溯源**：
返回的每条结论都带 ``citations``（文档 id / 标题 / 来源 / 相关性分数 /
检索模式），最终回答里可以标注「依据：xxx」，避免模型凭空编造。

检索本身走 ``search_knowledge`` MCP 工具 → retrieval.service →
BGE-M3 + Milvus hybrid + BGE-Reranker；向量不可用时自动降级为
MySQL FULLTEXT / LIKE，并在 ``notes`` 里如实告知调用方。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from commercepivot.agents.base import BaseAgent


class KnowledgeRagAgent(BaseAgent):
    name = "knowledge_rag_agent"
    description = "知识库检索问答：商品知识、卖点、使用方法、客服 FAQ 与平台规则"
    version = "1.0"
    skills = ["product_knowledge", "platform_rules", "faq_retrieval", "after_sale_policy"]
    tools = ["search_knowledge"]
    port = 8005

    async def handle(self, task_input: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(task_input.get("slots") or {})
        for key in ("top_k", "collection", "min_score"):
            if task_input.get(key) not in (None, ""):
                slots.setdefault(key, task_input.get(key))

        query = str(slots.get("knowledge_query") or task_input.get("question") or "").strip()
        notes: List[str] = []
        degraded = False
        reason: Optional[str] = None

        if not query:
            return {
                "agent": self.name,
                "status": "empty",
                "data": {"rows": [], "summary": {}},
                "findings": ["没有可检索的问题，请补充具体咨询内容。"],
                "metrics": {},
                "citations": [],
                "notes": ["knowledge_query 为空"],
                "degraded": False,
                "degrade_reason": None,
            }

        res = await self.call_tool(
            "search_knowledge",
            {
                "query": query,
                "top_k": slots.get("top_k", 3),
                "collection": slots.get("collection"),
                "use_rerank": True,
                "min_score": slots.get("min_score"),
            },
        )
        hits = res.get("rows") or []
        summary = res.get("summary") or {}
        citations: List[Dict[str, Any]] = []
        for hit in hits:
            citations.append(
                {
                    "id": hit.get("id"),
                    "title": hit.get("title"),
                    "source": hit.get("source"),
                    "collection": hit.get("collection"),
                    "score": hit.get("rerank_score") or hit.get("score"),
                    "retrieval_mode": hit.get("retrieval_mode"),
                }
            )

        findings: List[str] = []
        if hits:
            for hit in hits[:3]:
                snippet = str(hit.get("content") or "").replace("\n", " ")
                if len(snippet) > 160:
                    snippet = snippet[:160] + "…"
                findings.append(f"【{hit.get('title') or hit.get('id')}】{snippet}")
        else:
            findings.append("知识库中没有检索到相关内容，建议补充该类目的知识语料后重建索引。")

        if not res.get("ok"):
            degraded = True
            reason = self.fail_reason(res)
            notes.append(f"检索失败：{reason}")
        elif res.get("degraded") or summary.get("degraded"):
            degraded = True
            reason = res.get("degrade_reason") or summary.get("note")
            notes.append(reason or "检索已降级")

        if summary.get("retrieval_mode"):
            notes.append(f"检索模式：{summary['retrieval_mode']}")

        return {
            "agent": self.name,
            "status": "completed" if hits else "empty",
            "focus": "knowledge",
            "data": {
                "kind": "knowledge",
                "query": query,
                "rows": hits,
                "row_count": len(hits),
                "summary": summary,
                "source_tools": ["search_knowledge"],
            },
            "findings": findings,
            "metrics": {
                "hit_count": len(hits),
                "retrieval_mode": summary.get("retrieval_mode"),
                "top_score": (citations[0]["score"] if citations else None),
            },
            "citations": citations,
            "notes": notes,
            "degraded": degraded,
            "degrade_reason": reason,
        }


AGENT = KnowledgeRagAgent()

__all__ = ["AGENT", "KnowledgeRagAgent"]
