"""AI社員の実装レジストリ。implementation_key → Agent 実装。"""

from ai_business_explorer.agents.base import Agent
from ai_business_explorer.agents.employees.idea_generator import IdeaGenerator
from ai_business_explorer.agents.employees.market_researcher import MarketResearcher


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        if agent.implementation_key in self._agents:
            raise ValueError(f"agent implementation already registered: {agent.implementation_key}")
        self._agents[agent.implementation_key] = agent

    def get(self, implementation_key: str) -> Agent | None:
        return self._agents.get(implementation_key)

    def keys(self) -> list[str]:
        return sorted(self._agents)


def build_default_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(IdeaGenerator())
    registry.register(MarketResearcher())
    return registry
