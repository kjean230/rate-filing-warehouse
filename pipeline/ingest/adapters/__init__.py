"""Source adapters. One per state, both emitting DocumentRef."""

from pipeline.ingest.adapters.base import DocumentRef, SourceAdapter, build_adapter

__all__ = ["DocumentRef", "SourceAdapter", "build_adapter"]
