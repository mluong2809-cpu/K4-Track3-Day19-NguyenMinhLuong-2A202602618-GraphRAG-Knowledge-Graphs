"""Suggested name-keyed ontology, run with LAB_SOLUTION_PACKAGE=src_hint.

Retrieval and LLM prompting are reused from the main implementation so the
comparison isolates graph identity and entity normalization choices.
"""

from src.graph import (  # noqa: F401
    GraphRAGAgent, NEWS_EXTRACTION_PROMPT, SUBSTANCES, extract_news_cases,
    find_substances, link_entity, load_markdown_docs, normalize_crime,
    parse_law_article,
)
from src.graph import Neo4jGraph as _MainGraph


class Neo4jGraph(_MainGraph):
    def suggested_constraints(self):
        for label, key in (("Article", "id"), ("Clause", "id"), ("Crime", "name"),
                           ("Case", "name"), ("Substance", "name"),
                           ("Person", "name"), ("Location", "name")):
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_news_case(self, case, doc):
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k)
                SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""),
            location=case.get("location", ""), charges=case.get("charges", []),
            people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )


def build_graph(graph, law_docs, news_docs, llm_fn):
    graph.suggested_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article(article)
    crimes = [article["crime"] for article in articles if article["crime"]]
    for doc in news_docs:
        for case in extract_news_cases(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes):
            graph.add_news_case(case, doc)
