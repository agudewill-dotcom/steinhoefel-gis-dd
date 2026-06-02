"""
modules/import_gis.py
=====================
GIS file import pipeline for the Steinhöfel GIS Due-Diligence application.

Supports GeoJSON, KML, Shapefile (ZIP), and DXF.  Provides CRS management,
geometry validation, layer classification UI and spatial-join utilities.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree as ET

import geopandas as gpd
import numpy as np
import pandas as pd
import streamlit as st
from pyproj import CRS
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
)
from shapely.ops import unary_union
from shapely.validation import make_valid

from modules.data_model import LAYER_TYPES, create_parcel_uid

# ============================================================================
# Constants
# ============================================================================

_DEFAULT_CRS: Final[str] = "EPSG:25833"
_TARGET_CRS: Final[str] = "EPSG:4326"

# Pre-defined CRS options for the selector.
_CRS_OPTIONS: Final[dict[str, str]] = {
    "EPSG:25833 — ETRS89 / UTM zone 33N (Brandenburg default)": "EPSG:25833",
    "EPSG:25832 — ETRS89 / UTM zone 32N": "EPSG:25832",
    "EPSG:4326 — WGS 84 (lat/lon)": "EPSG:4326",
    "EPSG:32633 — WGS 84 / UTM zone 33N": "EPSG:32633",
    "EPSG:3857 — Web Mercator": "EPSG:3857",
    "Custom EPSG code…": "custom",
}

# KML namespaces.
_KML_NS: Final[dict[str, str]] = {
    "kml": "http://www.opengis.net/kml/2.2",
    "gx": "http://www.google.com/kml/ext/2.2",
}


# ============================================================================
# 1. read_geojson
# ============================================================================

def read_geojson(
    uploaded_file,
    source_crs: str = "EPSG:4326",
) -> gpd.GeoDataFrame:
    """Read a GeoJSON file from a Streamlit ``UploadedFile`` object.

    Parameters
    ----------
    uploaded_file:
        The uploaded file (``BytesIO``-like).
    source_crs:
        CRS to assign if the file does not declare one.

    Returns
    -------
    gpd.GeoDataFrame
        Geometry data reprojected to ``EPSG:4326``.
    """
    try:
        uploaded_file.seek(0)
        gdf = gpd.read_file(uploaded_file, driver="GeoJSON")
    except Exception as exc:
        st.error(f"❌ Failed to read GeoJSON: {exc}")
        raise ValueError(f"GeoJSON read error: {exc}") from exc

    if gdf.crs is None:
        try:
            gdf = gdf.set_crs(source_crs)
            st.info(f"ℹ️ No CRS found in GeoJSON; assumed **{source_crs}**.")
        except Exception as exc:
            st.warning(f"⚠️ Could not set CRS {source_crs}: {exc}")

    if gdf.crs and str(gdf.crs) != _TARGET_CRS:
        try:
            gdf = gdf.to_crs(_TARGET_CRS)
        except Exception as exc:
            st.warning(f"⚠️ CRS transformation failed: {exc}")

    return gdf


# ============================================================================
# 2. read_kml
# ============================================================================

def read_kml(uploaded_file) -> gpd.GeoDataFrame:
    """Read a KML file with fiona/geopandas; fall back to lxml XML parsing.

    Parameters
    ----------
    uploaded_file:
        The uploaded KML file.

    Returns
    -------
    gpd.GeoDataFrame
        Geometry data in ``EPSG:4326``.
    """
    uploaded_file.seek(0)
    raw_bytes = uploaded_file.read()

    # --- Strategy 1: fiona via geopandas ---
    gdf = _try_read_kml_fiona(raw_bytes)
    if gdf is not None and not gdf.empty:
        return _ensure_target_crs(gdf)

    # --- Strategy 2: manual lxml/ElementTree XML parsing ---
    st.info("ℹ️ Fiona KML driver unavailable — falling back to XML parser.")
    gdf = _parse_kml_xml(raw_bytes)
    if gdf is not None and not gdf.empty:
        return _ensure_target_crs(gdf)

    st.error("❌ Could not read KML with any available method.")
    raise ValueError("KML parsing failed.")


def _try_read_kml_fiona(raw_bytes: bytes) -> gpd.GeoDataFrame | None:
    """Attempt to read KML bytes using fiona's KML driver."""
    try:
        import fiona  # noqa: F811
        if "KML" not in fiona.supported_drivers:
            fiona.supported_drivers["KML"] = "r"
    except ImportError:
        pass

    with tempfile.NamedTemporaryFile(suffix=".kml", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = tmp.name

    try:
        gdf = gpd.read_file(tmp_path, driver="KML")
        return gdf
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _parse_kml_xml(raw_bytes: bytes) -> gpd.GeoDataFrame | None:
    """Parse KML XML manually to extract Placemarks as geometries."""
    try:
        # Try lxml first for better namespace handling.
        try:
            from lxml import etree as lxml_ET
            root = lxml_ET.fromstring(raw_bytes)
        except Exception:
            root = ET.fromstring(raw_bytes.decode("utf-8", errors="replace"))

        # Auto-detect namespace.
        ns = ""
        tag = root.tag
        if "}" in tag:
            ns = tag.split("}")[0] + "}"

        records: list[dict[str, Any]] = []

        for pm in root.iter(f"{ns}Placemark"):
            name = ""
            name_el = pm.find(f"{ns}name")
            if name_el is not None and name_el.text:
                name = name_el.text.strip()

            desc = ""
            desc_el = pm.find(f"{ns}description")
            if desc_el is not None and desc_el.text:
                desc = desc_el.text.strip()

            geom = _extract_kml_geometry(pm, ns)
            if geom is not None:
                records.append({
                    "name": name,
                    "description": desc,
                    "geometry": geom,
                })

        if not records:
            return None

        gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
        return gdf
    except Exception as exc:
        st.warning(f"⚠️ KML XML parsing error: {exc}")
        return None


def _extract_kml_geometry(placemark, ns: str):
    """Extract a Shapely geometry from a KML Placemark element."""
    # Point
    point_el = placemark.find(f".//{ns}Point/{ns}coordinates")
    if point_el is not None and point_el.text:
        coords = _parse_kml_coords(point_el.text)
        if coords:
            return Point(coords[0][:2])

    # LineString
    ls_el = placemark.find(f".//{ns}LineString/{ns}coordinates")
    if ls_el is not None and ls_el.text:
        coords = _parse_kml_coords(ls_el.text)
        if len(coords) >= 2:
            return LineString([c[:2] for c in coords])

    # Polygon
    poly_el = placemark.find(f".//{ns}Polygon")
    if poly_el is not None:
        outer_el = poly_el.find(
            f".//{ns}outerBoundaryIs/{ns}LinearRing/{ns}coordinates"
        )
        if outer_el is not None and outer_el.text:
            outer = [c[:2] for c in _parse_kml_coords(outer_el.text)]
            holes: list[list[tuple[float, float]]] = []
            for inner_el in poly_el.findall(
                f".//{ns}innerBoundaryIs/{ns}LinearRing/{ns}coordinates"
            ):
                if inner_el.text:
                    holes.append([c[:2] for c in _parse_kml_coords(inner_el.text)])
            if len(outer) >= 4:
                return Polygon(outer, holes if holes else None)

    # MultiGeometry
    multi_el = placemark.find(f".//{ns}MultiGeometry")
    if multi_el is not None:
        geoms = []
        for child in multi_el:
            # Recursively build a dummy placemark for each sub-element
            dummy = ET.Element("Placemark")
            dummy.append(child)
            g = _extract_kml_geometry(dummy, ns)
            if g is not None:
                geoms.append(g)
        if geoms:
            from shapely.geometry import shape
            from shapely.ops import unary_union
            return unary_union(geoms)

    return None


def _parse_kml_coords(text: str) -> list[tuple[float, ...]]:
    """Parse a KML ``<coordinates>`` text block into a list of tuples."""
    coords = []
    for part in text.strip().split():
        try:
            vals = tuple(float(v) for v in part.split(","))
            coords.append(vals)
        except ValueError:
            continue
    return coords


# ============================================================================
# 3. read_shapefile_zip
# ============================================================================

def read_shapefile_zip(
    uploaded_file,
    source_crs: str = "EPSG:25833",
) -> gpd.GeoDataFrame:
    """Read a zipped Shapefile from a Streamlit ``UploadedFile``.

    Parameters
    ----------
    uploaded_file:
        The uploaded ``.zip`` file.
    source_crs:
        CRS to assume if the ``.prj`` file is missing.

    Returns
    -------
    gpd.GeoDataFrame
        Geometry data reprojected to ``EPSG:4326``.
    """
    uploaded_file.seek(0)
    raw_bytes = uploaded_file.read()

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with zipfile.ZipFile(BytesIO(raw_bytes)) as zf:
                zf.extractall(tmpdir)
        except zipfile.BadZipFile as exc:
            st.error(f"❌ Invalid ZIP archive: {exc}")
            raise ValueError(f"Bad ZIP: {exc}") from exc

        # Find .shp files (may be nested in subdirectories).
        shp_files = list(Path(tmpdir).rglob("*.shp"))
        if not shp_files:
            st.error("❌ No `.shp` file found inside the ZIP archive.")
            raise ValueError("No .shp found in ZIP.")

        if len(shp_files) > 1:
            st.warning(
                f"⚠️ Multiple shapefiles found; using the first: "
                f"`{shp_files[0].name}`"
            )

        shp_path = str(shp_files[0])

        try:
            gdf = gpd.read_file(shp_path)
        except Exception as exc:
            st.error(f"❌ Failed to read Shapefile: {exc}")
            raise ValueError(f"Shapefile read error: {exc}") from exc

    if gdf.crs is None:
        try:
            gdf = gdf.set_crs(source_crs)
            st.info(f"ℹ️ No CRS in Shapefile; assumed **{source_crs}**.")
        except Exception as exc:
            st.warning(f"⚠️ Could not set CRS: {exc}")

    if gdf.crs and str(gdf.crs) != _TARGET_CRS:
        try:
            gdf = gdf.to_crs(_TARGET_CRS)
        except Exception as exc:
            st.warning(f"⚠️ CRS transformation failed: {exc}")

    return gdf


# ============================================================================
# 4. read_dxf
# ============================================================================

def read_dxf(
    uploaded_file,
    source_crs: str = "EPSG:25833",
) -> gpd.GeoDataFrame:
    """Read a DXF file using ``ezdxf`` and convert entities to geometries.

    Supported entities:

    * ``LWPOLYLINE`` (closed → Polygon, open → LineString)
    * ``POLYLINE`` (closed → Polygon, open → LineString)
    * ``LINE`` → LineString
    * ``HATCH`` → Polygon (boundary paths)
    * ``CIRCLE`` → Polygon (buffered point)
    * ``POINT`` → Point
    * ``TEXT`` / ``MTEXT`` → Point with ``text`` attribute

    Parameters
    ----------
    uploaded_file:
        The uploaded ``.dxf`` file.
    source_crs:
        The CRS of the DXF coordinate data.

    Returns
    -------
    gpd.GeoDataFrame
        Geometries reprojected to ``EPSG:4326`` with ``layer_name`` and
        ``text`` columns preserved.
    """
    try:
        import ezdxf
    except ImportError as exc:
        st.error("❌ The `ezdxf` library is required for DXF import.")
        raise ImportError("ezdxf not installed") from exc

    uploaded_file.seek(0)
    raw_bytes = uploaded_file.read()

    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = tmp.name

    try:
        doc = ezdxf.readfile(tmp_path)
    except Exception as exc:
        st.error(f"❌ Failed to read DXF file: {exc}")
        raise ValueError(f"DXF read error: {exc}") from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    msp = doc.modelspace()
    records: list[dict[str, Any]] = []

    for entity in msp:
        try:
            rec = _dxf_entity_to_record(entity)
            if rec is not None:
                records.append(rec)
        except Exception:
            # Skip malformed entities silently.
            continue

    if not records:
        st.warning("⚠️ No convertible geometry entities found in DXF file.")
        return gpd.GeoDataFrame(
            columns=["layer_name", "text", "geometry"],
            geometry="geometry",
            crs=_TARGET_CRS,
        )

    gdf = gpd.GeoDataFrame(records, geometry="geometry")

    try:
        gdf = gdf.set_crs(source_crs)
    except Exception as exc:
        st.warning(f"⚠️ Could not set source CRS {source_crs}: {exc}")

    if gdf.crs and str(gdf.crs) != _TARGET_CRS:
        try:
            gdf = gdf.to_crs(_TARGET_CRS)
        except Exception as exc:
            st.warning(f"⚠️ CRS transformation failed: {exc}")

    return gdf


def _xy(point) -> tuple[float, float]:
    """Extract 2D coordinates from a DXF point (drop Z)."""
    if hasattr(point, "x"):
        return (float(point.x), float(point.y))
    return (float(point[0]), float(point[1]))


def _dxf_entity_to_record(entity) -> dict[str, Any] | None:
    """Convert a single DXF entity to a dict with geometry + metadata."""
    dxftype = entity.dxftype()
    layer = entity.dxf.layer if hasattr(entity.dxf, "layer") else ""
    text_val = ""

    if dxftype == "LWPOLYLINE":
        pts = [_xy(p) for p in entity.get_points(format="xy")]
        if len(pts) < 2:
            return None
        if entity.closed or (len(pts) >= 3 and pts[0] == pts[-1]):
            if len(pts) < 3:
                return None
            geom = Polygon(pts)
        else:
            geom = LineString(pts)

    elif dxftype == "POLYLINE":
        pts = [_xy(v.dxf.location) for v in entity.vertices]
        if len(pts) < 2:
            return None
        is_closed = getattr(entity, "is_closed", False) or (
            len(pts) >= 3 and pts[0] == pts[-1]
        )
        if is_closed and len(pts) >= 3:
            geom = Polygon(pts)
        else:
            geom = LineString(pts)

    elif dxftype == "LINE":
        start = _xy(entity.dxf.start)
        end = _xy(entity.dxf.end)
        geom = LineString([start, end])

    elif dxftype == "HATCH":
        polys: list[Polygon] = []
        try:
            for path in entity.paths:
                if hasattr(path, "vertices"):
                    pts = [_xy(v) for v in path.vertices]
                    if len(pts) >= 3:
                        polys.append(Polygon(pts))
                elif hasattr(path, "edges"):
                    # Edge-path – extract line vertices.
                    pts = []
                    for edge in path.edges:
                        if hasattr(edge, "start"):
                            pts.append(_xy(edge.start))
                        if hasattr(edge, "end"):
                            pts.append(_xy(edge.end))
                    if len(pts) >= 3:
                        polys.append(Polygon(pts))
        except Exception:
            return None
        if not polys:
            return None
        geom = unary_union(polys) if len(polys) > 1 else polys[0]

    elif dxftype == "CIRCLE":
        centre = _xy(entity.dxf.center)
        radius = float(entity.dxf.radius)
        geom = Point(centre).buffer(radius, resolution=32)

    elif dxftype == "POINT":
        geom = Point(_xy(entity.dxf.location))

    elif dxftype in ("TEXT", "MTEXT"):
        if dxftype == "TEXT":
            insert = _xy(entity.dxf.insert)
            text_val = getattr(entity.dxf, "text", "")
        else:
            insert = _xy(entity.dxf.insert)
            text_val = entity.text if hasattr(entity, "text") else ""
            # Strip MTEXT formatting codes.
            text_val = re.sub(r"\\[A-Za-z][^;]*;", "", str(text_val))
            text_val = re.sub(r"[{}]", "", text_val).strip()
        geom = Point(insert)

    else:
        return None  # Unsupported entity type.

    if geom is None or geom.is_empty:
        return None

    return {
        "layer_name": layer,
        "text": text_val,
        "geometry": geom,
    }


# ============================================================================
# 5. import_gis_file
# ============================================================================

def import_gis_file(
    uploaded_file,
    filename: str,
    source_crs: str,
) -> gpd.GeoDataFrame:
    """Dispatch to the correct reader based on file extension.

    Parameters
    ----------
    uploaded_file:
        Streamlit ``UploadedFile``.
    filename:
        Original filename (used for extension detection).
    source_crs:
        CRS string (e.g. ``"EPSG:25833"``).

    Returns
    -------
    gpd.GeoDataFrame
        The imported data with a ``source_file`` column added.
    """
    ext = Path(filename).suffix.lower()

    readers = {
        ".geojson": lambda: read_geojson(uploaded_file, source_crs),
        ".json": lambda: read_geojson(uploaded_file, source_crs),
        ".kml": lambda: read_kml(uploaded_file),
        ".zip": lambda: read_shapefile_zip(uploaded_file, source_crs),
        ".shp.zip": lambda: read_shapefile_zip(uploaded_file, source_crs),
        ".dxf": lambda: read_dxf(uploaded_file, source_crs),
    }

    reader = readers.get(ext)
    if reader is None:
        # Try compound extension (e.g. .shp.zip).
        stem = Path(filename).stem.lower()
        if stem.endswith(".shp"):
            reader = readers[".zip"]

    if reader is None:
        st.error(
            f"❌ Unsupported file format: `{ext}`.  "
            f"Supported: GeoJSON, KML, Shapefile (ZIP), DXF."
        )
        raise ValueError(f"Unsupported format: {ext}")

    gdf = reader()
    gdf["source_file"] = filename
    return gdf


# ============================================================================
# 6. validate_geometries
# ============================================================================

def validate_geometries(
    gdf: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, list[str]]:
    """Fix invalid geometries and collect warnings.

    Uses ``buffer(0)`` and ``make_valid`` to repair geometry issues.

    Parameters
    ----------
    gdf:
        Input GeoDataFrame.

    Returns
    -------
    tuple[gpd.GeoDataFrame, list[str]]
        ``(fixed_gdf, warnings)``
    """
    warnings_list: list[str] = []
    if gdf.empty or "geometry" not in gdf.columns:
        return gdf, warnings_list

    fixed_geoms = []
    for idx, geom in enumerate(gdf.geometry):
        if geom is None or geom.is_empty:
            fixed_geoms.append(geom)
            warnings_list.append(f"Row {idx}: geometry is empty or None.")
            continue

        if not geom.is_valid:
            try:
                repaired = make_valid(geom)
                if repaired.is_empty:
                    repaired = geom.buffer(0)
                fixed_geoms.append(repaired)
                warnings_list.append(
                    f"Row {idx}: repaired invalid geometry "
                    f"(type={geom.geom_type})."
                )
            except Exception as exc:
                try:
                    fixed_geoms.append(geom.buffer(0))
                    warnings_list.append(
                        f"Row {idx}: repaired via buffer(0) (error: {exc})."
                    )
                except Exception:
                    fixed_geoms.append(geom)
                    warnings_list.append(
                        f"Row {idx}: could not repair geometry."
                    )
        else:
            fixed_geoms.append(geom)

    result = gdf.copy()
    result["geometry"] = fixed_geoms
    return result, warnings_list


# ============================================================================
# 7. check_crs_validity
# ============================================================================

def check_crs_validity(gdf: gpd.GeoDataFrame) -> list[str]:
    """Check whether geometries look plausible for EPSG:4326.

    For WGS 84 data, valid latitude is ``[-90, 90]`` and valid longitude
    is ``[-180, 180]``.  Large deviations suggest wrong CRS assignment.

    Parameters
    ----------
    gdf:
        GeoDataFrame expected to be in EPSG:4326.

    Returns
    -------
    list[str]
        Warning messages (empty if all looks fine).
    """
    warnings_list: list[str] = []
    if gdf.empty or "geometry" not in gdf.columns:
        return warnings_list

    try:
        bounds = gdf.total_bounds  # (minx, miny, maxx, maxy)
    except Exception:
        return ["Could not compute geometry bounds."]

    if any(math.isnan(b) for b in bounds):
        warnings_list.append("Geometry bounds contain NaN values.")
        return warnings_list

    minx, miny, maxx, maxy = bounds

    if minx < -180 or maxx > 180:
        warnings_list.append(
            f"Longitude range [{minx:.2f}, {maxx:.2f}] exceeds ±180°.  "
            f"The source CRS may be incorrect."
        )
    if miny < -90 or maxy > 90:
        warnings_list.append(
            f"Latitude range [{miny:.2f}, {maxy:.2f}] exceeds ±90°.  "
            f"The source CRS may be incorrect."
        )

    # Heuristic: if coords are very large they are probably projected metres.
    if abs(minx) > 1_000 or abs(maxx) > 1_000 or abs(miny) > 1_000 or abs(maxy) > 1_000:
        warnings_list.append(
            "Coordinates appear to be in projected metres, not degrees.  "
            "Please verify the source CRS selection."
        )

    # Sanity: for German projects, expect lon ≈ 5–16, lat ≈ 47–56.
    if not warnings_list:
        if not (4.0 <= minx <= 16.5 and 4.0 <= maxx <= 16.5):
            warnings_list.append(
                f"Longitude [{minx:.4f}, {maxx:.4f}] is outside "
                f"typical German range (5°–16° E).  Double-check CRS."
            )
        if not (46.5 <= miny <= 56.0 and 46.5 <= maxy <= 56.0):
            warnings_list.append(
                f"Latitude [{miny:.4f}, {maxy:.4f}] is outside "
                f"typical German range (47°–55° N).  Double-check CRS."
            )

    return warnings_list


# ============================================================================
# 8. render_crs_selector
# ============================================================================

def render_crs_selector() -> str:
    """Render a Streamlit CRS selector widget.

    Returns
    -------
    str
        Selected EPSG string, e.g. ``"EPSG:25833"``.
    """
    options = list(_CRS_OPTIONS.keys())
    selected_label = st.selectbox(
        "🌐 Source Coordinate Reference System (CRS)",
        options=options,
        index=0,
        help=(
            "Select the CRS used in your GIS file.  For most Brandenburg "
            "projects the default EPSG:25833 is correct.  GeoJSON and KML "
            "files typically use EPSG:4326."
        ),
    )

    crs_code = _CRS_OPTIONS[selected_label]

    if crs_code == "custom":
        custom_input = st.text_input(
            "Enter EPSG code (e.g. `25833`)",
            value="25833",
            key="custom_epsg_input",
        )
        custom_input = custom_input.strip()
        if not custom_input:
            st.warning("⚠️ Please enter an EPSG code.")
            return _DEFAULT_CRS

        # Normalise – allow both "EPSG:25833" and "25833".
        if custom_input.upper().startswith("EPSG:"):
            epsg_str = custom_input.upper()
        else:
            epsg_str = f"EPSG:{custom_input}"

        # Validate.
        try:
            CRS.from_user_input(epsg_str)
        except Exception:
            st.error(f"❌ Invalid EPSG code: `{epsg_str}`")
            return _DEFAULT_CRS

        return epsg_str

    return crs_code


# ============================================================================
# 9. render_layer_mapping_ui
# ============================================================================

def render_layer_mapping_ui(
    gdf: gpd.GeoDataFrame,
    layer_types: list[str] | None = None,
) -> dict[str, str]:
    """Display unique layer names and let the user classify each one.

    Parameters
    ----------
    gdf:
        The imported GeoDataFrame (must contain a ``layer_name`` column).
    layer_types:
        Allowed layer types.  Defaults to :data:`LAYER_TYPES`.

    Returns
    -------
    dict[str, str]
        ``{layer_name: layer_type}``  where ``layer_type`` may be
        ``"ignore"`` for layers the user wishes to skip.
    """
    if layer_types is None:
        layer_types = list(LAYER_TYPES)

    classification_options = ["ignore"] + layer_types

    if "layer_name" not in gdf.columns:
        st.warning("⚠️ No `layer_name` column found in GIS data.")
        return {}

    unique_layers = sorted(
        gdf["layer_name"].dropna().unique().tolist(),
        key=lambda s: str(s).lower(),
    )

    if not unique_layers:
        st.info("ℹ️ No distinct layer names found.")
        return {}

    st.markdown("### 🏷️ Layer Classification")
    st.caption(
        f"Found **{len(unique_layers)}** unique layer(s).  "
        f"Assign each to an infrastructure type, or choose **ignore** to skip."
    )

    mapping: dict[str, str] = {}

    # Attempt a naïve auto-guess.
    auto_guesses = _auto_guess_layer_types(unique_layers, layer_types)

    for layer_name in unique_layers:
        # Count features for context.
        count = int((gdf["layer_name"] == layer_name).sum())
        label = f"`{layer_name}` ({count} features)"

        default_type = auto_guesses.get(layer_name, "ignore")
        default_idx = (
            classification_options.index(default_type)
            if default_type in classification_options
            else 0
        )

        selected = st.selectbox(
            label,
            options=classification_options,
            index=default_idx,
            key=f"layer_map_{layer_name}",
        )
        mapping[layer_name] = selected

    return mapping


def _auto_guess_layer_types(
    layer_names: list[str],
    valid_types: list[str],
) -> dict[str, str]:
    """Heuristically guess layer types from DXF / GIS layer names."""
    guesses: dict[str, str] = {}
    _type_keywords: dict[str, list[str]] = {
        "PV plant": ["pv", "solar", "modul", "panel", "anlage"],
        "compensation area": ["kompensation", "ausgleich", "compensation", "cef", "öko"],
        "cable route": ["kabel", "cable", "trasse"],
        "cable tray": ["kabelkanal", "cable tray", "kabelrinne"],
        "access road": ["zuweg", "access", "weg", "road", "straße"],
        "temporary access road": ["temp", "provisor", "temporary"],
        "substation": ["umspann", "substation", "ums"],
        "coupling station": ["koppel", "coupling", "übergabe"],
        "fence": ["zaun", "fence", "einzäunung"],
        "building": ["gebäude", "building", "haus"],
        "security equipment": ["sicherheit", "security", "kamera", "camera"],
        "building plan / zoning plan": ["bebauung", "b-plan", "bplan", "zoning"],
        "parcel boundary": ["grenze", "boundary", "flurstück", "parcel", "kataster"],
        "parcel label": ["label", "beschriftung", "nummer", "text"],
        "other BoP equipment": ["bop", "balance"],
    }

    for layer_name in layer_names:
        ln = layer_name.lower()
        for ltype, keywords in _type_keywords.items():
            if ltype not in valid_types:
                continue
            if any(kw in ln for kw in keywords):
                guesses[layer_name] = ltype
                break

    return guesses


# ============================================================================
# 10. classify_and_split
# ============================================================================

def classify_and_split(
    gdf: gpd.GeoDataFrame,
    layer_mapping: dict[str, str],
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Split a GeoDataFrame into parcel and infrastructure subsets.

    Parameters
    ----------
    gdf:
        The full imported GeoDataFrame.
    layer_mapping:
        ``{layer_name: layer_type}`` from :func:`render_layer_mapping_ui`.

    Returns
    -------
    tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]
        ``(parcel_gdf, infrastructure_gdf)``
    """
    if "layer_name" not in gdf.columns:
        st.warning("⚠️ No `layer_name` column — returning empty splits.")
        empty = gpd.GeoDataFrame(columns=["layer_name", "layer_type", "geometry"])
        return empty.copy(), empty.copy()

    # Assign layer_type column based on mapping.
    gdf = gdf.copy()
    gdf["layer_type"] = gdf["layer_name"].map(layer_mapping).fillna("ignore")

    # Separate ignored features.
    gdf = gdf[gdf["layer_type"] != "ignore"].copy()

    parcel_types = {"parcel boundary", "parcel label", "parcel owner label"}
    mask_parcel = gdf["layer_type"].isin(parcel_types)

    parcel_gdf = gdf[mask_parcel].copy().reset_index(drop=True)
    infra_gdf = gdf[~mask_parcel].copy().reset_index(drop=True)

    return parcel_gdf, infra_gdf


# ============================================================================
# 11. spatial_join_parcels_infrastructure
# ============================================================================

def spatial_join_parcels_infrastructure(
    parcels_gdf: gpd.GeoDataFrame,
    infra_gdf: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Spatial-join infrastructure features to parcels using *intersects*.

    Each infrastructure feature receives a ``related_parcel_uid`` column
    listing the UIDs of parcels it intersects.

    Parameters
    ----------
    parcels_gdf:
        Parcel geometries.  Must contain ``parcel_uid`` and ``geometry``.
    infra_gdf:
        Infrastructure geometries.

    Returns
    -------
    gpd.GeoDataFrame
        The infrastructure GeoDataFrame enriched with
        ``related_parcel_uid`` (semicolon-separated if multiple).
    """
    if parcels_gdf.empty or infra_gdf.empty:
        infra_gdf = infra_gdf.copy()
        if "related_parcel_uid" not in infra_gdf.columns:
            infra_gdf["related_parcel_uid"] = ""
        return infra_gdf

    if "parcel_uid" not in parcels_gdf.columns:
        st.warning("⚠️ Parcels GeoDataFrame has no `parcel_uid` column.")
        infra_gdf = infra_gdf.copy()
        infra_gdf["related_parcel_uid"] = ""
        return infra_gdf

    try:
        joined = gpd.sjoin(
            infra_gdf,
            parcels_gdf[["parcel_uid", "geometry"]],
            how="left",
            predicate="intersects",
        )

        # Aggregate parcel UIDs per infrastructure feature.
        uid_agg = (
            joined.groupby(joined.index)["parcel_uid"]
            .apply(lambda s: "; ".join(s.dropna().unique()))
            .rename("related_parcel_uid")
        )

        result = infra_gdf.copy()
        result["related_parcel_uid"] = result.index.map(uid_agg).fillna("")
        return result

    except Exception as exc:
        st.warning(f"⚠️ Spatial join failed: {exc}")
        infra_gdf = infra_gdf.copy()
        infra_gdf["related_parcel_uid"] = ""
        return infra_gdf


# ============================================================================
# 12. try_extract_parcel_ids_from_gis
# ============================================================================

def try_extract_parcel_ids_from_gis(
    gdf: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Attempt to extract parcel identifiers from GIS attribute columns.

    The function searches for columns whose names suggest parcel identification
    (e.g. containing ``'flur'``, ``'parcel'``, ``'gemarkung'``).  When
    ``TEXT``/``MTEXT`` entities are present (from DXF), it also tries to match
    text labels to nearby polygon geometries.

    Parameters
    ----------
    gdf:
        The imported GeoDataFrame (potentially containing both geometry and
        text-label features).

    Returns
    -------
    gpd.GeoDataFrame
        The input GeoDataFrame with an added ``parcel_uid`` column where
        identification was possible (empty string otherwise).
    """
    gdf = gdf.copy()
    gdf["parcel_uid"] = ""
    gdf["gemarkung"] = ""
    gdf["flur"] = ""
    gdf["flurstuck"] = ""

    # --- Strategy 1: look for attribute columns ---
    col_lower_map = {c.lower(): c for c in gdf.columns}

    gemarkung_col = _find_col(col_lower_map, ["gemarkung", "gemarkungsname", "district"])
    flur_col = _find_col(col_lower_map, ["flurnummer", "flur"])
    flurstuck_col = _find_col(
        col_lower_map,
        ["flurstück", "flurstuck", "flurstücksnummer", "parcel", "parcel_id"],
    )

    if gemarkung_col and flur_col and flurstuck_col:
        for idx in gdf.index:
            try:
                g = str(gdf.at[idx, gemarkung_col]).strip()
                f = str(gdf.at[idx, flur_col]).strip()
                fs = str(gdf.at[idx, flurstuck_col]).strip()
                if g and f and fs:
                    gdf.at[idx, "parcel_uid"] = create_parcel_uid(g, f, fs)
                    gdf.at[idx, "gemarkung"] = g
                    gdf.at[idx, "flur"] = f
                    gdf.at[idx, "flurstuck"] = fs
            except Exception:
                continue
        return gdf

    # --- Strategy 2: parse text labels from DXF TEXT/MTEXT entities ---
    if "text" in gdf.columns:
        text_features = gdf[
            (gdf["text"].astype(str).str.strip() != "")
            & (gdf.geometry.geom_type == "Point")
        ].copy()

        polygon_features = gdf[
            gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        ].copy()

        if not text_features.empty and not polygon_features.empty:
            _match_labels_to_polygons(gdf, text_features, polygon_features)

    return gdf


def _find_col(
    col_lower_map: dict[str, str],
    candidates: list[str],
) -> str | None:
    """Find the first column whose lowered name contains a candidate."""
    for candidate in candidates:
        for cl, original in col_lower_map.items():
            if candidate in cl:
                return original
    return None


# Common German cadastral label pattern:  "Flur 3, 42/1" or "3/42/1" etc.
_PARCEL_LABEL_RE = re.compile(
    r"(?:flur\s*)?(\d+)\s*[,/\-]\s*(\d+(?:/\d+)?)",
    re.IGNORECASE,
)


def _match_labels_to_polygons(
    gdf: gpd.GeoDataFrame,
    text_features: gpd.GeoDataFrame,
    polygon_features: gpd.GeoDataFrame,
) -> None:
    """Match text labels to nearby polygon features (in-place update)."""
    for txt_idx in text_features.index:
        text_val = str(text_features.at[txt_idx, "text"]).strip()
        match = _PARCEL_LABEL_RE.search(text_val)
        if not match:
            continue

        flur_val = match.group(1)
        flurstuck_val = match.group(2)
        text_point = text_features.at[txt_idx, "geometry"]

        # Find the polygon that contains (or is nearest to) the text point.
        best_poly_idx = None
        best_dist = float("inf")

        for poly_idx in polygon_features.index:
            poly_geom = polygon_features.at[poly_idx, "geometry"]
            try:
                if poly_geom.contains(text_point):
                    best_poly_idx = poly_idx
                    break
                dist = poly_geom.distance(text_point)
                if dist < best_dist:
                    best_dist = dist
                    best_poly_idx = poly_idx
            except Exception:
                continue

        if best_poly_idx is not None and best_dist < 0.01:  # ~1 km threshold in degrees
            gdf.at[best_poly_idx, "flur"] = flur_val
            gdf.at[best_poly_idx, "flurstuck"] = flurstuck_val
            gdf.at[best_poly_idx, "parcel_uid"] = create_parcel_uid(
                "", flur_val, flurstuck_val,
            )


# ============================================================================
# Helpers (private)
# ============================================================================

def _ensure_target_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Set CRS to WGS 84 if missing and reproject if necessary."""
    if gdf.crs is None:
        gdf = gdf.set_crs(_TARGET_CRS)
    elif str(gdf.crs) != _TARGET_CRS:
        try:
            gdf = gdf.to_crs(_TARGET_CRS)
        except Exception as exc:
            st.warning(f"⚠️ CRS reprojection failed: {exc}")
    return gdf
