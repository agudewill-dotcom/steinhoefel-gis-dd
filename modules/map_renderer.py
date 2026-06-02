"""
modules/map_renderer.py
========================
Simple map renderer: highlights Excel parcels on a map.
Uses vectorized GeoDataFrame operations for speed.
"""

import folium
import geopandas as gpd
import pandas as pd
import hashlib
import numpy as np

CATEGORY_COLORS = {
    'pv_plant': '#FFD700',
    'cable': '#FF4500',
    'access_road': '#8B4513',
    'compensation_area': '#228B22',
    'substation': '#4169E1',
    'other': '#9370DB',
}

STATUS_COLORS = {
    'secured': '#27AE60',
    'in_progress': '#F39C12',
    'unsecured': '#E74C3C',
    'partly_secured_or_unclear': '#E67E22',
    'unclear': '#BDC3C7',
}

COLOR_MODES = {
    'By Category': 'category',
    'By Secured Status': 'status',
    'By Owner': 'owner',
}


def _hash_to_color(value: str) -> str:
    if not value or pd.isna(value) or value == 'nan':
        return '#CCCCCC'
    palette = [
        '#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231',
        '#911eb4', '#46f0f0', '#f032e6', '#bcf60c', '#fabebe',
        '#008080', '#e6beff', '#9a6324', '#800000', '#808000',
    ]
    h = int(hashlib.md5(str(value).encode('utf-8')).hexdigest(), 16)
    return palette[h % len(palette)]


def _compute_color_column(merged: gpd.GeoDataFrame, color_mode: str) -> pd.Series:
    """Vectorized color computation — no row-by-row loop."""
    has_data = merged['secured_status'].notna() if 'secured_status' in merged.columns else pd.Series(False, index=merged.index)
    colors = pd.Series('#CCCCCC', index=merged.index)

    if color_mode == 'category' and 'category' in merged.columns:
        colors = merged['category'].map(CATEGORY_COLORS).fillna('#9370DB')
    elif color_mode == 'status' and 'secured_status' in merged.columns:
        colors = merged['secured_status'].map(STATUS_COLORS).fillna('#BDC3C7')
    elif color_mode == 'owner' and 'owner_name' in merged.columns:
        colors = merged['owner_name'].apply(_hash_to_color)

    # Grey out parcels with no Excel data
    colors[~has_data] = '#CCCCCC'
    return colors


def _force_2d(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Strip Z coordinates from all geometries."""
    try:
        import shapely
        gdf = gdf.copy()
        gdf['geometry'] = gdf['geometry'].apply(
            lambda g: shapely.force_2d(g) if g is not None and g.has_z else g
        )
    except (ImportError, Exception):
        pass
    return gdf


def _clean(val):
    if pd.isna(val) or str(val).strip() in ('', 'nan', 'None'):
        return '-'
    return str(val).strip()


def render_map(df: pd.DataFrame, parcels_gdf=None, infra_gdf=None, color_mode='category') -> folium.Map:
    """Render a simple map highlighting Excel parcels."""

    # Determine map center
    center_lat, center_lon = 52.39, 14.17
    if parcels_gdf is not None and not parcels_gdf.empty:
        try:
            bounds = parcels_gdf.total_bounds
            center_lat = (bounds[1] + bounds[3]) / 2
            center_lon = (bounds[0] + bounds[2]) / 2
        except Exception:
            pass

    m = folium.Map(location=[center_lat, center_lon], zoom_start=13, control_scale=True)

    # --- Add parcels ---
    if parcels_gdf is not None and not parcels_gdf.empty and 'parcel_uid' in parcels_gdf.columns:
        df_dedup = df.drop_duplicates(subset=['parcel_uid'], keep='first') if 'parcel_uid' in df.columns else df

        # Merge: drop overlap to avoid _x/_y columns
        overlap = set(parcels_gdf.columns) & set(df_dedup.columns) - {'parcel_uid', 'geometry'}
        merge_gdf = parcels_gdf.drop(columns=list(overlap), errors='ignore')
        merged = merge_gdf.merge(df_dedup, on='parcel_uid', how='left')

        # Ensure WGS84
        if hasattr(merged, 'crs') and merged.crs is not None and str(merged.crs) != 'EPSG:4326':
            merged = merged.to_crs('EPSG:4326')

        # Strip Z coordinates
        merged = _force_2d(merged)

        # Drop empty geometries
        merged = merged[merged.geometry.notna() & ~merged.geometry.is_empty].copy()

        if not merged.empty:
            # Compute colors vectorized
            merged['_color'] = _compute_color_column(merged, color_mode)

            # Prepare display columns
            display_cols = ['parcel_uid', 'flurstueck', 'owner_name', 'secured_status',
                            'category', 'contract_type_raw', 'easement_type',
                            'public_private', 'bauabschnitt']
            for c in display_cols:
                if c not in merged.columns:
                    merged[c] = '-'
                else:
                    merged[c] = merged[c].fillna('-').astype(str).replace('nan', '-')

            # Use folium.GeoJson with the whole GeoDataFrame at once (fast!)
            folium.GeoJson(
                merged[['geometry', '_color'] + display_cols],
                name='Parcels',
                style_function=lambda feature: {
                    'fillColor': feature['properties']['_color'],
                    'color': '#333333',
                    'weight': 1,
                    'fillOpacity': 0.55,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=['flurstueck', 'owner_name', 'secured_status'],
                    aliases=['Flurstück:', 'Owner:', 'Status:'],
                    sticky=True,
                ),
                popup=folium.GeoJsonPopup(
                    fields=['parcel_uid', 'category', 'owner_name', 'secured_status',
                            'contract_type_raw', 'easement_type', 'public_private',
                            'bauabschnitt'],
                    aliases=['Parcel UID', 'Category', 'Owner', 'Secured',
                             'Contract', 'Easement', 'Public/Private', 'Bauabschnitt'],
                    max_width=400,
                ),
                show=True,
            ).add_to(m)

            # Fit map to parcel bounds
            try:
                bounds = merged.total_bounds  # [minx, miny, maxx, maxy]
                m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
            except Exception:
                pass

    # --- Add infrastructure ---
    if infra_gdf is not None and not infra_gdf.empty:
        infra = infra_gdf.copy()
        if hasattr(infra, 'crs') and infra.crs is not None and str(infra.crs) != 'EPSG:4326':
            infra = infra.to_crs('EPSG:4326')
        infra = _force_2d(infra)
        infra = infra[infra.geometry.notna() & ~infra.geometry.is_empty]

        if not infra.empty:
            for c in ['layer_name', 'text']:
                if c not in infra.columns:
                    infra[c] = ''
                else:
                    infra[c] = infra[c].fillna('').astype(str)

            folium.GeoJson(
                infra[['geometry', 'layer_name', 'text']],
                name='Infrastructure',
                style_function=lambda x: {'color': '#FF4500', 'weight': 2, 'fillOpacity': 0.3},
                tooltip=folium.GeoJsonTooltip(fields=['layer_name', 'text'], aliases=['Layer:', 'Text:']),
                show=True,
            ).add_to(m)

    # --- Legend (with explicit dark text) ---
    if color_mode == 'category':
        legend_items = [(k.replace('_', ' ').title(), v) for k, v in CATEGORY_COLORS.items()]
        title = 'Category'
    elif color_mode == 'status':
        legend_items = [(k.replace('_', ' ').title(), v) for k, v in STATUS_COLORS.items()]
        title = 'Secured Status'
    else:
        legend_items = []
        title = ''

    if legend_items:
        html = (
            '<div style="position:fixed; bottom:30px; left:30px; z-index:9999; '
            'background:white; padding:12px 14px; border:1px solid #999; border-radius:5px; '
            'font-size:12px; font-family:sans-serif; box-shadow:2px 2px 6px rgba(0,0,0,0.3); '
            'color:#000000 !important;">'
        )
        html += f'<b style="color:#000000;">{title}</b><br>'
        for label, color in legend_items:
            html += (
                f'<div style="margin-top:4px; color:#000000;">'
                f'<i style="background:{color}; width:14px; height:14px; '
                f'display:inline-block; margin-right:6px; border-radius:2px; '
                f'vertical-align:middle;"></i>'
                f'<span style="color:#000000;">{label}</span></div>'
            )
        html += '</div>'
        m.get_root().html.add_child(folium.Element(html))

    folium.LayerControl(collapsed=False).add_to(m)
    return m


def search_data(df: pd.DataFrame, term: str) -> pd.DataFrame:
    """Filter DataFrame by a search term across key columns."""
    if not term:
        return df
    term = str(term).lower()
    mask = pd.Series(False, index=df.index)
    for col in ['parcel_uid', 'owner_name', 'current_lessee_or_permit_holder', 'gemarkung', 'flurstueck']:
        if col in df.columns:
            mask = mask | df[col].astype(str).str.lower().str.contains(term, na=False)
    return df[mask]
