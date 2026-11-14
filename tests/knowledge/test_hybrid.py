from app.knowledge.index import KnowledgeIndex
from tests.knowledge.test_index import source


class ConceptEmbeddings:
    model_id = 'isolated-semantic-fixture'

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.extend(texts)
        return [[1., 0.] if any(term in text for term in ('оплат', 'pay')) else [0., 1.] for text in texts]


async def test_semantic_search_persistence_current_sources_and_company_isolation(tmp_path):
    embeddings = ConceptEmbeddings()
    path = tmp_path/'knowledge.sqlite3'
    index = KnowledgeIndex(path, embedder=embeddings)
    await index.index_sources('one', [source('Срок оплаты 15 дней.')])
    await index.index_sources('two', [source('Срок оплаты 999 дней.', business='two')])
    assert index.search('one', 'When to pay?')[0].text == 'Срок оплаты 15 дней.'
    assert index.search('one', 'When to pay?', allowed_source_ids=[]) == []
    assert index.search('missing', 'When to pay?') == []
    index.close()
    embeddings.calls.clear()
    index = KnowledgeIndex(path, embedder=embeddings)
    await index.index_sources('one', [source('Срок оплаты 15 дней.')])
    assert embeddings.calls == [], 'Persisted vectors are reused without recomputing document embeddings'
    await index.index_sources('one', [])
    assert index.search('one', 'When to pay?') == []
    with index._lock:
        assert index._db.execute("SELECT COUNT(*) FROM rag_vectors WHERE business_id='one'").fetchone()[0] == 0
    index.close()
