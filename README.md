# CAD Copilot

Describe a part in plain English and let Claude build it in SolidWorks.

```
you> make an 80x50x6 mm mounting plate with 4 corner holes, 5 mm dia, 8 mm in from each edge
  -> new_part({})
  -> create_sketch({"plane": "top", "shapes": [{"type": "rectangle", "x1": -40, "y1": -25, "x2": 40, "y2": 25}]})
  -> extrude({"sketch": "Sketch1", "depth": 6})
  -> create_sketch({"plane": "top", "shapes": [{"type": "circle", "cx": -32, "cy": -17, "r": 2.5}, ...]})
  -> cut({"sketch": "Sketch2", "through_all": true})
copilot> Built an 80 x 50 x 6 mm plate with four Ø5 mm through holes...
```

## How it works

```
you ──text──> agent.py ──(tools)──> Claude API
                 │  <──tool calls───────┘
                 v
            sw_bridge.py ──COM (pywin32)──> SolidWorks
```

- **agent.py** holds the tool definitions, the system prompt, and the agent loop. Claude decides which tools to call, the loop runs them and sends back the results, and it repeats until the part is done.
- **Tools**: new part, sketch (rectangles, circles, lines, centerlines), extrude, cut, revolve, fillet, chamfer, shell, model info (feature tree, bounding box, mass), screenshot, undo, save/export.
- **Self-check**: after building, Claude takes an isometric screenshot and looks at it to confirm the part matches the request.
- **sw_bridge.py** maps each tool onto SolidWorks API calls (`SketchManager`, `FeatureManager.FeatureExtrusion2`, `FeatureCut4`, and so on). Errors go back to Claude as text so it can correct itself.
- **MockBridge** has the same interface with no SolidWorks behind it, so you can work on prompts and the loop from any computer.

## Setup

1. Install Python 3.10+ from python.org (tick "Add to PATH").
2. `pip install -r requirements.txt`
3. Get an API key at console.anthropic.com and set it:
   `setx ANTHROPIC_API_KEY "sk-ant-..."` (open a new terminal afterwards).
4. Check everything works (offline, free): `python -m unittest test_agent`
5. Run it:
   - `python agent.py --mock` works anywhere, no SolidWorks needed.
   - `python agent.py` needs SolidWorks installed; it connects to the running copy or starts one.

## Things to try

- "Make a 40 mm cube with a 10 mm hole through the middle"
- "Build a 100x60x20 mm box and shell it to a tray with 2 mm walls, open at the top"
- "Make a 20 mm diameter, 80 mm long shaft with a 1 mm chamfer on both ends" (uses revolve)
- "Add 3 mm fillets to the four vertical edges"
- "Put 6 M5 clearance holes on a 60 mm bolt circle" (Claude works out the positions itself)
- "Add an M8 thread to the shaft" (there's no thread tool, so it should say so)
- "Save it as bracket.STEP"

## Roadmap ideas (good portfolio material)

1. **More tools**: linear/circular pattern, mirror, and reference planes. Each is one bridge method plus one tool entry.
2. **Smarter selection**: pick faces and edges by description ("the top face") in place of 3D points.
3. **Parametric edits**: read and change dimensions by name ("make the plate 10 mm longer").
4. **Other CAD backends**: write an `InventorBridge` or `FusionBridge` with the same methods; agent.py doesn't change.
5. **In-app UI**: a SolidWorks task-pane add-in (C#) or a small web UI in place of the terminal.
6. **MCP server**: expose the bridge as an MCP server so Claude Desktop or Claude Code can drive SolidWorks directly.

## Known limitations

- Tested on SOLIDWORKS 2025: sketch, extrude, fillet, shell, revolve, chamfer, model info and screenshot all work.
- Edges and faces are picked by 3D points. The bridge finds the nearest edge or face in the actual geometry (within 0.5 mm), so hidden edges work too, but Claude still has to work out the coordinates. That's fine for simple prismatic and turned parts.
- Cost: each request resends the conversation. Prompt caching, dropping old screenshots and a 30-step cap per request keep this in check, but a long session with many parts still adds up. Start a fresh session for each new part.
