"""A2A bidirectional discovery protocol (2026-11-16)."""
from __future__ import annotations
import json, os, time, urllib.request, urllib.error
from pathlib import Path

CACHE_TTL_S = 300

class DiscoveryError(ValueError):
    pass

class DiscoveryRegistry:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"peers": []}), encoding="utf-8")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def register(self, url, name=""):
        if not url.startswith(("http://", "https://")):
            raise DiscoveryError("peer_url_must_be_http")
        if any(p["url"] == url for p in self.data["peers"]):
            return {"added": False, "reason": "already_registered"}
        entry = {"url": url.rstrip("/"), "name": name or url, "registered_at": time.time()}
        self.data["peers"].append(entry)
        self._save()
        return {"added": True, "url": url}

    def unregister(self, url):
        before = len(self.data["peers"])
        self.data["peers"] = [p for p in self.data["peers"] if p["url"] != url]
        self._save()
        return {"removed": before - len(self.data["peers"])}

    def peers(self):
        return self.data["peers"]

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

class AgentCardCache:
    def __init__(self, ttl_s=CACHE_TTL_S):
        self.ttl_s = ttl_s
        self._cache = {}

    def get(self, url):
        entry = self._cache.get(url)
        if entry and (time.time() - entry[1]) < self.ttl_s:
            return entry[0]
        return None

    def put(self, url, card):
        self._cache[url] = (card, time.time())

def fetch_remote_card(base_url, timeout_s=10):
    url = base_url.rstrip("/") + "/.well-known/agent-card.json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            card = json.loads(resp.read().decode("utf-8"))
        if not isinstance(card, dict) or "name" not in card:
            raise DiscoveryError("invalid_agent_card")
        return card
    except urllib.error.HTTPError as e:
        raise DiscoveryError("remote_card_http_%d" % e.code)
    except Exception as e:
        raise DiscoveryError("remote_card_unreachable: %s" % str(e)[:120])

def discover_all(registry, cache=None, timeout_s=10):
    cache = cache or AgentCardCache()
    results = []
    for peer in registry.peers():
        url = peer["url"]
        cached = cache.get(url)
        if cached:
            results.append({"url": url, "card": cached, "source": "cache"})
            continue
        try:
            card = fetch_remote_card(url, timeout_s)
            cache.put(url, card)
            results.append({"url": url, "card": card, "source": "remote"})
        except DiscoveryError as e:
            results.append({"url": url, "error": str(e), "source": "unreachable"})
    return results

def build_dynamic_card(interface_id, capabilities, public_url, hub_url, description=""):
    return {
        "name": interface_id,
        "description": description or ("UAI hub agent: %s" % interface_id),
        "url": public_url,
        "version": "0.15.4",
        "capabilities": {"streaming": True, "pushNotifications": True, "stateTransitionHistory": True},
        "skills": [{"id": c.get("name",""), "name": c.get("name",""), "description": c.get("description",""), "tags": ["uai"]} for c in capabilities],
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "provider": {"organization": "UAI", "url": hub_url},
        "uai/hub_url": hub_url,
        "uai/contract_version": "1.0",
        "uai/acceptance_gate": True,
    }
