"""Agent roster: one record per execution agent, keyed by its normalized name."""

import json
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ...logging_config import logger
from .log_store import _slugify

# Words too common to say anything about which agent a request belongs to.
_STOPWORDS = frozenset(
    "a an and about at for from in is it of on or re the to with my me i".split()
)


def _now() -> str:
    # Microseconds, so two uses in the same second still sort in the order they happened.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _singular(word: str) -> str:
    """Crude plural folding, so "invoices" finds "invoice". Applied to both sides alike."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text: str) -> set[str]:
    # Accents dropped, so "zoe" finds "Zoë"; any alphabet's letters count, so "Иван" is a word.
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return {_singular(w) for w in re.findall(r"[^\W_]+", plain) if w not in _STOPWORDS}


def _first_request(agent_name: str) -> str:
    """An old agent's first instruction is the best record of what it was created for."""
    from .log_store import get_execution_agent_logs

    for tag, _, payload in get_execution_agent_logs().iter_entries(agent_name):
        if tag == "agent_request":
            return " ".join(payload.split())[:160]
    return ""


@dataclass
class AgentRecord:
    name: str
    description: str = ""
    created_at: str = ""
    last_used: str = ""

    @property
    def key(self) -> str:
        # Same normalization as the log file name, so roster and logs can't disagree.
        return _slugify(self.name)


@dataclass
class Match:
    agent: AgentRecord
    score: float
    matched: list[str]
    coverage: float  # share of the query's known words' weight this agent matched, 0..1
    specific: list[str]  # matched words that only a few agents have, like "lisbon" or "dental"


# A word in this many agents or fewer is specific enough to suggest the same work.
SPECIFIC_WORD_MAX_AGENTS = 3


class AgentRoster:
    """Stores agent records in a JSON file."""

    def __init__(self, roster_path: Path):
        self._roster_path = roster_path
        self._agents: dict[str, AgentRecord] = {}
        self.load()

    def load(self) -> None:
        """Load records from roster.json. Accepts the old format (a list of names).

        Builds the new set first and swaps it in at the end: the API reads the roster from
        worker threads, and an empty in-between state could be saved over the real file.
        """
        agents: dict[str, AgentRecord] = {}
        if not self._roster_path.exists():
            self._agents = agents
            return
        try:
            data = json.loads(self._roster_path.read_text())
        except Exception as exc:
            logger.warning(f"Failed to load roster.json: {exc}")
            return  # keep what's in memory rather than forgetting every agent
        upgraded = False
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, str):
                # Old format: just a name. Backfill a description so search can find it.
                record = AgentRecord(name=entry, description=_first_request(entry))
                upgraded = True
            elif isinstance(entry, dict) and entry.get("name"):
                fields = AgentRecord.__dataclass_fields__
                record = AgentRecord(**{k: "" if v is None else str(v) for k, v in entry.items() if k in fields})
            else:
                continue
            # Old rosters could hold "Email to Alice" and "email to alice" as two agents
            # sharing one log file. Keep the first; they were already one agent on disk.
            agents.setdefault(record.key, record)
        self._agents = agents
        if upgraded:
            self.save()  # convert once, so old logs aren't re-read on every load

    def save(self) -> None:
        """Write atomically so a crash mid-write can't leave a half-written roster."""
        self._roster_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([asdict(a) for a in self._agents.values()], indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._roster_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, self._roster_path)
        except Exception as exc:
            logger.warning(f"Failed to save roster.json: {exc}")
            Path(tmp).unlink(missing_ok=True)

    def get(self, name: str) -> Optional[AgentRecord]:
        """Find an agent by name, ignoring case, spacing, and punctuation."""
        return self._agents.get(_slugify(name))

    def add_agent(self, name: str, description: str = "") -> AgentRecord:
        """Create an agent, or return the existing one with the same normalized name."""
        existing = self.get(name)
        if existing:
            return existing
        now = _now()
        record = AgentRecord(name=name, description=description, created_at=now, last_used=now)
        self._agents[record.key] = record
        self.save()
        return record

    def touch(self, name: str) -> None:
        """Mark an agent as just used, so it stays in the recent list."""
        record = self.get(name)
        if record:
            record.last_used = _now()
            self.save()

    def recent(self, limit: int) -> list[AgentRecord]:
        """Most recently used first. Insertion order breaks ties (newer wins)."""
        ordered = list(self._agents.values())
        ordered.reverse()
        return sorted(ordered, key=lambda a: a.last_used, reverse=True)[:limit]

    def search(self, query: str, limit: int = 5) -> list[Match]:
        """Rank agents by the query words they share, weighting rare words more.

        This is the inverse-document-frequency part of BM25. "email" appears in most
        agents, so it barely counts; "alice" appears in few, so it counts a lot.
        """
        query_words = _words(query)
        agents = list(self._agents.values())
        if not query_words or not agents:
            return []
        docs = [(a, _words(f"{a.name} {a.description}")) for a in agents]
        n = len(docs)

        df = {w: sum(1 for _, words in docs if w in words) for w in query_words}
        weights = {w: math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5)) for w in query_words}
        # Words no agent has ("status" in "Tokyo flight status") say nothing about which agent
        # this resembles, and would otherwise outweigh every shared word.
        total = sum(weights[w] for w in query_words if df[w]) or 1.0
        matches = []
        for agent, words in docs:
            shared = sorted(query_words & words, key=lambda w: -weights[w])
            if shared:
                score = sum(weights[w] for w in shared)
                specific = [w for w in shared if df[w] <= SPECIFIC_WORD_MAX_AGENTS and not w.isdigit()]
                matches.append(Match(agent, score, shared, score / total, specific))
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:limit]

    def get_agents(self) -> list[str]:
        """All agent names."""
        return [a.name for a in self._agents.values()]

    def __len__(self) -> int:
        return len(self._agents)

    def clear(self) -> None:
        """Clear the agent roster."""
        self._agents = {}
        try:
            self._roster_path.unlink(missing_ok=True)
            logger.info("Cleared agent roster")
        except Exception as exc:
            logger.warning(f"Failed to clear roster.json: {exc}")


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_ROSTER_PATH = _DATA_DIR / "execution_agents" / "roster.json"

_agent_roster = AgentRoster(_ROSTER_PATH)


def get_agent_roster() -> AgentRoster:
    """Get the singleton roster instance."""
    return _agent_roster
