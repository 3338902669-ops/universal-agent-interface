"""Agent negotiation semantics (2026-11-16)."""
from __future__ import annotations
import json, time
from pathlib import Path

class NegotiationResponse:
    ACCEPT = "accept"
    REJECT = "reject"
    COUNTER_OFFER = "counter_offer"
    DELEGATE = "delegate"

    def __init__(self, response_type, agent_id, reason="", conditions=None, delegate_to=None):
        if response_type not in (self.ACCEPT, self.REJECT, self.COUNTER_OFFER, self.DELEGATE):
            raise ValueError("invalid_response_type: %s" % response_type)
        if response_type == self.REJECT and not reason:
            raise ValueError("reject_requires_reason")
        if response_type == self.DELEGATE and not delegate_to:
            raise ValueError("delegate_requires_target")
        self.response_type = response_type
        self.agent_id = agent_id
        self.reason = reason
        self.conditions = conditions or []
        self.delegate_to = delegate_to
        self.timestamp = time.time()

    def to_dict(self):
        return {"type": self.response_type, "agent_id": self.agent_id,
                "reason": self.reason, "conditions": self.conditions,
                "delegate_to": self.delegate_to, "timestamp": self.timestamp}

class NegotiationLog:
    def __init__(self, task_id):
        self.task_id = task_id
        self.entries = []
        self.final_agent = None
        self.final_type = None

    def add(self, response):
        self.entries.append(response.to_dict())
        if response.response_type == NegotiationResponse.ACCEPT:
            self.final_agent = response.agent_id
            self.final_type = NegotiationResponse.ACCEPT

    @property
    def rejected_agents(self):
        return [e["agent_id"] for e in self.entries if e["type"] == NegotiationResponse.REJECT]

    @property
    def is_resolved(self):
        return self.final_type == NegotiationResponse.ACCEPT

    def summary(self):
        parts = []
        for e in self.entries:
            parts.append("%s -> %s (%s)" % (e["agent_id"], e["type"], e.get("reason","")[:50]))
        if self.final_agent:
            parts.append("FINAL: %s accepted" % self.final_agent)
        return "; ".join(parts) if parts else "no entries"

class AgentNegotiator:
    def __init__(self, agent_id, skills, max_concurrent=2, current_load=0, known_limits=None):
        self.agent_id = agent_id
        self.skills = set(s.lower() for s in skills)
        self.max_concurrent = max_concurrent
        self.current_load = current_load
        self.known_limits = known_limits or []

    def evaluate(self, task_desc, task_type, required_skills=None):
        desc_lower = task_desc.lower()
        for limit in self.known_limits:
            if limit.lower() in desc_lower:
                return NegotiationResponse(NegotiationResponse.REJECT, self.agent_id, reason="known_limit: %s" % limit)
        if self.current_load >= self.max_concurrent:
            return NegotiationResponse(NegotiationResponse.REJECT, self.agent_id, reason="capacity_full: %d/%d" % (self.current_load, self.max_concurrent))
        if required_skills:
            has_any = any(s.lower() in self.skills for s in required_skills)
            if not has_any and self.skills:
                return NegotiationResponse(NegotiationResponse.REJECT, self.agent_id, reason="skill_mismatch: needs %s, have %s" % (required_skills, sorted(self.skills)))
        if required_skills:
            matched = [s for s in required_skills if s.lower() in self.skills]
            if matched and len(matched) < len(required_skills):
                missing = [s for s in required_skills if s.lower() not in self.skills]
                return NegotiationResponse(NegotiationResponse.COUNTER_OFFER, self.agent_id, reason="partial_match: have %s, missing %s" % (matched, missing), conditions=["need %s support" % m for m in missing])
        return NegotiationResponse(NegotiationResponse.ACCEPT, self.agent_id)

class NegotiationLoop:
    def __init__(self, router, negotiators, max_rounds=None):
        self.router = router
        self.negotiators = negotiators
        self.max_rounds = max_rounds or len(negotiators)

    def negotiate(self, task_desc, task_id=""):
        route_result = self.router.route(task_desc)
        candidates = route_result.get("candidates", [])
        if not candidates:
            return {"status": "no_candidates", "log": NegotiationLog(task_id).summary()}
        log = NegotiationLog(task_id)
        tried = set()
        for round_num, candidate in enumerate(candidates):
            agent_id = candidate["agent_id"]
            if agent_id in tried:
                continue
            tried.add(agent_id)
            negotiator = self.negotiators.get(agent_id)
            if not negotiator:
                log.add(NegotiationResponse(NegotiationResponse.REJECT, agent_id, reason="no_negotiator"))
                continue
            task_type = route_result.get("task_type", "general")
            response = negotiator.evaluate(task_desc, task_type)
            log.add(response)
            if response.response_type == NegotiationResponse.ACCEPT:
                return {"status": "accepted", "agent_id": agent_id, "negotiation_log": log.summary(), "rounds": round_num + 1, "rejected_count": len(log.rejected_agents)}
            elif response.response_type == NegotiationResponse.DELEGATE:
                delegate_to = response.delegate_to
                if delegate_to in self.negotiators and delegate_to not in tried:
                    log.add(NegotiationResponse(NegotiationResponse.ACCEPT, delegate_to, reason="delegated by %s" % agent_id))
                    return {"status": "accepted", "agent_id": delegate_to, "negotiation_log": log.summary(), "rounds": round_num + 1, "delegated_by": agent_id}
        return {"status": "escalated", "reason": "all %d candidates rejected" % len(tried), "negotiation_log": log.summary(), "rejected_agents": log.rejected_agents}
