"""Scratch-only ArcPy-stub checks; not an ArcMap integration test."""
import runpy
import sys
import types
from collections import defaultdict


class Point(object):
    def __init__(self, x, y):
        self.X, self.Y = x, y


class Shape(object):
    def __init__(self, x, y):
        self.firstPoint = Point(x, y)

    def projectAs(self, sr, transformation=None):
        return self


class Cursor(object):
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return iter(self.rows)

    def __exit__(self, kind, value, traceback):
        return False


arcpy = types.ModuleType("arcpy")
arcpy.AddMessage = lambda message: None
arcpy.AddWarning = lambda message: None
arcpy.AddError = lambda message: None
arcpy.da = types.SimpleNamespace()

rows = [
    (0, Shape(0, 0), "", 0, 0),
    (1, Shape(10, 0), "old_name", 99, 1000),
    (2, Shape(20, 0), "", 0, 0),
]
arcpy.da.SearchCursor = lambda layer, fields: Cursor(rows)


class Parameter(object):
    def __init__(self, **kwargs):
        self.name = kwargs["name"]
        self.datatype = kwargs["datatype"]
        self.parameterType = kwargs["parameterType"]
        self.direction = kwargs["direction"]
        self.value = None
        self.schema = types.SimpleNamespace(clone=False)
        self.parameterDependencies = []
        self.enabled = True
        self.altered = False

    @property
    def valueAsText(self):
        return str(self.value) if self.value is not None else None


arcpy.Parameter = Parameter
sys.modules["arcpy"] = arcpy
script = runpy.run_path("MU_Node_Builder_ArcMap108.pyt", run_name="logic_test")

incoming = defaultdict(list)
outgoing = defaultdict(list)


def add(grid, pos, pipe):
    grid[script["grid_cell"](pos, 2.0)].append((pos, pipe))


add(incoming, (0, 0), (54, "HS_01a_i", 115.8093, 112.46, 500))
add(outgoing, (0, 0), (52, "HS_01b", 112.46, 111.96, 700))
add(incoming, (10, 0), (61, "A", 100, 99, 500))
add(incoming, (10, 0), (62, "B", 101, 99, 700))
add(outgoing, (20, 0), (63, "C", 100, 98, 900))

calculated, counters = script["calculate_pipe_values"](
    "nodes", incoming, outgoing, 2.0, 2.0, True, False, "EPSG25832", "")
assert calculated[0] == ("HS_01a_i", 112.46, 600), calculated[0]
assert calculated[1] == ("", 0, 0), calculated[1]
assert calculated[2] == ("", 100, 0), calculated[2]
assert counters["junctions_left_blank"] == 1

calculated_alt, counters_alt = script["calculate_pipe_values"](
    "nodes", incoming, outgoing, 2.0, 2.0, False, True, "EPSG25832", "")
assert calculated_alt[1] == ("B", 99, 800), calculated_alt[1]
assert counters_alt["largest_dia_selected"] == 1

params = script["BuildMUNodes"]().getParameterInfo()
assert len(params) == 16, len(params)
assert not any(p.name in ("output_folder", "output_name") for p in params)
assert params[15].parameterType == "Derived"
assert params[15].parameterDependencies == [params[5].name]
assert params[10].value == 2.0 and params[11].value == 2.0
assert params[12].value is True


class MutableCursor(object):
    def __init__(self, existing_rows):
        self.rows = existing_rows

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return False

    def __iter__(self):
        return iter(self.rows)

    def updateRow(self, row):
        pass


mutable = [[0, "old", 50.3, 100, 900], [1, "", 0.0, 0, 0]]
arcpy.da.UpdateCursor = lambda fc, fields: MutableCursor(mutable)
count = script["update_node_attributes"](
    "nodes", {0: ("HS_01a_i", 112.46, 600)}, {1: 99.9})
assert count == 2
assert mutable[0] == [0, "HS_01a_i", 50.3, 112.46, 600]
assert mutable[1] == [1, "", 99.9, 0, 0]
print("Toolbox topology and junction checks passed")
