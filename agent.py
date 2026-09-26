"""CAD Copilot: tell SolidWorks what to build in plain English.

    python agent.py          # drive a running SolidWorks
    python agent.py --mock   # no SolidWorks needed; prints what it would do
"""
import json
import sys

import anthropic

from sw_bridge import MockBridge, SolidWorksBridge, SolidWorksError

MODEL = "claude-opus-5"
MAX_STEPS = 30  # API calls per request before pausing, so a stuck loop can't run up the bill

SYSTEM_PROMPT = """You are CAD Copilot, an assistant that builds and edits parts in SolidWorks
by calling tools. The user describes what they want in plain English; you turn it
into a sequence of modelling operations.

Units and coordinates:
- All lengths are millimetres.
- Sketch shapes use the sketch plane's own 2D coordinates, with the part origin at (0, 0).
  Front Plane: sketch (x, y) = model (X, Y). Top Plane: sketch x = model X, sketch y = model -Z.
  Right Plane: sketch x = model -Z, sketch y = model Y.
- face_point and edge_points are 3D model coordinates (X, Y, Z) of a point lying on that face or edge.

How to work:
- If no part is open, call new_part first.
- Build one feature at a time: create_sketch, then extrude or cut using the sketch name it returns.
- Prefer sketching on the standard planes and centring parts on the origin; it keeps
  coordinates predictable. For holes, a through_all cut from a standard plane is the most robust choice.
- Before a fillet, or whenever you're unsure of the geometry, call get_model_info and use the
  bounding box to work out edge and face coordinates.
- For round parts (shafts, knobs, bottles), sketch half the profile plus one centerline on the axis, then revolve.
- After finishing a part, or after any step that could plausibly have gone wrong, call take_screenshot
  and check that the part looks like what the user asked for.
- If a tool returns an error, read it, fix the inputs, and try again. Use undo if a feature came out wrong.
- For hole patterns (grids, bolt circles) or symmetric features, calculate every position yourself
  and draw all the shapes in a single sketch, then cut or extrude once.
- If the user asks for something no tool can do (threads, lofts, sweeps...), say so plainly rather
  than improvising something different, and suggest the closest thing you can do.
- When you're done, briefly summarise what you built (overall size, features)."""

POINT = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}

TOOLS = [
    {
        "name": "new_part",
        "description": "Create a new, empty part document using the default template.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_sketch",
        "description": (
            "Create a sketch containing one or more closed shapes (or lines) on a standard plane "
            "or on a planar face. Returns the new sketch's name, e.g. 'Sketch2'. Give either "
            "plane or face_point."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plane": {"type": "string", "enum": ["front", "top", "right"]},
                "face_point": {**POINT, "description": "A model-space point on the planar face to sketch on."},
                "shapes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["rectangle", "circle", "line", "centerline"]},
                            "x1": {"type": "number"}, "y1": {"type": "number"},
                            "x2": {"type": "number"}, "y2": {"type": "number"},
                            "cx": {"type": "number"}, "cy": {"type": "number"},
                            "r": {"type": "number", "description": "Circle radius"},
                        },
                        "required": ["type"],
                    },
                    "description": (
                        "rectangle: opposite corners x1,y1,x2,y2. circle: centre cx,cy and radius r. "
                        "line / centerline: x1,y1 to x2,y2. Lines must join end to end to form a closed "
                        "profile; a centerline is construction geometry used as a revolve axis."
                    ),
                },
            },
            "required": ["shapes"],
        },
    },
    {
        "name": "extrude",
        "description": "Extrude a sketch into a solid (boss). Merges with existing bodies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sketch": {"type": "string", "description": "Sketch name, e.g. 'Sketch1'"},
                "depth": {"type": "number", "description": "Depth in mm (total depth if midplane)"},
                "reverse": {"type": "boolean", "description": "Extrude in the opposite direction"},
                "midplane": {"type": "boolean", "description": "Extrude equally in both directions"},
            },
            "required": ["sketch", "depth"],
        },
    },
    {
        "name": "cut",
        "description": "Cut material away using a sketch: a blind cut to a depth, or through everything.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sketch": {"type": "string"},
                "depth": {"type": "number", "description": "Depth in mm (ignored if through_all)"},
                "through_all": {"type": "boolean", "description": "Cut through the whole part in both directions"},
                "reverse": {"type": "boolean"},
            },
            "required": ["sketch"],
        },
    },
    {
        "name": "fillet",
        "description": "Round one or more edges with a constant radius. Each edge is picked by a point lying on it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "radius": {"type": "number", "description": "Radius in mm"},
                "edge_points": {"type": "array", "items": POINT, "minItems": 1},
            },
            "required": ["radius", "edge_points"],
        },
    },
    {
        "name": "chamfer",
        "description": "Bevel one or more edges by a distance and angle. Each edge is picked by a point lying on it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "distance": {"type": "number", "description": "Chamfer distance in mm"},
                "angle": {"type": "number", "description": "Angle in degrees (default 45)"},
                "edge_points": {"type": "array", "items": POINT, "minItems": 1},
            },
            "required": ["distance", "edge_points"],
        },
    },
    {
        "name": "revolve",
        "description": (
            "Revolve a sketch around the single centerline it contains, to make round parts. "
            "Set cut to remove material instead (e.g. a groove)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sketch": {"type": "string"},
                "angle": {"type": "number", "description": "Degrees, default 360"},
                "reverse": {"type": "boolean"},
                "cut": {"type": "boolean"},
            },
            "required": ["sketch"],
        },
    },
    {
        "name": "shell",
        "description": (
            "Hollow out the part with a constant wall thickness. The faces picked by face_points are "
            "removed (left open), e.g. the top face of a box to make a tray."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "thickness": {"type": "number", "description": "Wall thickness in mm"},
                "face_points": {"type": "array", "items": POINT, "minItems": 1},
                "outward": {"type": "boolean", "description": "Add the walls outside the current shape"},
            },
            "required": ["thickness", "face_points"],
        },
    },
    {
        "name": "take_screenshot",
        "description": "Return an isometric image of the current part, to check visually that it looks right.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_model_info",
        "description": "List the feature tree plus the bounding box, volume and mass of the current part.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "undo",
        "description": "Undo the last operation(s).",
        "input_schema": {
            "type": "object",
            "properties": {"steps": {"type": "integer", "minimum": 1}},
        },
    },
    {
        "name": "save",
        "description": "Save the part. The extension picks the format: .SLDPRT, .STEP, .STL, etc.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def run_tool(bridge, name, args):
    """Run one tool call. Returns (result, is_error); result is text or a list of content blocks."""
    if name not in TOOL_NAMES:
        return f"Unknown tool {name!r}.", True
    try:
        return getattr(bridge, name)(**args), False
    except SolidWorksError as e:
        return f"Error: {e}", True
    except Exception as e:  # COM errors, bad arguments, etc.; let Claude see and recover
        return f"Error ({type(e).__name__}): {e}", True


def drop_old_screenshots(messages):
    """Keep only the newest screenshot in the history; every image is resent on every request."""
    image_results = [
        block
        for m in messages if m["role"] == "user" and isinstance(m["content"], list)
        for block in m["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
        and isinstance(block["content"], list)
        and any(b.get("type") == "image" for b in block["content"])
    ]
    for block in image_results[:-1]:
        block["content"] = "[Earlier screenshot removed to save tokens.]"


def run_turn(client, bridge, messages):
    """Let Claude call tools until it has finished answering the latest user message."""
    for _ in range(MAX_STEPS):
        drop_old_screenshots(messages)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            cache_control={"type": "ephemeral"},  # reuse the unchanged history at ~10% of the price
            # If Opus declines a request, the API retries it on a fallback model instead of stopping.
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"\ncopilot> {block.text}")

        if response.stop_reason == "tool_use":
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                print(f"  -> {block.name}({json.dumps(block.input)})")
                content, is_error = run_tool(bridge, block.name, block.input)
                print(f"     {content if isinstance(content, str) else '[image]'}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,  # a string, or a list of blocks (e.g. an image)
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "refusal":
            print("\ncopilot> (The model declined this request.)")
        elif response.stop_reason == "max_tokens":
            print("\ncopilot> (Response was cut off. Try a smaller request.)")
        return

    print(f"\ncopilot> (Stopped after {MAX_STEPS} steps to limit cost. Say 'continue' to keep going.)")


def main():
    mock = "--mock" in sys.argv
    try:
        bridge = MockBridge() if mock else SolidWorksBridge()
    except SolidWorksError as e:
        sys.exit(f"{e}\nTip: run with --mock to try it without SolidWorks.")

    client = anthropic.Anthropic()
    messages = []
    print(f"CAD Copilot ({'mock mode' if mock else 'connected to SolidWorks'}). Type 'quit' to exit.")

    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user.lower() in {"quit", "exit"}:
            break
        if not user:
            continue
        start = len(messages)
        messages.append({"role": "user", "content": user})
        try:
            run_turn(client, bridge, messages)
        except anthropic.AuthenticationError:
            sys.exit("Invalid API key. Set the ANTHROPIC_API_KEY environment variable.")
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            # Drop the half-finished turn so the history stays valid. Any features already
            # built stay in SolidWorks; get_model_info will show them next time.
            print(f"API error: {e}. Try again.")
            del messages[start:]


if __name__ == "__main__":
    main()
