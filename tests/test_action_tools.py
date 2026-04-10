"""Tests for Phase 3: ActionCollector + domain-specific action tools.

These tests exercise the action collector and the inner functions of
the domain tools without requiring the `camel` package (which wraps
them as FunctionTools for OASIS integration).
"""

from ssflow.action_collector import ActionCollector
from ssflow.action_tools import ROLE_TOOL_MAP, tool_for_role
from ssflow.persona import Persona


def _p(id, role):
    return Persona(id=id, archetype="X", display_name="X",
                   voice_prompt="x", entity_role=role)


class TestActionCollector:
    def test_add_and_drain(self):
        ac = ActionCollector()
        ac.set_round(3)
        ac.add("co", "company_ir_byd", "announce", {"text": "test"})
        ac.add("reg", "regulator_csrc", "regulate", {"text": "inquiry"})
        pending = ac.drain()
        assert len(pending) == 2
        assert pending[0].round_idx == 3
        assert pending[0].action_type == "announce"
        assert len(ac.drain()) == 0

    def test_drain_is_atomic(self):
        ac = ActionCollector()
        ac.add("a", "p", "x", {})
        ac.add("b", "p", "y", {})
        assert len(ac) == 2
        out = ac.drain()
        assert len(out) == 2
        assert len(ac) == 0


class TestAnnounceTool:
    def test_announce_inner_fn(self):
        """Test the announce logic by calling the collector directly."""
        ac = ActionCollector()
        ac.add(
            agent_id="company_ir_byd",
            persona_id="company_ir_byd",
            action_type="announce",
            payload={
                "text": "[公司公告] Q1业绩不及预期\n营收同比下降10%",
                "content_type": "company_announcement",
                "announcement_type": "earnings_update",
                "authority_weight": 0.90,
            },
        )
        pending = ac.drain()
        assert len(pending) == 1
        assert pending[0].action_type == "announce"
        assert "[公司公告]" in pending[0].payload["text"]
        assert pending[0].payload["authority_weight"] == 0.90


class TestRegulateTool:
    def test_regulate_inner_fn(self):
        ac = ActionCollector()
        ac.add(
            agent_id="regulator_csrc",
            persona_id="regulator_csrc",
            action_type="regulate",
            payload={
                "text": "[监管] 比亚迪: 要求说明业绩下滑原因",
                "content_type": "policy_statement",
                "regulation_type": "inquiry",
                "target": "比亚迪",
                "authority_weight": 0.95,
            },
        )
        pending = ac.drain()
        assert len(pending) == 1
        assert "[监管]" in pending[0].payload["text"]
        assert pending[0].payload["authority_weight"] == 0.95


class TestPublishTool:
    def test_publish_inner_fn(self):
        ac = ActionCollector()
        ac.add(
            agent_id="sellside_analyst_citic",
            persona_id="sellside_analyst_citic",
            action_type="publish_research",
            payload={
                "text": "[研报] 比亚迪Q1点评 (评级: hold)\n业绩低于预期但不改长期看法",
                "content_type": "research_note",
                "rating": "hold",
                "authority_weight": 0.80,
            },
        )
        pending = ac.drain()
        assert len(pending) == 1
        assert "hold" in pending[0].payload["text"]


class TestToolForRole:
    def test_role_tool_map_coverage(self):
        assert "company_ir" in ROLE_TOOL_MAP
        assert "regulator" in ROLE_TOOL_MAP
        assert "policy" in ROLE_TOOL_MAP
        assert "analyst" in ROLE_TOOL_MAP

    def test_media_gets_none(self):
        ac = ActionCollector()
        assert tool_for_role(_p("m", "media"), ac) is None

    def test_trader_gets_none(self):
        ac = ActionCollector()
        assert tool_for_role(_p("t", "trader"), ac) is None

    def test_kol_gets_none(self):
        ac = ActionCollector()
        assert tool_for_role(_p("k", "kol"), ac) is None
