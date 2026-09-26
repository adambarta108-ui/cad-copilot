"""Thin wrapper around the SolidWorks COM API.

Every public method takes millimetres and returns a short human-readable
string, because that string is what gets sent back to Claude as the tool result.
Method names match the tool names in agent.py.
"""
import base64
import io
import math
import os
import tempfile
import types

MM = 0.001  # the SolidWorks API works in metres
PICK_TOLERANCE = 0.5 * MM  # how close a point must be to count as "on" an edge or face

PLANES = {"front": "Front Plane", "top": "Top Plane", "right": "Right Plane"}

# SolidWorks enum values used below (swconst)
SW_DEFAULT_TEMPLATE_PART = 8  # swUserPreferenceStringValue_e
END_BLIND, END_THROUGH_ALL, END_MIDPLANE = 0, 1, 6  # swEndConditions_e
SAVE_SILENT = 1  # swSaveAsOptions_e


class SolidWorksError(RuntimeError):
    pass


def solidworks_installed():
    """True if SolidWorks has registered its automation interface on this PC."""
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application\CLSID"))
        return True
    except (ImportError, OSError):
        return False


def parts_folder(create=True):
    """Documents\\CAD Copilot Parts: where saves with a bare filename end up."""
    documents = os.path.join(os.path.expanduser("~"), "Documents")
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL
            documents = buf.value  # respects OneDrive / redirected Documents folders
    except (AttributeError, OSError):
        pass
    folder = os.path.join(documents, "CAD Copilot Parts")
    if create:
        os.makedirs(folder, exist_ok=True)
    return folder


def _resolve_save_path(path, create=True):
    return path if os.path.isabs(path) else os.path.join(parts_folder(create), path)


def _mm(point):
    return [v * MM for v in point]


def _call(obj, name):
    """Call a no-argument COM method.

    With late binding, pywin32 sometimes exposes these as properties (already
    invoked, so adding () raises 'Member not found') and sometimes as methods,
    depending on the object. This handles both.
    """
    attr = getattr(obj, name)
    return attr() if isinstance(attr, types.MethodType) else attr


class SolidWorksBridge:
    def __init__(self):
        import pythoncom
        import win32com.client

        # COM "Nothing", needed for optional object arguments like callouts
        self._nothing = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        try:
            self.app = win32com.client.Dispatch("SldWorks.Application")
        except Exception as e:
            raise SolidWorksError("Could not connect to SolidWorks. Is it installed?") from e
        self.app.Visible = True

    # ---------- helpers ----------

    @property
    def model(self):
        doc = self.app.ActiveDoc
        if doc is None:
            raise SolidWorksError("No document is open. Call new_part first.")
        return doc

    def _select(self, name, kind, x=0.0, y=0.0, z=0.0, append=False, mark=0):
        ok = self.model.Extension.SelectByID2(name, kind, x, y, z, append, mark, self._nothing, 0)
        if not ok:
            raise SolidWorksError(f"Could not select {kind} '{name}' at ({x}, {y}, {z}) m.")

    def _select_nearest(self, kind, point, mark=0):
        """Add the edge or face lying at `point` (mm) to the selection.

        Works from the geometry itself, so unlike SelectByID2 it can pick
        entities that are hidden from the current view.
        """
        target = _mm(point)
        getter = "GetEdges" if kind == "EDGE" else "GetFaces"
        candidates = [e for body in (self.model.GetBodies2(0, False) or ()) for e in (_call(body, getter) or ())]
        if not candidates:
            raise SolidWorksError("There is no solid body to pick from yet.")

        def distance(entity):
            return math.dist(entity.GetClosestPointOn(*target)[:3], target)

        best = min(candidates, key=distance)
        gap = distance(best)
        if gap > PICK_TOLERANCE:
            raise SolidWorksError(
                f"No {kind.lower()} at {point}; the nearest one is {gap / MM:.2f} mm away. "
                "Use get_model_info to check the geometry."
            )
        select_data = _call(self.model.SelectionManager, "CreateSelectData")
        select_data.Mark = mark
        if not best.Select4(True, select_data):
            raise SolidWorksError(f"Found the {kind.lower()} at {point} but could not select it.")

    def _last_feature_name(self):
        return self.model.FeatureByPositionReverse(0).Name

    def _finish(self, feature, what):
        if feature is None:
            raise SolidWorksError(f"SolidWorks failed to create the {what}. Check the sketch and parameters.")
        self.model.ViewZoomtofit2()
        return f"Created {feature.Name}."

    # ---------- tools ----------

    def new_part(self):
        template = self.app.GetUserPreferenceStringValue(SW_DEFAULT_TEMPLATE_PART)
        if not template:
            raise SolidWorksError("No default part template is set in SolidWorks options.")
        if self.app.NewDocument(template, 0, 0, 0) is None:
            raise SolidWorksError("Could not create a new part.")
        return "Created a new empty part."

    def create_sketch(self, shapes, plane=None, face_point=None):
        m = self.model
        m.ClearSelection2(True)
        if face_point:
            self._select_nearest("FACE", face_point)
        else:
            self._select(PLANES[(plane or "front").lower()], "PLANE")

        sm = m.SketchManager
        sm.InsertSketch(True)
        sm.AddToDB = True  # skip snapping/inferencing so coordinates are exact
        try:
            for s in shapes:
                if s["type"] == "rectangle":
                    result = sm.CreateCornerRectangle(s["x1"] * MM, s["y1"] * MM, 0, s["x2"] * MM, s["y2"] * MM, 0)
                elif s["type"] == "circle":
                    result = sm.CreateCircleByRadius(s["cx"] * MM, s["cy"] * MM, 0, s["r"] * MM)
                elif s["type"] == "line":
                    result = sm.CreateLine(s["x1"] * MM, s["y1"] * MM, 0, s["x2"] * MM, s["y2"] * MM, 0)
                elif s["type"] == "centerline":
                    result = sm.CreateCenterLine(s["x1"] * MM, s["y1"] * MM, 0, s["x2"] * MM, s["y2"] * MM, 0)
                else:
                    raise SolidWorksError(f"Unknown shape type {s['type']!r}.")
                if result is None:
                    raise SolidWorksError(f"Failed to draw {s}.")
        finally:
            sm.AddToDB = False
            sm.InsertSketch(True)  # exit the sketch
        return f"Created {self._last_feature_name()} with {len(shapes)} shape(s)."

    def extrude(self, sketch, depth, reverse=False, midplane=False):
        self.model.ClearSelection2(True)
        self._select(sketch, "SKETCH")
        feature = self.model.FeatureManager.FeatureExtrusion2(
            True, False, reverse,
            END_MIDPLANE if midplane else END_BLIND, 0,
            depth * MM, 0,
            False, False, False, False, 0, 0,
            False, False, False, False,
            True, True, True,  # merge, use feature scope, auto-select
            0, 0, False,
        )
        return self._finish(feature, "extrusion")

    def cut(self, sketch, depth=None, through_all=False, reverse=False):
        if not through_all and not depth:
            raise SolidWorksError("Give a depth or set through_all.")
        self.model.ClearSelection2(True)
        self._select(sketch, "SKETCH")
        if through_all:
            # through all in both directions, so it works whichever side the sketch is on
            single_dir, end1, end2 = False, END_THROUGH_ALL, END_THROUGH_ALL
        else:
            single_dir, end1, end2 = True, END_BLIND, END_BLIND
        feature = self.model.FeatureManager.FeatureCut4(
            single_dir, False, reverse, end1, end2,
            (depth or 0) * MM, (depth or 0) * MM,
            False, False, False, False, 0, 0,
            False, False, False, False,
            False, True, True, True, True, False,
            0, 0, False, False,
        )
        return self._finish(feature, "cut")

    def fillet(self, radius, edge_points):
        self.model.ClearSelection2(True)
        for p in edge_points:
            self._select_nearest("EDGE", p, mark=1)
        n = self._nothing
        feature = self.model.FeatureManager.FeatureFillet3(
            195, radius * MM, radius * MM, 0, 0, 0, 0, n, n, n, n, n, n, n
        )
        return self._finish(feature, "fillet")

    def revolve(self, sketch, angle=360, reverse=False, cut=False):
        # The sketch must contain exactly one centerline; SolidWorks uses it as the axis.
        self.model.ClearSelection2(True)
        self._select(sketch, "SKETCH")
        feature = self.model.FeatureManager.FeatureRevolve2(
            True, True, False, cut, reverse, False,
            0, 0,  # swEndCondBlind in both directions
            math.radians(angle), 0,
            False, False, 0, 0,
            0, 0, 0,  # no thin feature
            True, True, True,  # merge, use feature scope, auto-select
        )
        return self._finish(feature, "revolve")

    def chamfer(self, distance, edge_points, angle=45):
        self.model.ClearSelection2(True)
        for p in edge_points:
            self._select_nearest("EDGE", p, mark=1)
        feature = self.model.FeatureManager.InsertFeatureChamfer(
            4, 1,  # options, swChamferAngleDistance
            distance * MM, math.radians(angle), 0, 0, 0, 0,
        )
        return self._finish(feature, "chamfer")

    def shell(self, thickness, face_points, outward=False):
        m = self.model
        m.ClearSelection2(True)
        for p in face_points:
            self._select_nearest("FACE", p, mark=1)
        before = self._last_feature_name()
        m.InsertFeatureShell(thickness * MM, outward)  # returns nothing, so check the tree instead
        after = self._last_feature_name()
        if after == before:
            raise SolidWorksError("SolidWorks failed to create the shell. The walls may be too thick for the geometry.")
        m.ViewZoomtofit2()
        return f"Created {after}."

    def take_screenshot(self):
        """Isometric picture of the part, returned as an image block for Claude to look at."""
        from PIL import Image

        m = self.model
        m.ShowNamedView2("*Isometric", 7)  # swIsometricView
        _call(m, "ViewZoomtofit2")
        _call(m, "ViewZoomout")  # the image aspect differs from the window, so leave a margin
        path = os.path.join(tempfile.gettempdir(), "cad_copilot_view.bmp")
        if not m.SaveBMP(path, 1024, 768):
            raise SolidWorksError("Could not capture the view.")
        buf = io.BytesIO()
        Image.open(path).save(buf, "PNG")  # the API takes PNG/JPEG, not BMP
        return [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(buf.getvalue()).decode(),
            }},
            {"type": "text", "text": "Isometric view of the current part."},
        ]

    def get_model_info(self):
        m = self.model
        lines = ["Features:"]
        feat = _call(m, "FirstFeature")
        while feat is not None:
            kind = _call(feat, "GetTypeName2")
            if not kind.endswith("Folder") and kind != "DetailCabinet":
                lines.append(f"  - {feat.Name} ({kind})")
            feat = _call(feat, "GetNextFeature")

        try:
            box = m.GetPartBox(True)  # (xmin, ymin, zmin, xmax, ymax, zmax) in metres
            if box:
                lo = [round(v / MM, 3) for v in box[:3]]
                hi = [round(v / MM, 3) for v in box[3:]]
                lines.append(f"Bounding box (mm): min {lo}, max {hi}")
        except Exception:
            pass

        mp = _call(m.Extension, "CreateMassProperty")
        if mp is not None:
            mass = f"{mp.Mass * 1000:.1f} g" if mp.Mass > 0 else "unknown (no material assigned)"
            lines.append(f"Volume: {mp.Volume * 1e9:.1f} mm^3, mass: {mass}")
        else:
            lines.append("No solid body yet.")
        return "\n".join(lines)

    def undo(self, steps=1):
        self.model.EditUndo2(steps)
        return f"Undid {steps} step(s)."

    def save(self, path):
        path = _resolve_save_path(path)
        err = self.model.SaveAs3(path, 0, SAVE_SILENT)
        if err != 0:
            raise SolidWorksError(f"Save failed with SolidWorks error code {err}.")
        return f"Saved to {path}."


class MockBridge:
    """Stands in for SolidWorks so you can develop the AI side on any machine."""

    def __init__(self):
        self.features = []
        self.sketch_count = 0

    def _log(self, msg):
        print(f"    [mock] {msg}")

    def _require_part(self):
        if not self.features:
            raise SolidWorksError("No document is open. Call new_part first.")

    def _add(self, prefix):
        n = sum(1 for f in self.features if f.startswith(prefix)) + 1
        name = f"{prefix}{n}"
        self.features.append(name)
        return name

    def new_part(self):
        self.features = ["Origin", "Front Plane", "Top Plane", "Right Plane"]
        self._log("new part")
        return "Created a new empty part."

    def create_sketch(self, shapes, plane=None, face_point=None):
        self._require_part()
        name = self._add("Sketch")
        self._log(f"{name} on {face_point or plane}: {shapes}")
        return f"Created {name} with {len(shapes)} shape(s)."

    def extrude(self, sketch, depth, reverse=False, midplane=False):
        self._require_part()
        name = self._add("Boss-Extrude")
        self._log(f"{name} from {sketch}, depth={depth} mm, reverse={reverse}, midplane={midplane}")
        return f"Created {name}."

    def cut(self, sketch, depth=None, through_all=False, reverse=False):
        self._require_part()
        name = self._add("Cut-Extrude")
        self._log(f"{name} from {sketch}, depth={depth}, through_all={through_all}")
        return f"Created {name}."

    def fillet(self, radius, edge_points):
        self._require_part()
        name = self._add("Fillet")
        self._log(f"{name} r={radius} mm on edges at {edge_points}")
        return f"Created {name}."

    def revolve(self, sketch, angle=360, reverse=False, cut=False):
        self._require_part()
        name = self._add("Cut-Revolve" if cut else "Revolve")
        self._log(f"{name} from {sketch}, angle={angle}")
        return f"Created {name}."

    def chamfer(self, distance, edge_points, angle=45):
        self._require_part()
        name = self._add("Chamfer")
        self._log(f"{name} {distance} mm x {angle} deg on edges at {edge_points}")
        return f"Created {name}."

    def shell(self, thickness, face_points, outward=False):
        self._require_part()
        name = self._add("Shell")
        self._log(f"{name} t={thickness} mm, removing faces at {face_points}")
        return f"Created {name}."

    def take_screenshot(self):
        self._require_part()
        return "Screenshots aren't available in mock mode; there's no geometry to show."

    def get_model_info(self):
        self._require_part()
        body = "\n".join(f"  - {f}" for f in self.features)
        return f"Features:\n{body}\n(mock mode: no geometry or mass data)"

    def undo(self, steps=1):
        for _ in range(min(steps, len(self.features) - 4)):
            self._log(f"undo {self.features.pop()}")
        return f"Undid {steps} step(s)."

    def save(self, path):
        path = _resolve_save_path(path, create=False)
        self._log(f"save to {path}")
        return f"(Demo mode: nothing was written.) Would have saved to {path}."
