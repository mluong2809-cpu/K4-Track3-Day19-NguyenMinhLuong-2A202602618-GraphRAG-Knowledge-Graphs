"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
SUBSTANCE_ALIASES = {"heroin": "Heroine", "ma túy đá": "Methamphetamine",
                     "thuốc lắc": "MDMA", "ecstasy": "MDMA", "ketamin": "Ketamine"}
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    needle = normalize(name)
    if not needle:
        return None
    canonical = {normalize(item): item for item in known if normalize(item)}
    if needle in canonical:
        return canonical[needle]
    matches = difflib.get_close_matches(needle, list(canonical), n=1, cutoff=0.8)
    return canonical[matches[0]] if matches else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    found = {name for name in SUBSTANCES if name.lower() in lowered}
    found.update(canonical for alias, canonical in SUBSTANCE_ALIASES.items() if alias in lowered)
    return sorted(found)

def canonical_substance(name: str) -> str:
    """Use one node for common spellings and street names of a substance."""
    needle = name.strip().lower()
    if needle in SUBSTANCE_ALIASES:
        return SUBSTANCE_ALIASES[needle]
    exact = next((s for s in SUBSTANCES if s.lower() == needle), None)
    if exact:
        return exact
    # Extract a listed chemical from a longer mention such as "ma túy MDMA".
    mentioned = [s for s in SUBSTANCES if re.search(r"(?<!\w)" + re.escape(s.lower()) + r"(?!\w)", needle)]
    return mentioned[0] if len(mentioned) == 1 else name.strip()

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    if not isinstance(cases, list):
        return []
    for case in cases:
        if not isinstance(case, dict):
            continue
        raw_charges = case.get("charges", [])
        if not isinstance(raw_charges, list):
            raw_charges = []
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in raw_charges if isinstance(x, str)) if c})
        case["people"] = [p for p in (case.get("people") or []) if isinstance(p, dict)]
        for person in case["people"]:
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
        case["charges"] = sorted(set(case["charges"]) | {p["charge"] for p in case.get("people", []) if p.get("charge")})
    return [case for case in cases if isinstance(case, dict)]

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "id"),
                           ("Substance", "name"), ("Person", "id"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {id: $id})
              SET k.name = $name, k.summary = $summary, k.date = $date,
                  k.doc_id = $doc_id, k.source_title = $title, k.stage = $stage
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {id: p.id})
                SET person.name = p.name, person.doc_id = $doc_id,
                    person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            id=case["id"],
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=case.get("people", []),
            substances=case.get("substances", []), stage=case.get("stage", "không rõ"),
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        seed_ids, facts = self.seed_facts(question, doc_ids, limit=min(25, max_facts))
        # A substance aggregation question needs every matching news case, not just vector hits.
        aggregation = "những vụ" in question.lower() or "các vụ" in question.lower()
        substances = find_substances(question)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids
               OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
               OR ($aggregation AND EXISTS {
                   MATCH (k)-[:INVOLVES]->(sub:Substance) WHERE sub.name IN $substances })
            RETURN DISTINCT elementId(k) AS id, k.name AS name, k.summary AS summary,
                   k.stage AS stage, k.doc_id AS doc_id
            LIMIT 30
            """, ids=seed_ids, aggregation=aggregation, substances=substances,
        )
        case_ids = [row["id"] for row in cases]
        for row in cases:
            facts.append(f"Vụ việc '{row['name']}' (nguồn {row['doc_id']}, giai đoạn {row['stage']}): {row['summary']}")
        # Traverse Case -> Crime <- Article -> Clause across the two knowledge bases.
        legal = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(crime:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $ids AND cl.number <= 4
            OPTIONAL MATCH (cl)-[:MENTIONS]->(sub:Substance)<-[:INVOLVES]-(k)
            WITH DISTINCT k, crime, a, cl, collect(DISTINCT sub.name) AS matching
            RETURN k.name AS case_name, crime.name AS crime, a.id AS article,
                   a.title AS title, cl.number AS number, cl.penalty AS penalty,
                   CASE WHEN size(matching) > 0 THEN cl.text ELSE '' END AS detail,
                   matching AS substances
            ORDER BY a.id, cl.number
            LIMIT 80
            """, ids=case_ids,
        )
        for row in legal:
            facts.append(f"Vụ '{row['case_name']}' / tội {row['crime']} -> {row['article']} "
                         f"khoản {row['number']}: {row['penalty']}. {row['detail']}")
        # Direct law questions and vector hits on a law document still need clause text.
        article_numbers = re.findall(r"[Đđ]iều\s+(\d+)", question)
        direct = self.run(
            """
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.doc_id IN $docs OR any(n IN $numbers WHERE a.id CONTAINS (' ' + n + ' '))
            RETURN a.id AS article, a.title AS title, cl.number AS number,
                   cl.penalty AS penalty, cl.text AS detail
            ORDER BY a.id, cl.number LIMIT 25
            """, docs=doc_ids, numbers=article_numbers,
        )
        for row in direct:
            facts.append(f"[{row['article']} - {row['title']}] khoản {row['number']}: {row['detail']}")
        return list(dict.fromkeys(facts))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.suggested_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article(article)
    crimes = sorted({article["crime"] for article in articles if article["crime"]})
    for doc in news_docs:
        cases = extract_news_cases(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes)
        for index, case in enumerate(cases, start=1):
            case["id"] = f"{doc.id}#case-{index}"
            case["stage"] = next((stage for word, stage in
                                  (("tuyên", "xét xử"), ("xét xử", "xét xử"),
                                   ("truy tố", "truy tố"), ("bắt", "điều tra"))
                                  if word in (case.get("summary", "") + doc.metadata.get("title", "")).lower()),
                                 "không rõ")
            case["substances"] = [dict(s, name=canonical_substance(s["name"]))
                                  for s in (case.get("substances") or []) if isinstance(s, dict) and s.get("name")]
            people = []
            for p in case.get("people", []):
                if not isinstance(p, dict) or not p.get("name"):
                    continue
                p["id"] = f"{doc.id}#person-{normalize_crime(p['name'])}"
                people.append(p)
            case["people"] = people
            graph.add_news_case(case, doc)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(chunk["metadata"]["doc_id"] for chunk in chunks))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {fact}" for fact in facts),
            chunks="\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, 1)),
            question=question,
        )
        return self.llm_fn(prompt)
