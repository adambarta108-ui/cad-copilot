"""Offline tests: no API key or SolidWorks needed.

    python -m unittest test_agent
"""
import inspect
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace as NS

import agent
from sw_bridge import MockBridge, SolidWorksBridge


def tool_use(id, name, **inp):
    return NS(type="tool_use", id=id, name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


class FakeClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class ToolDefinitionTests(unittest.TestCase):
    def test_every_tool_exists_on_both_bridges(self):
        for bridge in (SolidWorksBridge, MockBridge):
            for name in agent.TOOL_NAMES:
                self.assertTrue(hasattr(bridge, name), f"{bridge.__name__} is missing {name}")

    def test_schema_properties_match_method_parameters(self):
        for tool in agent.TOOLS:
            props = set(tool["input_schema"]["properties"])
            for bridge in (SolidWorksBridge, MockBridge):
                params = set(inspect.signature(getattr(bridge, tool["name"])).parameters) - {"self"}
                self.assertEqual(props, params, f"{bridge.__name__}.{tool['name']}")


class RunToolTests(unittest.TestCase):
    def setUp(self):
        self.bridge = MockBridge()
        self.out = io.StringIO()

    def run_tool(self, name, **args):
        with redirect_stdout(self.out):
            return agent.run_tool(self.bridge, name, args)

    def test_errors_are_returned_not_raised(self):
        result, is_error = self.run_tool("extrude", sketch="Sketch1", depth=5)
        self.assertTrue(is_error)
        self.assertIn("new_part", result)

    def test_unknown_tool(self):
        _, is_error = self.run_tool("format_c_drive")
        self.assertTrue(is_error)

    def test_bad_arguments_are_reported(self):
        self.run_tool("new_part")
        result, is_error = self.run_tool("extrude", sketch="Sketch1")  # missing depth
        self.assertTrue(is_error)
        self.assertIn("TypeError", result)

    def test_feature_names_and_undo(self):
        self.run_tool("new_part")
        self.assertIn("Sketch1", self.run_tool("create_sketch", plane="top", shapes=[{"type": "circle", "cx": 0, "cy": 0, "r": 5}])[0])
        self.assertIn("Boss-Extrude1", self.run_tool("extrude", sketch="Sketch1", depth=10)[0])
        self.run_tool("undo", steps=1)
        self.assertNotIn("Boss-Extrude1", self.run_tool("get_model_info")[0])


class RunTurnTests(unittest.TestCase):
    def test_tool_loop_builds_a_plate(self):
        client = FakeClient([
            NS(stop_reason="tool_use", content=[
                text("Starting a new part."),
                tool_use("t1", "new_part"),
            ]),
            NS(stop_reason="tool_use", content=[
                tool_use("t2", "create_sketch", plane="top",
                         shapes=[{"type": "rectangle", "x1": -40, "y1": -25, "x2": 40, "y2": 25}]),
            ]),
            NS(stop_reason="tool_use", content=[tool_use("t3", "extrude", sketch="Sketch1", depth=6)]),
            NS(stop_reason="end_turn", content=[text("Built an 80 x 50 x 6 mm plate.")]),
        ])
        bridge = MockBridge()
        messages = [{"role": "user", "content": "make a plate"}]
        with redirect_stdout(io.StringIO()) as out:
            agent.run_turn(client, bridge, messages)

        self.assertEqual(len(client.requests), 4)
        self.assertEqual(bridge.features[-2:], ["Sketch1", "Boss-Extrude1"])
        self.assertIn("80 x 50 x 6", out.getvalue())
        # history alternates user/assistant and every tool_use got a matching result
        self.assertEqual([m["role"] for m in messages], ["user"] + ["assistant", "user"] * 3 + ["assistant"])
        last_result = messages[-2]["content"][0]
        self.assertEqual((last_result["tool_use_id"], last_result["is_error"]), ("t3", False))

    def test_parallel_tool_calls_share_one_result_message(self):
        client = FakeClient([
            NS(stop_reason="tool_use", content=[tool_use("a", "new_part"), tool_use("b", "get_model_info")]),
            NS(stop_reason="end_turn", content=[text("done")]),
        ])
        messages = [{"role": "user", "content": "go"}]
        with redirect_stdout(io.StringIO()):
            agent.run_turn(client, MockBridge(), messages)
        results = messages[2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["a", "b"])

    def test_step_limit_stops_a_runaway_loop(self):
        client = FakeClient([
            NS(stop_reason="tool_use", content=[tool_use(f"t{i}", "undo")]) for i in range(agent.MAX_STEPS)
        ])
        with redirect_stdout(io.StringIO()) as out:
            agent.run_turn(client, MockBridge(), [{"role": "user", "content": "x"}])
        self.assertEqual(len(client.requests), agent.MAX_STEPS)
        self.assertIn("Stopped after", out.getvalue())

    def test_only_the_newest_screenshot_is_kept(self):
        image = [{"type": "image", "source": {}}, {"type": "text", "text": "view"}]
        messages = [
            {"role": "user", "content": "go"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": list(image)}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b", "content": "ok"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": list(image)}]},
        ]
        agent.drop_old_screenshots(messages)
        self.assertIsInstance(messages[1]["content"][0]["content"], str)
        self.assertEqual(messages[2]["content"][0]["content"], "ok")
        self.assertEqual(messages[3]["content"][0]["content"], image)

    def test_refusal_stops_the_turn(self):
        client = FakeClient([NS(stop_reason="refusal", content=[])])
        with redirect_stdout(io.StringIO()) as out:
            agent.run_turn(client, MockBridge(), [{"role": "user", "content": "x"}])
        self.assertIn("declined", out.getvalue())


if __name__ == "__main__":
    unittest.main()
