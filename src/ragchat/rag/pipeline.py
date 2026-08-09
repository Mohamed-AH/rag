"""The retrieval-augmented generation chain, built with LCEL.

Replaces the deprecated ``RetrievalQA`` helper with an explicit LangChain Expression
Language pipeline:

    question
      -> retrieve relevant documents (pgvector similarity search)
      -> format them into a context block
      -> grounded prompt
      -> Gemini
      -> string answer

The chain is provider-agnostic: it takes any ``Retriever`` and any chat model, which is
what lets the test suite drive it with fakes and no API keys.
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableParallel, RunnablePassthrough

_SYSTEM_PROMPT = (
    "You are a precise assistant answering questions from a knowledge base. "
    "Use ONLY the provided context to answer. If the context does not contain the "
    "answer, say you don't have enough information — do not invent facts. "
    "Be concise and cite concepts from the context."
)

_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", _SYSTEM_PROMPT),
        ("human", "Question: {question}\n\nContext:\n{context}"),
    ]
)


def format_docs(docs: list[Document]) -> str:
    """Join retrieved documents into a single context string."""
    return "\n\n".join(doc.page_content for doc in docs)


def build_rag_chain(retriever: BaseRetriever, llm: BaseChatModel) -> Runnable[str, dict[str, Any]]:
    """Build the RAG chain.

    Input: a question string.
    Output: ``{"question": str, "documents": list[Document], "answer": str}``.
    """
    answer_chain = (
        RunnablePassthrough.assign(context=lambda x: format_docs(x["documents"]))
        | _PROMPT
        | llm
        | StrOutputParser()
    )

    return RunnableParallel(
        question=RunnablePassthrough(),
        documents=retriever,
    ) | RunnablePassthrough.assign(answer=answer_chain)
