"""Answer questions from the user's own documents, with citations."""

from __future__ import annotations

from typing import List, Tuple

from .config import Config
from .llm import LLM
from .retrieval import Hit, Retriever

SYSTEM = (
    "You are Echo, answering questions about the user's personal document "
    "library. Use ONLY the excerpts provided. Cite each factual claim with the "
    "matching excerpt number in square brackets, like [2]. If the excerpts do "
    "not contain the answer, say so plainly and mention what is missing. "
    "Be concise and concrete. Do not invent details, links or dates."
)


def ask_from_docs(
    cfg: Config,
    retriever: Retriever,
    llm: LLM,
    question: str,
    limit: int = 6,
) -> Tuple[str, List[Hit]]:
    hits = retriever.search(question, limit=limit)
    if not hits:
        return (
            "I couldn't find anything relevant in your documents. Add a document "
            "with `echo-ai new` or point me at a broader question.",
            [],
        )
    blocks = []
    for index, hit in enumerate(hits, start=1):
        blocks.append(
            "[%d] %s (doc: %s)\n%s" % (index, hit.title, hit.doc_id, hit.text.strip())
        )
    user = "Question: %s\n\nDocument excerpts:\n\n%s" % (question, "\n\n".join(blocks))
    response = llm.chat(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ]
    )
    answer = (response.get("content") or "").strip()
    if not answer:
        answer = "The model returned an empty answer. Try rephrasing the question."
    return answer, hits
