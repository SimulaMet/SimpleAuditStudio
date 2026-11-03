"""
title: Studio RAG Overrides
author: SimpleAudit Studio
version: 0.3.0
description: >-
  Per-model retrieval for Studio agents. Reads meta.simpleaudit.retrieval
  (search mode, top K, final K, relevance threshold, BM25 weight, full context)
  from the model and runs the file retrieval itself. Models without those
  settings use the global RAG settings, so they match Open WebUI's built-in RAG.
  Installed and kept global by chat/sync.py (ensure_rag_overrides_function).
"""

from pydantic import BaseModel


class Filter:
    def __init__(self):
        # Official: set on the instance. Open WebUI then skips its built-in retrieval
        # for requests this filter runs on, so this filter performs the retrieval.
        self.file_handler = True
        self.valves = self.Valves()

    class Valves(BaseModel):
        pass

    @staticmethod
    def _last_user_text(messages: list) -> str:
        for msg in reversed(messages or []):
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, list):
                    content = ' '.join(p.get('text', '') for p in content if p.get('type') == 'text')
                return content or ''
        return ''

    async def inlet(
        self,
        body: dict,
        __request__=None,
        __model__: dict | None = None,
        __user__: dict | None = None,
    ) -> dict:
        from open_webui.main import app as owui_app
        from open_webui.models.config import Config
        from open_webui.models.users import Users
        from open_webui.retrieval.utils import get_sources_from_items

        metadata = body.get('metadata') or {}
        files = [
            item for item in (metadata.get('files') or body.get('files') or [])
            if item.get('type') != 'filesystem'
        ]
        if not files or not __user__:
            return body

        query = self._last_user_text(body.get('messages'))
        if not query:
            return body

        # Global defaults (the same values the built-in RAG reads).
        g = await Config.get_many(
            'rag.top_k',
            'rag.top_k_reranker',
            'rag.relevance_threshold',
            'rag.hybrid_bm25_weight',
            'rag.full_context',
        )
        # Per-model settings saved by Studio (meta.simpleaudit.retrieval). Unset values
        # fall back to the global defaults.
        retrieval = (((__model__ or {}).get('info') or {}).get('meta') or {}).get('simpleaudit', {}).get('retrieval') or {}
        hybrid_search = (retrieval.get('search_mode') or 'semantic') == 'hybrid'
        bm25_weight = retrieval.get('bm25_weight')
        if bm25_weight is None:
            bm25_weight = g.get('rag.hybrid_bm25_weight')
        k = retrieval.get('top_k') or g.get('rag.top_k')
        k_reranker = retrieval.get('final_k') or g.get('rag.top_k_reranker')
        r = retrieval.get('relevance_threshold')
        if r is None:
            r = g.get('rag.relevance_threshold')
        full_context = retrieval.get('full_context', g.get('rag.full_context'))

        app_state = owui_app.state
        user = await Users.get_user_by_id(__user__['id'])
        reranker = app_state.RERANKING_FUNCTION

        sources = await get_sources_from_items(
            request=__request__,
            items=files,
            queries=[query],
            embedding_function=lambda q, prefix: app_state.EMBEDDING_FUNCTION(q, prefix=prefix, user=user),
            k=k,
            reranking_function=(lambda q, docs: reranker(q, docs, user=user)) if reranker else None,
            k_reranker=k_reranker,
            r=r,
            hybrid_bm25_weight=bm25_weight,
            hybrid_search=hybrid_search,
            full_context=full_context,
            user=user,
        )

        blocks = []
        idx = 0
        for src in sources or []:
            source = src.get('source') or {}
            for doc in src.get('document') or []:
                idx += 1
                name = source.get('name') or source.get('id') or 'source'
                resource_id = source.get('id') or ''
                blocks.append(
                    f'<source id="{idx}" name="{name}" resource-id="{resource_id}" resource-type="collection">\n'
                    f'{doc}\n</source>'
                )

        if blocks:
            context = 'Use the following context to answer. Cite sources by their id.\n\n' + '\n\n'.join(blocks)
            messages = body.get('messages') or []
            last_user = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=None)
            if last_user is not None:
                messages.insert(last_user, {'role': 'system', 'content': context})
            body['messages'] = messages

        metadata['sources'] = sources or []
        body['metadata'] = metadata
        return body
