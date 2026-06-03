"""
modules/online_gis_fetcher.py
=============================
Fetches official ALKIS cadastral parcel geometries from the Brandenburg
INSPIRE WFS and matches them to the Excel DD data.
"""

import requests
import geopandas as gpd
import pandas as pd
import streamlit as st
import io
import re
from pyproj import CRS

WFS_URL = "https://inspire.brandenburg.de/services/cp_alkis_wfs"

# Steinhöfel approximate center (EPSG:4326)
_STEINHOEFEL_LON = 14.17
_STEINHOEFEL_LAT = 52.39


def _fetch_single_tile(minx, miny, maxx, maxy, crs_urn, target_crs, retries=2):
    """Fetch a single WFS tile with retry."""
    params = {
        'service': 'WFS',
        'version': '2.0.0',
        'request': 'GetFeature',
        'typeNames': 'cp:CadastralParcel',
        'bbox': f"{minx},{miny},{maxx},{maxy},{crs_urn}",
    }
    
    for attempt in range(retries + 1):
        try:
            response = requests.get(WFS_URL, params=params, timeout=90)
            response.raise_for_status()
            
            content_str = response.content[:500].decode('utf-8', errors='ignore')
            if '<ows:ExceptionReport' in content_str or '<ExceptionReport' in content_str:
                return gpd.GeoDataFrame()
            
            gdf = gpd.read_file(io.BytesIO(response.content))
            
            if gdf.empty:
                return gdf
                
            if gdf.crs is None:
                gdf.set_crs("EPSG:25833", inplace=True)
                
            if target_crs and str(gdf.crs) != target_crs:
                gdf = gdf.to_crs(target_crs)
                
            return gdf
        except Exception:
            if attempt < retries:
                import time
                time.sleep(2)
                continue
            return gpd.GeoDataFrame()


@st.cache_resource(show_spinner=False, ttl=3600)
def fetch_brandenburg_alkis_parcels(minx, miny, maxx, maxy, crs_urn="urn:ogc:def:crs:EPSG::4326", target_crs="EPSG:4326"):
    """
    Fetches cadastral parcels from Brandenburg ALKIS WFS.
    Splits large areas into a grid of smaller tiles (max ~10km each) to avoid connection resets.
    """
    # Calculate tile size — max 10km per tile in EPSG:25833 (meters)
    MAX_TILE = 10000
    width = maxx - minx
    height = maxy - miny
    
    n_cols = max(1, int(width / MAX_TILE) + 1)
    n_rows = max(1, int(height / MAX_TILE) + 1)
    
    tile_w = width / n_cols
    tile_h = height / n_rows
    
    total_tiles = n_cols * n_rows
    all_gdfs = []
    progress = st.progress(0, text=f"Fetching ALKIS parcels (0/{total_tiles} tiles)...")
    
    for r in range(n_rows):
        for c in range(n_cols):
            tile_idx = r * n_cols + c + 1
            progress.progress(tile_idx / total_tiles, text=f"Fetching ALKIS parcels ({tile_idx}/{total_tiles} tiles)...")
            
            t_minx = minx + c * tile_w
            t_miny = miny + r * tile_h
            t_maxx = minx + (c + 1) * tile_w
            t_maxy = miny + (r + 1) * tile_h
            
            gdf = _fetch_single_tile(t_minx, t_miny, t_maxx, t_maxy, crs_urn, target_crs)
            if not gdf.empty:
                all_gdfs.append(gdf)
    
    progress.empty()
    
    if not all_gdfs:
        st.error("Could not fetch any parcels from the WFS. The service may be temporarily unavailable.")
        return gpd.GeoDataFrame()
    
    # Combine and deduplicate
    combined = pd.concat(all_gdfs, ignore_index=True)
    # Drop duplicate geometries from overlapping tiles
    if 'nationalCadastralReference' in combined.columns:
        combined = combined.drop_duplicates(subset=['nationalCadastralReference'], keep='first')
    elif 'inspireid' in [c.lower() for c in combined.columns]:
        id_col = [c for c in combined.columns if c.lower() == 'inspireid'][0]
        combined = combined.drop_duplicates(subset=[id_col], keep='first')
    
    st.success(f"Fetched {len(combined)} parcels from {total_tiles} tiles.")
    return combined


def detect_parcel_identifier_columns(gdf: gpd.GeoDataFrame) -> dict:
    """
    Inspects columns of the WFS response to find parcel identifier fields.
    """
    col_mapping = {
        'parcel_number': None,
        'flur': None,
        'gemarkung': None,
        'national_cadastral_reference': None
    }
    
    for c in gdf.columns:
        cl = c.lower()
        if 'nationalcadastralreference' in cl or 'inspireid' in cl:
            col_mapping['national_cadastral_reference'] = c
        if cl in ('label', 'flurstück', 'flurstueck', 'flst', 'flurstücksnummer', 'flurstuecksnummer'):
            if not col_mapping['parcel_number']:
                col_mapping['parcel_number'] = c
        if cl in ('flur', 'flurnummer') and 'flurst' not in cl:
            col_mapping['flur'] = c
        if cl in ('gemarkung', 'gemarkungsname', 'district'):
            col_mapping['gemarkung'] = c
            
    return col_mapping


def get_aoi_bounds(uploaded_gdf, buffer_m=500):
    """
    Returns (minx, miny, maxx, maxy, crs_urn) for the WFS request.
    Uses EPSG:25833 to buffer in meters.
    """
    if uploaded_gdf is not None and isinstance(uploaded_gdf, gpd.GeoDataFrame) and not uploaded_gdf.empty:
        temp_gdf = uploaded_gdf.to_crs("EPSG:25833")
        aoi = temp_gdf.unary_union.buffer(buffer_m)
        bounds = aoi.bounds  # shapely returns (minx, miny, maxx, maxy) tuple
        return bounds[0], bounds[1], bounds[2], bounds[3], "urn:ogc:def:crs:EPSG::25833"
    else:
        # Precise bounding box for the Steinhöfel project area:
        # Covers Gemarkungen: Demnitz, Steinhöfel, Heinersdorf, Hasenfelde,
        # Neuendorf im Sande, Arensdorf, Tempelberg
        # Coordinates in EPSG:25833 (meters)
        # SW corner: ~14.05°E, 52.33°N  |  NE corner: ~14.28°E, 52.45°N
        from shapely.geometry import box
        sw = gpd.GeoSeries(gpd.points_from_xy([14.05], [52.33]), crs="EPSG:4326").to_crs("EPSG:25833")
        ne = gpd.GeoSeries(gpd.points_from_xy([14.28], [52.45]), crs="EPSG:4326").to_crs("EPSG:25833")
        sw_b = sw.total_bounds  # [minx, miny, ...]
        ne_b = ne.total_bounds  # [..., maxx, maxy]
        # Add small buffer
        return float(sw_b[0] - 500), float(sw_b[1] - 500), float(ne_b[2] + 500), float(ne_b[3] + 500), "urn:ogc:def:crs:EPSG::25833"


def _parse_national_cadastral_reference(ncr_value: str) -> tuple:
    """
    Attempt to parse a German INSPIRE nationalCadastralReference into components.
    
    Common formats:
      - "urn:adv:oid:DELBB0320800804__00055______" (20-digit Flurstückskennzeichen embedded)
      - "120804005500000" (raw 20-digit key)
      - "Steinhöfel-003-00055/000"
    
    The 20-digit German Flurstückskennzeichen:
      Positions 1-2:   Bundesland (e.g. 12 = Brandenburg)
      Positions 3-6:   Kreis
      Positions 7-10:  Gemarkungsnummer  
      Positions 11-13: Flurnummer
      Positions 14-17: Zähler (numerator)
      Positions 18-20: Nenner (denominator)
    
    Returns (gemarkung_num, flur_num, zaehler, nenner) or (None, None, None, None)
    """
    if not ncr_value or pd.isna(ncr_value):
        return None, None, None, None
    
    val = str(ncr_value).strip()
    
    # Try to extract a 20-digit sequence
    # Remove common prefixes
    clean = re.sub(r'[^0-9_]', '', val.replace('urn:adv:oid:', '').replace('DEL', '').replace('BB', ''))
    digits = re.sub(r'[^0-9]', '', clean)
    
    if len(digits) >= 17:
        # Try 20-digit interpretation
        try:
            # Take last 17+ digits (skip Bundesland + Kreis prefix if present)
            # Positions: gemarkung(4) + flur(3) + zaehler(4-5) + nenner(3-4)
            # This varies, so be flexible
            gemarkung_num = digits[:4] if len(digits) >= 20 else digits[:4]
            flur_num = digits[4:7] if len(digits) >= 20 else digits[4:7]
            zaehler = digits[7:12].lstrip('0') or '0'
            nenner = digits[12:16].lstrip('0') if len(digits) > 12 else ''
            return gemarkung_num, flur_num.lstrip('0') or '0', zaehler, nenner
        except Exception:
            pass
    
    return None, None, None, None


def normalize_online_parcel_identifiers(online_gdf: gpd.GeoDataFrame, excel_df: pd.DataFrame) -> gpd.GeoDataFrame:
    """
    Attempts to match online WFS parcels to Excel data using multiple strategies.
    """
    if online_gdf.empty:
        return online_gdf
    
    # Ensure excel_df has required columns
    required_cols = ['parcel_uid', 'flurstueck', 'flur', 'gemarkung']
    for col in required_cols:
        if col not in excel_df.columns:
            # Can't match without these columns
            online_gdf = online_gdf.copy()
            online_gdf['parcel_uid'] = ''
            online_gdf['match_status'] = 'unmatched'
            online_gdf['match_method'] = 'No Excel columns available'
            return online_gdf
        
    online_gdf = online_gdf.copy()
    mapping = detect_parcel_identifier_columns(online_gdf)
    
    online_gdf['parcel_uid'] = ''
    online_gdf['match_status'] = 'unmatched'
    online_gdf['match_method'] = ''
    
    # Build lookup structures from Excel data
    excel_uids = set(excel_df['parcel_uid'].dropna().unique())
    
    # Build a map: flurstueck -> list of UIDs for fuzzy matching
    fs_to_uids = {}
    for uid in excel_uids:
        parts = uid.split('|')
        if len(parts) == 3:
            fs = parts[2].strip()
            if fs:
                fs_to_uids.setdefault(fs, []).append(uid)
    
    for idx in online_gdf.index:
        matched = False
        
        # Strategy 1: Try National Cadastral Reference parsing
        if mapping['national_cadastral_reference'] and not matched:
            ncr_val = str(online_gdf.at[idx, mapping['national_cadastral_reference']])
            gm_num, fl_num, zaehler, nenner = _parse_national_cadastral_reference(ncr_val)
            
            if zaehler is not None:
                # Build candidate Flurstück: "zaehler" or "zaehler/nenner"
                fs_candidate = zaehler if (not nenner or nenner == '0') else f"{zaehler}/{nenner}"
                
                # Try exact match against Excel UIDs
                candidate_uids = fs_to_uids.get(fs_candidate, [])
                if len(candidate_uids) == 1:
                    online_gdf.at[idx, 'parcel_uid'] = candidate_uids[0]
                    online_gdf.at[idx, 'match_status'] = 'matched'
                    online_gdf.at[idx, 'match_method'] = 'National Cadastral Reference'
                    matched = True
                elif len(candidate_uids) > 1:
                    # If flur also matches, disambiguate
                    if fl_num:
                        refined = [u for u in candidate_uids if u.split('|')[1].strip() == fl_num]
                        if len(refined) == 1:
                            online_gdf.at[idx, 'parcel_uid'] = refined[0]
                            online_gdf.at[idx, 'match_status'] = 'matched'
                            online_gdf.at[idx, 'match_method'] = 'National Cadastral Reference + Flur'
                            matched = True
                        else:
                            online_gdf.at[idx, 'match_status'] = 'ambiguous'
                            online_gdf.at[idx, 'match_method'] = 'Ambiguous NCR'
                            matched = True
        
        if matched:
            continue
        
        # Strategy 2: Try label / parcel_number field for exact Flurstück match
        if mapping['parcel_number'] and not matched:
            val = str(online_gdf.at[idx, mapping['parcel_number']]).strip()
            if val and val != 'nan' and val != 'None':
                candidate_uids = fs_to_uids.get(val, [])
                if len(candidate_uids) == 1:
                    online_gdf.at[idx, 'parcel_uid'] = candidate_uids[0]
                    online_gdf.at[idx, 'match_status'] = 'matched'
                    online_gdf.at[idx, 'match_method'] = 'Label / Flurstück Unique'
                    matched = True
                elif len(candidate_uids) > 1:
                    online_gdf.at[idx, 'match_status'] = 'ambiguous'
                    online_gdf.at[idx, 'match_method'] = 'Ambiguous Flurstück'
                    matched = True
                
    return online_gdf
