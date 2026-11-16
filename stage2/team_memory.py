"""Team memory bridge (2026-11-16).

Inject relevant graph facts into agent prompts so agents "know" past context.

Features:
- bolt connection to Neo4j (Graphiti graph)
- search Episodic / Entity nodes with word-split fallback
- TTL cache (300s) to avoid repeated Neo4j queries on identical tasks
- circuit breaker (60s) after a failed query so a down Neo4j never slows dispatch
- connection_timeout 1.5s so unreachable Neo4j fails fast
- record_outcome writes task results back to the graph
"""
from __future__ import annotations

import os
import time


# 与 inbound.py 的信封凭据检查保持一致：这些字样不得进入指令
CREDENTIAL_MARKERS = ("api_key", "apikey", "password", "secret", "token=")


def _looks_like_credential(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in CREDENTIAL_MARKERS)




def _now_iso_source():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

def _now_iso_source():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

class TeamMemoryBridge:
    def __init__(self, uri="bolt://127.0.0.1:7687", user="neo4j", password="graphiti",
                 max_facts=5, connect_timeout_s=1.5, cache_ttl_s=300, circuit_s=60):
        self.uri = uri
        self.auth = (user, password)
        self.max_facts = max_facts
        self._driver = None
        self._connect_timeout_s = connect_timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._circuit_s = circuit_s
        self._cache = {}
        self._circuit_open_until = 0.0
        self.last_dropped_facts = 0

    # ---------- infrastructure ----------

    def _get_driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                self.uri, auth=self.auth,
                connection_timeout=self._connect_timeout_s,
            )
        return self._driver

    def close(self):
        if self._driver:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None

    def _circuit_open(self):
        return time.time() < self._circuit_open_until

    def _cache_get(self, key):
        e = self._cache.get(key)
        if e and (time.time() - e[1]) < self._cache_ttl_s:
            return e[0]
        return None

    def _cache_put(self, key, val):
        self._cache[key] = (val, time.time())
        if len(self._cache) > 200:
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][1])[:50]:
                self._cache.pop(k, None)

    # ---------- search ----------

    def search(self, query, max_facts=0):
        drv = self._get_driver()
        limit = max_facts or self.max_facts
        cypher = (
            "MATCH (e:Episodic) "
            "WHERE e.summary CONTAINS $q OR e.name CONTAINS $q "
            "RETURN e.name AS name, e.summary AS summary LIMIT $limit"
        )
        facts = []
        try:
            with drv.session() as session:
                for record in session.run(cypher, {"q": query, "limit": limit}):
                    facts.append(dict(record))
                if len(facts) < limit:
                    for w in query.split():
                        if len(w) < 3 or len(facts) >= limit:
                            continue
                        for record in session.run(cypher, {"q": w, "limit": limit - len(facts)}):
                            d = dict(record)
                            if d not in facts:
                                facts.append(d)
        except Exception as e:
            facts.append({"error": str(e)[:200]})
        return facts

    def search_entities(self, query):
        drv = self._get_driver()
        cypher = (
            "MATCH (n:Entity) "
            "WHERE n.name CONTAINS $q "
            "RETURN n.name AS name, n.summary AS summary LIMIT $limit"
        )
        entities = []
        try:
            with drv.session() as session:
                for record in session.run(cypher, {"q": query, "limit": self.max_facts}):
                    entities.append(dict(record))
                if len(entities) < self.max_facts:
                    for w in query.split():
                        if len(w) < 3 or len(entities) >= self.max_facts:
                            continue
                        for record in session.run(cypher, {"q": w, "limit": self.max_facts - len(entities)}):
                            d = dict(record)
                            if d not in entities:
                                entities.append(d)
        except Exception as e:
            entities.append({"error": str(e)[:200]})
        return entities

    # ---------- main API ----------

    def inject_context(self, task_desc, agent_id=""):
        """Return task_desc prefixed with relevant team memory, or task_desc unchanged."""
        if self._circuit_open():
            return task_desc

        cached = self._cache_get(task_desc)
        if cached is not None:
            relevant = cached
        else:
            entities = self.search_entities(task_desc)
            if any("error" in x for x in entities):
                self._circuit_open_until = time.time() + self._circuit_s
                return task_desc
            facts = self.search(task_desc)
            if any("error" in x for x in facts):
                self._circuit_open_until = time.time() + self._circuit_s
                return task_desc

            relevant = []
            for e in entities:
                s = e.get("summary", "") or e.get("name", "")
                if s and s not in relevant:
                    relevant.append(s[:200])
            for f in facts:
                s = f.get("summary", "") or f.get("name", "")
                if s and s not in relevant:
                    relevant.append(s[:200])
            self._cache_put(task_desc, relevant)

        # 过滤掉含凭据样字样的记忆：既不外泄，也避免撞信封凭据检查
        dropped = [r for r in relevant if _looks_like_credential(r)]
        self.last_dropped_facts = len(dropped)
        if dropped:
            relevant = [r for r in relevant if not _looks_like_credential(r)]

        if not relevant:
            return task_desc

        block = "[团队记忆 - 自动注入]\n"
        for r in relevant[:self.max_facts]:
            block += "- %s\n" % r
        block += "[团队记忆结束]\n\n"
        return block + task_desc

    def record_outcome(self, agent_id, task_desc, success, verdict, source="stage2.team_memory"):
        drv = self._get_driver()
        summary = "%s executed '%s' result: %s (verdict: %s)" % (
            agent_id, task_desc[:100], "success" if success else "fail", verdict)
        try:
            with drv.session() as session:
                session.run(
                    "CREATE (e:Episodic {name: $name, summary: $summary, "
                    "created_at: datetime(), reference_time: $rt, "
                    "source: $src, group_id: 'neo4j'})",
                    {"name": agent_id + " task outcome", "summary": summary,
                     "rt": _now_iso_source(), "src": source},
                )
        except Exception:
            pass
