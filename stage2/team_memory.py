"""Team memory bridge (2026-11-16). Inject graph facts into agent prompts."""
from __future__ import annotations
import os

class TeamMemoryBridge:
    def __init__(self, uri="bolt://127.0.0.1:7687", user="neo4j", password="graphiti", max_facts=5):
        self.uri = uri
        self.auth = (user, password)
        self.max_facts = max_facts
        self._driver = None

    def _get_driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(self.uri, auth=self.auth)
        return self._driver

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def search(self, query, max_facts=0):
        drv = self._get_driver()
        limit = max_facts or self.max_facts
        cypher = "MATCH (e:Episodic) WHERE e.summary CONTAINS $q1 OR e.name CONTAINS $q1 RETURN e.name AS name, e.summary AS summary, e.created_at AS created_at ORDER BY e.created_at DESC LIMIT $limit"
        facts = []
        try:
            with drv.session() as session:
                result = session.run(cypher, {"q1": query, "limit": limit})
                for record in result:
                    facts.append(dict(record))
                if len(facts) < limit:
                    words = query.split()
                    for w in words:
                        if len(w) < 3 or len(facts) >= limit:
                            continue
                        result = session.run(cypher, {"q1": w, "limit": limit - len(facts)})
                        for record in result:
                            d = dict(record)
                            if d not in facts:
                                facts.append(d)
        except Exception as e:
            facts.append({"error": str(e)[:200]})
        return facts

    def search_entities(self, query):
        drv = self._get_driver()
        entities = []
        try:
            with drv.session() as session:
                cypher = "MATCH (n:Entity) WHERE n.name CONTAINS $q RETURN n.name AS name, n.summary AS summary LIMIT $limit"
                result = session.run(cypher, {"q": query, "limit": self.max_facts})
                for record in result:
                    entities.append(dict(record))
                # 拆词搜索补全
                if len(entities) < self.max_facts:
                    for w in query.split():
                        if len(w) < 3 or len(entities) >= self.max_facts:
                            continue
                        result = session.run(cypher, {"q": w, "limit": self.max_facts - len(entities)})
                        for record in result:
                            d = dict(record)
                            if d not in entities:
                                entities.append(d)
        except Exception as e:
            entities.append({"error": str(e)[:200]})
        return entities

    def inject_context(self, task_desc, agent_id=""):
        facts = self.search(task_desc)
        entities = self.search_entities(task_desc)
        relevant = []
        for f in facts:
            s = f.get("summary", "")
            if s and s not in relevant:
                relevant.append(s[:200])
        for e in entities:
            s = e.get("summary", "")
            if s and s not in relevant:
                relevant.append(s[:200])
        if not relevant:
            return task_desc
        memory_block = "[团队记忆 - 自动注入]\n"
        for r in relevant[:self.max_facts]:
            memory_block += "- %s\n" % r
        memory_block += "[团队记忆结束]\n\n"
        return memory_block + task_desc

    def record_outcome(self, agent_id, task_desc, success, verdict):
        drv = self._get_driver()
        import time as _t
        summary = "%s executed '%s' result: %s (verdict: %s)" % (agent_id, task_desc[:100], "success" if success else "fail", verdict)
        try:
            with drv.session() as session:
                session.run("CREATE (e:Episodic {name: $name, summary: $summary, created_at: datetime(), group_id: 'neo4j'})", {"name": agent_id + " task outcome", "summary": summary})
        except Exception:
            pass
