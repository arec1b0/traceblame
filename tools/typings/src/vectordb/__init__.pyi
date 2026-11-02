# source: arec1b0/agentic-rag at 0e3780d: src/vectordb/document_processor.py and chroma_store.py,
#   only what tools/fixtures/agentic_rag/run.py uses.
from typing import Any

class Document:
    content: str
    metadata: dict[str, Any]
    doc_id: str | None
    def __init__(
        self, content: str, metadata: dict[str, Any], doc_id: str | None = ...
    ) -> None: ...

class DocumentProcessor:
    def __init__(
        self, chunk_size: int = ..., chunk_overlap: int = ..., min_chunk_size: int = ...
    ) -> None: ...
    def chunk_document(self, document: Document) -> list[Document]: ...

class ChromaStore:
    def __init__(
        self,
        collection_name: str | None = ...,
        persist_directory: str | None = ...,
        embedding_model: str | None = ...,
    ) -> None: ...
    def add_documents(self, documents: list[Document]) -> list[str]: ...
