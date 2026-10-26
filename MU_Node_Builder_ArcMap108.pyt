# -*- coding: utf-8 -*-
"""ArcMap 10.8 / Python 2.7 toolbox: update digitized MU nodes in place.

Inputs can be chosen from the map Table of Contents or browsed in ArcMap.
The selected MU node shapefile is edited directly after an automatic backup.
Spatial matching uses ETRS89 / UTM 32N (EPSG:25832), but the input geometry
and coordinate system are not changed.
"""

import arcpy
import datetime
import math
import uuid
from collections import defaultdict


OUTPUT_SR_CODE = 25832
OUTPUT_NODE_FIELDS = ("Node_ID", "GL", "Invert_L", "Dia_mm")
NUMBER_TYPES = ("SmallInteger", "Integer", "Single", "Double")
NO_DATA_VALUES = (-99.0, -999.0, -9999.0)


def emit(message, level="info"):
    if level == "warning":
        arcpy.AddWarning(message)
    elif level == "error":
        arcpy.AddError(message)
    else:
        arcpy.AddMessage(message)


def field_named(layer, requested):
    matches = [f for f in arcpy.ListFields(layer)
               if f.name.lower() == str(requested).lower()]
    if len(matches) != 1:
        raise RuntimeError("Field '{0}' not found in '{1}'.".format(
            requested, layer))
    return matches[0]


def require_layer(layer, shape_type, friendly_name):
    desc = arcpy.Describe(layer)
    if desc.shapeType != shape_type:
        raise RuntimeError("{0} must be a {1} feature layer; found {2}.".format(
            friendly_name, shape_type, desc.shapeType))
    sr = desc.spatialReference
    if sr is None or sr.name == "Unknown":
        raise RuntimeError("Define a coordinate system for '{0}' before running. "
                           "A map display projection is not enough.".format(
                               friendly_name))
    selected = getattr(desc, "FIDSet", None)
    if selected is not None and str(selected).strip():
        raise RuntimeError("Clear selected features in '{0}' first. "
                           "The toolbox must process ALL features.".format(
                               friendly_name))
    return sr


def choose_transformation(source_sr, target_sr):
    if source_sr.GCS.factoryCode == target_sr.GCS.factoryCode:
        return ""
    choices = arcpy.ListTransformations(source_sr, target_sr)
    if not choices:
        raise RuntimeError("No geographic/datum transformation available from "
                           "{0} to {1}.".format(source_sr.name, target_sr.name))
    emit("Datum transformation: {0}".format(choices[0]))
    return choices[0]


def to_metric_geometry(shape, sr, transformation):
    if shape is None:
        return None
    if transformation:
        return shape.projectAs(sr, transformation)
    return shape.projectAs(sr)


def xy(point):
    return (point.X, point.Y) if point is not None else None


def number(value):
    if value is None:
        return None
    try:
        result = float(str(value).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def level(value, reject_zero=False):
    result = number(value)
    if result is None or result in NO_DATA_VALUES:
        return None
    if reject_zero and abs(result) < 0.0000001:
        return None
    return result


def dia_value(value):
    result = number(value)
    return result if result is not None and result > 0 else None


def zero_or_null(value):
    if value is None:
        return True
    n = number(value)
    return n is not None and abs(n) < 0.0000001


def empty_text(value):
    return value is None or not str(value).strip()


def grid_cell(coords, grid_size):
    return (int(math.floor(coords[0] / grid_size)),
            int(math.floor(coords[1] / grid_size)))


def candidates_near(grid, point_xy, distance):
    gx, gy = grid_cell(point_xy, distance)
    result = []
    max_sq = distance * distance
    for cx in (gx - 1, gx, gx + 1):
        for cy in (gy - 1, gy, gy + 1):
            for matched_xy, record in grid.get((cx, cy), []):
                dx = point_xy[0] - matched_xy[0]
                dy = point_xy[1] - matched_xy[1]
                if dx * dx + dy * dy <= max_sq:
                    result.append(record)
    return result


def ensure_node_fields(feature_class):
    """Add missing model fields; protect existing incompatible field types."""
    specifications = (
        ("Node_ID", "TEXT", 64),
        ("GL", "DOUBLE", None),
        ("Invert_L", "DOUBLE", None),
        ("Dia_mm", "LONG", None)
    )
    for name, add_type, size in specifications:
        matches = [f for f in arcpy.ListFields(feature_class)
                   if f.name.lower() == name.lower()]
        if not matches:
            if size:
                arcpy.AddField_management(feature_class, name, add_type,
                                          field_length=size)
            else:
                arcpy.AddField_management(feature_class, name, add_type)
            continue
        field = matches[0]
        if name == "Node_ID" and field.type != "String":
            raise RuntimeError("Existing Node_ID must be a text field.")
        if name in ("GL", "Invert_L") and field.type not in ("Double", "Single"):
            raise RuntimeError("Existing {0} must be Float or Double, so decimal "
                               "elevations are not truncated.".format(name))
        if name == "Dia_mm" and field.type not in NUMBER_TYPES:
            raise RuntimeError("Existing Dia_mm must be numeric.")


def build_pipe_grids(pipes, branch_field, zin_field, zout_field, dia_field,
                     work_sr, transform, tolerance, first_inflow):
    upstream_ends = defaultdict(list)
    downstream_starts = defaultdict(list)
    total = 0
    skipped = 0
    fields = ["OID@", "SHAPE@", branch_field, zin_field, zout_field, dia_field]
    with arcpy.da.SearchCursor(pipes, fields) as cursor:
        for oid, geom, branch, zin, zout, diameter in cursor:
            total += 1
            geom = to_metric_geometry(geom, work_sr, transform)
            if geom is None or geom.firstPoint is None or geom.lastPoint is None:
                skipped += 1
                continue
            start = xy(geom.firstPoint)
            end = xy(geom.lastPoint)
            in_end, out_end = (start, end) if first_inflow else (end, start)
            pipe = (oid, branch, level(zin), level(zout), dia_value(diameter))
            # Pipe's outflow endpoint can supply the node's 'previous' pipe.
            upstream_ends[grid_cell(out_end, tolerance)].append((out_end, pipe))
            # Pipe's inflow endpoint is used to cross-check the next pipe.
            downstream_starts[grid_cell(in_end, tolerance)].append((in_end, pipe))
    emit("Read {0} pipes; missing geometries: {1}.".format(total, skipped))
    return upstream_ends, downstream_starts


def choose_multi_inlet(incoming):
    """When not skipping a multi-inlet, use the UNIQUE largest diameter."""
    valid = [p for p in incoming if p[4] is not None]
    if not valid:
        return None
    largest = max(p[4] for p in valid)
    winners = [p for p in valid if abs(p[4] - largest) < 0.000001]
    return winners[0] if len(winners) == 1 else None


def calculate_pipe_values(node_fc, pipe_ends, pipe_starts, spatial_tol,
                          invert_tol, skip_multi, overwrite, work_sr,
                          node_transform):
    result = {}
    counters = defaultdict(int)
    examples = []
    field_list = ["OID@", "SHAPE@", "Node_ID", "Invert_L", "Dia_mm"]
    with arcpy.da.SearchCursor(node_fc, field_list) as cursor:
        for oid, geom, old_id, old_invert, old_dia in cursor:
            geom = to_metric_geometry(geom, work_sr, node_transform)
            if geom is None or geom.firstPoint is None:
                counters["no_geometry"] += 1
                continue
            position = xy(geom.firstPoint)
            incoming = candidates_near(pipe_ends, position, spatial_tol)
            outgoing = candidates_near(pipe_starts, position, spatial_tol)
            incoming_count = len(incoming)

            if incoming_count >= 2 and skip_multi:
                # This explicitly clears pipe-derived attributes at junctions,
                # even when copied input nodes already had values there.
                result[oid] = ("", 0.0, 0)
                counters["junctions_left_blank"] += 1
                continue

            if incoming_count == 1:
                previous = incoming[0]
            elif incoming_count > 1:
                previous = choose_multi_inlet(incoming)
                if previous is None:
                    counters["unresolved_multi_inlet"] += 1
                    result[oid] = ("", 0.0, 0)
                    continue
                counters["largest_dia_selected"] += 1
            else:
                previous = None
                counters["no_incoming"] += 1

            following = outgoing[0] if len(outgoing) == 1 else None
            node_id = old_id if not empty_text(old_id) else ""
            invert = old_invert if old_invert is not None else 0.0
            model_dia = old_dia if old_dia is not None else 0

            if previous is not None:
                pipe_oid, branch, zin, zout, diameter = previous
                if len(examples) < 15:
                    examples.append((oid, pipe_oid, branch))
                branch_name = str(branch).strip() if branch is not None else ""
                if (not empty_text(node_id) and branch_name and
                        str(node_id).strip() != branch_name and not overwrite):
                    counters["existing_id_disagrees"] += 1
                    # Preserve a manually filled, inconsistent record intact.
                    result[oid] = (node_id, invert, model_dia)
                    continue
                if branch_name and (overwrite or empty_text(node_id)):
                    node_id = branch_name
                if diameter is not None and (overwrite or zero_or_null(model_dia)):
                    new_dia = diameter + 100.0
                    if abs(new_dia - round(new_dia)) < 0.000001:
                        model_dia = int(round(new_dia))
                    else:
                        counters["noninteger_diameter"] += 1
            else:
                zout = None

            next_zin = following[2] if following else None
            proposed_invert = None
            if zout is not None and next_zin is not None:
                if abs(zout - next_zin) <= invert_tol:
                    proposed_invert = zout
                else:
                    counters["invert_disagreement"] += 1
            elif zout is not None:
                proposed_invert = zout
            elif next_zin is not None:
                proposed_invert = next_zin
                counters["used_next_zin"] += 1

            if proposed_invert is not None and (overwrite or zero_or_null(invert)):
                invert = proposed_invert
            result[oid] = (node_id, invert, model_dia)

    emit("Pipe topology: {0} target nodes inspected.".format(len(result)))
    for key in sorted(counters):
        emit("  {0}: {1}".format(key, counters[key]))
    for node_oid, pipe_oid, branch in examples:
        emit("  sample MU FID {0} -> pipe FID {1}, Branch {2}".format(
            node_oid, pipe_oid, branch))
    return result, counters


def survey_elevations(survey, survey_field, node_fc, work_sr,
                      survey_transform, node_transform, tolerance):
    """Find unambiguous one-to-one survey point matches (any valid z > 0)."""
    survey_grid = defaultdict(list)
    with arcpy.da.SearchCursor(survey, ["OID@", "SHAPE@", survey_field]) as cur:
        for oid, shape, raw_z in cur:
            z = level(raw_z, reject_zero=True)
            if z is None:
                continue
            geom = to_metric_geometry(shape, work_sr, survey_transform)
            if geom is None or geom.firstPoint is None:
                continue
            coords = xy(geom.firstPoint)
            survey_grid[grid_cell(coords, tolerance)].append((coords, (oid, z)))

    proposals = {}
    claimant_count = defaultdict(int)
    ambiguous = 0
    with arcpy.da.SearchCursor(node_fc, ["OID@", "SHAPE@"]) as cur:
        for node_oid, geom in cur:
            geom = to_metric_geometry(geom, work_sr, node_transform)
            if geom is None or geom.firstPoint is None:
                continue
            found = candidates_near(survey_grid, xy(geom.firstPoint), tolerance)
            if len(found) == 1:
                survey_oid, z = found[0]
                proposals[node_oid] = (survey_oid, z)
                claimant_count[survey_oid] += 1
            elif len(found) > 1:
                ambiguous += 1

    values = {}
    reused = 0
    for node_oid, (survey_oid, z) in proposals.items():
        if claimant_count[survey_oid] == 1:
            values[node_oid] = z
        else:
            reused += 1
    emit("Survey GL assignments: {0}; ambiguous survey neighborhoods: {1}; "
         "shared source points: {2}.".format(len(values), ambiguous, reused))
    return values


def mesh_elevations(mesh, bathy_field, node_fc, eligible_oids,
                    mesh_sr, node_sr):
    """Fill remaining GL from containing elements; reject conflicting edges."""
    if not eligible_oids:
        return {}
    temp_fc = "in_memory\\mu_mesh_{0}".format(uuid.uuid4().hex[:12])
    old_transformation = arcpy.env.geographicTransformations
    matches = defaultdict(list)
    invalid = set()
    try:
        transformation = choose_transformation(mesh_sr, node_sr)
        if transformation:
            arcpy.env.geographicTransformations = transformation
        arcpy.SpatialJoin_analysis(node_fc, mesh, temp_fc,
                                   "JOIN_ONE_TO_MANY", "KEEP_ALL", "#", "INTERSECT")
        join_field = field_named(temp_fc, bathy_field).name
        with arcpy.da.SearchCursor(temp_fc,
                                   ["TARGET_FID", "JOIN_FID", join_field]) as cur:
            for target_oid, mesh_oid, raw_z in cur:
                if target_oid not in eligible_oids:
                    continue
                if mesh_oid is None or mesh_oid == -1:
                    continue
                z = level(raw_z, reject_zero=True)
                if z is None:
                    invalid.add(target_oid)
                else:
                    matches[target_oid].append(z)
    finally:
        arcpy.env.geographicTransformations = old_transformation
        if arcpy.Exists(temp_fc):
            arcpy.Delete_management(temp_fc)

    assigned = {}
    conflicting = 0
    no_mesh = 0
    invalid_count = 0
    for oid in eligible_oids:
        z_values = matches.get(oid, [])
        if oid in invalid:
            invalid_count += 1
        elif not z_values:
            no_mesh += 1
        elif max(z_values) - min(z_values) > 0.001:
            conflicting += 1
        else:
            assigned[oid] = z_values[0]
    emit("Mesh Bathymetry GL assignments: {0}; no polygon: {1}; "
         "conflicting boundaries: {2}; invalid Bathymetry: {3}.".format(
             len(assigned), no_mesh, conflicting, invalid_count))
    return assigned


def update_node_attributes(node_fc, pipe_values, gl_values):
    count = 0
    with arcpy.da.UpdateCursor(node_fc,
                               ["OID@", "Node_ID", "GL", "Invert_L", "Dia_mm"]) as cur:
        for row in cur:
            oid = row[0]
            if oid in pipe_values:
                pid, inv, dia = pipe_values[oid]
                row[1] = pid
                row[3] = inv
                row[4] = dia
            # Keep manually entered GL when no usable source elevation exists.
            if oid in gl_values:
                row[2] = gl_values[oid]
            cur.updateRow(row)
            count += 1
    return count


def make_backup(node_fc):
    """Back up the original shapefile (including its existing attributes)."""
    path = arcpy.Describe(node_fc).catalogPath
    if not path or not path.lower().endswith(".shp"):
        raise RuntimeError("Digitized MU nodes must be a shapefile (.shp) "
                           "for safe in-place update and backup.")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = path[:-4] + "_backup_" + timestamp
    backup = stem + ".shp"
    suffix = 1
    while arcpy.Exists(backup):
        backup = "{0}_{1}.shp".format(stem, suffix)
        suffix += 1
    arcpy.Copy_management(path, backup)
    emit("Original MU nodes backed up to: " + backup, "warning")
    return backup


class Toolbox(object):
    def __init__(self):
        self.label = "MU Node Attribute Updater (ArcMap 10.8)"
        self.alias = "munodebuilder"
        self.tools = [BuildMUNodes]


class BuildMUNodes(object):
    def __init__(self):
        self.label = "Update Digitized MU Nodes In Place"
        self.description = ("Updates the selected MU node SHAPEFILE directly "
                            "using pipe topology, optional survey levels, "
                            "and mesh Bathymetry. A dated backup is created. "
                            "Coordinates and source projection are preserved; "
                            "matching uses EPSG:25832 in metres.")
        self.canRunInBackground = False

    def getParameterInfo(self):
        definitions = [
            ("Pipe polylines", "pipes", "GPFeatureLayer", "Required"),            # 0
            ("Pipe Branch field (for Node_ID)", "branch", "Field", "Required"),  # 1
            ("Pipe Z_Inflow field", "zin", "Field", "Required"),             # 2
            ("Pipe Z_Outflow field", "zout", "Field", "Required"),           # 3
            ("Pipe diameter field (mm)", "pipe_dia", "Field", "Required"),  # 4
            ("Digitized MU node points", "nodes", "GPFeatureLayer", "Required"), # 5
            ("Mesh element polygons", "mesh", "GPFeatureLayer", "Required"),     # 6
            ("Mesh Bathymetry field", "bathy", "Field", "Required"),       # 7
            ("CAD / DWG survey points (optional)", "survey", "GPFeatureLayer", "Optional"), # 8
            ("Survey elevation field", "survey_z", "Field", "Optional"),  # 9
            ("Spatial endpoint and survey match tolerance (m)", "spatial_tol", "GPDouble", "Required"), # 10
            ("Inflow/outflow elevation agreement tolerance (m)", "invert_tol", "GPDouble", "Required"), # 11
            ("Leave multi-inlet junction pipe fields blank", "skip_multi", "GPBoolean", "Required"), # 12
            ("Pipe first vertex represents inflow", "first_inflow", "GPBoolean", "Required"), # 13
            ("Replace already populated pipe-derived fields", "overwrite", "GPBoolean", "Required"), # 14
            ("Updated input MU node shapefile", "updated_nodes", "GPFeatureLayer", "Derived") # 15
        ]
        params = []
        for display, name, datatype, requirement in definitions:
            direction = "Output" if requirement == "Derived" else "Input"
            params.append(arcpy.Parameter(displayName=display, name=name,
                                          datatype=datatype, parameterType=requirement,
                                          direction=direction))
        for idx in (1, 2, 3, 4):
            params[idx].parameterDependencies = [params[0].name]
        params[7].parameterDependencies = [params[6].name]
        params[9].parameterDependencies = [params[8].name]
        params[15].parameterDependencies = [params[5].name]
        params[15].schema.clone = True
        for idx, value in ((1, "Branch"), (2, "Z_Inflow"), (3, "Z_Outflow"),
                           (4, "Dia_mm"), (7, "Bathymetry"), (9, "Elevation"),
                           (10, 2.0), (11, 2.0), (12, True), (13, True),
                           (14, False)):
            params[idx].value = value
        params[9].enabled = False

        for i in (0, 5, 6, 8):
            params[i].category = "1. Input layers"
        for i in (1, 2, 3, 4, 7, 9):
            params[i].category = "2. Field mapping"
        for i in (10, 11, 12, 13, 14):
            params[i].category = "3. Matching and junction options"
        return params

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        survey_on = bool(parameters[8].valueAsText)
        parameters[9].enabled = survey_on
        if not survey_on:
            parameters[9].value = None
        elif not parameters[9].valueAsText and not parameters[9].altered:
            parameters[9].value = "Elevation"

        for fld_idx, source_idx, preferred in (
                (1, 0, "Branch"), (2, 0, "Z_Inflow"),
                (3, 0, "Z_Outflow"), (4, 0, "Dia_mm"),
                (7, 6, "Bathymetry"), (9, 8, "Elevation")):
            if parameters[source_idx].valueAsText and not parameters[fld_idx].altered:
                try:
                    fields = arcpy.ListFields(parameters[source_idx].valueAsText)
                    for f in fields:
                        if f.name.lower() == preferred.lower():
                            parameters[fld_idx].value = f.name
                            break
                except Exception:
                    pass

        parameters[15].value = parameters[5].value
        return

    def updateMessages(self, parameters):
        for idx, shape_type in ((0, "Polyline"), (5, "Point"),
                                (6, "Polygon"), (8, "Point")):
            source = parameters[idx].valueAsText
            if source:
                try:
                    actual = arcpy.Describe(source).shapeType
                    if actual != shape_type:
                        parameters[idx].setErrorMessage("Choose a {0} feature "
                            "layer. For DWG, expand the group and choose its Point sublayer."
                            .format(shape_type))
                except Exception:
                    pass

        if parameters[8].valueAsText and not parameters[9].valueAsText:
            parameters[9].setErrorMessage("Choose the survey elevation field.")
        for idx in (10, 11):
            v = number(parameters[idx].valueAsText)
            if v is not None and (v <= 0 if idx == 10 else v < 0):
                parameters[idx].setErrorMessage("Enter a valid distance in meters.")
        node_source = parameters[5].valueAsText
        if node_source:
            try:
                path = arcpy.Describe(node_source).catalogPath
                if not path.lower().endswith(".shp"):
                    parameters[5].setErrorMessage("Choose a digitized point "
                        "shapefile (.shp), which this tool updates directly.")
                else:
                    parameters[5].setWarningMessage("The selected shapefile "
                        "will be UPDATED IN PLACE. An automatic dated "
                        "backup will be created beside it.")
            except Exception:
                pass
        return

    def execute(self, parameters, messages):
        pipes = parameters[0].valueAsText
        pipe_branch = parameters[1].valueAsText
        pipe_zin = parameters[2].valueAsText
        pipe_zout = parameters[3].valueAsText
        pipe_dia = parameters[4].valueAsText
        nodes = parameters[5].valueAsText
        mesh = parameters[6].valueAsText
        bathy = parameters[7].valueAsText
        survey = parameters[8].valueAsText
        survey_z = parameters[9].valueAsText if survey else None
        spatial_tol = float(parameters[10].valueAsText)
        invert_tol = float(parameters[11].valueAsText)
        skip_multi = parameters[12].valueAsText.lower() == "true"
        first_inflow = parameters[13].valueAsText.lower() == "true"
        overwrite = parameters[14].valueAsText.lower() == "true"

        if spatial_tol <= 0 or invert_tol < 0:
            raise RuntimeError("Spatial tolerance must be positive; invert "
                               "agreement tolerance cannot be negative.")

        node_sr = require_layer(nodes, "Point", "MU nodes")
        if not arcpy.Describe(nodes).catalogPath.lower().endswith(".shp"):
            raise RuntimeError("Input MU nodes must be a .shp file.")
        pipe_sr = require_layer(pipes, "Polyline", "Pipe network")
        mesh_sr = require_layer(mesh, "Polygon", "Bathymetry mesh")
        for fname in (pipe_branch, pipe_zin, pipe_zout, pipe_dia):
            field_named(pipes, fname)
        field_named(mesh, bathy)
        if survey:
            survey_sr = require_layer(survey, "Point", "CAD survey points")
            field_named(survey, survey_z)
        else:
            survey_sr = None
            emit("No CAD survey selected: GL will come from mesh Bathymetry.")

        work_sr = arcpy.SpatialReference(OUTPUT_SR_CODE)
        node_transform = choose_transformation(node_sr, work_sr)
        pipe_transform = choose_transformation(pipe_sr, work_sr)
        survey_transform = choose_transformation(survey_sr, work_sr) if survey else ""

        backup = make_backup(nodes)
        ensure_node_fields(nodes)
        emit("UPDATING selected MU node shapefile IN PLACE: " +
             arcpy.Describe(nodes).catalogPath, "warning")

        incoming_grid, outgoing_grid = build_pipe_grids(
            pipes, pipe_branch, pipe_zin, pipe_zout, pipe_dia,
            work_sr, pipe_transform, spatial_tol, first_inflow)
        pipe_updates, pipe_stats = calculate_pipe_values(
            nodes, incoming_grid, outgoing_grid, spatial_tol,
            invert_tol, skip_multi, overwrite, work_sr, node_transform)

        all_node_oids = set()
        with arcpy.da.SearchCursor(nodes, ["OID@"]) as cur:
            for row in cur:
                all_node_oids.add(row[0])
        gl_updates = {}
        if survey:
            gl_updates = survey_elevations(
                survey, survey_z, nodes, work_sr, survey_transform,
                node_transform, spatial_tol)
        left_for_mesh = all_node_oids.difference(gl_updates)
        mesh_values = mesh_elevations(mesh, bathy, nodes, left_for_mesh,
                                      mesh_sr, node_sr)
        gl_updates.update(mesh_values)

        completed = update_node_attributes(nodes, pipe_updates, gl_updates)
        emit("COMPLETE: {0} existing MU nodes updated; shapefile projection "
             "and geometry unchanged.".format(completed))
        emit("Ground levels assigned from survey / mesh: {0}; nodes without "
             "a new GL source: {1} (existing GL retained).".format(
                 len(gl_updates), len(all_node_oids) - len(gl_updates)))
        emit("Backup available at: " + backup)
        if skip_multi:
            emit("Multi-inlet junctions intentionally left blank for "
                 "Node_ID, Invert_L, and Dia_mm: {0}.".format(
                     pipe_stats["junctions_left_blank"]))
        else:
            emit("For multi-inlet nodes, the UNIQUE largest-diameter incoming "
                 "pipe is used; ties remain blank.")
        parameters[15].value = nodes
        return
