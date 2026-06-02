import streamlit as st
import pandas as pd
import geopandas as gpd

# Set up page config first
st.set_page_config(
    page_title="Steinhöfel GIS Due Diligence",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Load modules
from modules.sample_data import get_sample_kpis, load_builtin_sample_data
from modules.import_excel import read_excel_file, auto_detect_columns, render_column_mapping_ui, process_uploaded_excel, process_steinhoefel_template, EXPECTED_COLUMNS
from modules.import_gis import import_gis_file, render_crs_selector, render_layer_mapping_ui, classify_and_split, try_extract_parcel_ids_from_gis
from modules.online_gis_fetcher import fetch_brandenburg_alkis_parcels, get_aoi_bounds, normalize_online_parcel_identifiers
from modules.validation_engine import run_validations, get_issues_summary, get_financing_critical
from modules.map_renderer import render_map, COLOR_MODES, CATEGORY_COLORS, STATUS_COLORS, search_data
from modules.exporter import render_download_buttons
from streamlit_folium import st_folium

def apply_custom_css():
    st.markdown("""
        <style>
        .main-header { color: #1F4E78; font-weight: 700; margin-bottom: 20px; }
        .kpi-card { background: rgba(255, 255, 255, 0.1); padding: 15px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center; border-top: 3px solid #2E86C1; border: 1px solid rgba(128, 128, 128, 0.2); height: 100%;}
        .kpi-value { font-size: 24px; font-weight: bold; }
        .kpi-label { font-size: 11px; text-transform: uppercase; opacity: 0.8; margin-top: 5px; }
        .info-box { background-color: rgba(46, 134, 193, 0.1); border-left: 5px solid #2E86C1; padding: 15px; border-radius: 4px; margin-bottom: 20px; }
        </style>
    """, unsafe_allow_html=True)


def _is_gdf_valid(gdf):
    """Safely check if a GeoDataFrame is non-None and non-empty."""
    return gdf is not None and isinstance(gdf, gpd.GeoDataFrame) and not gdf.empty


class SteinhoefelApp:
    def __init__(self):
        if 'legal_data' not in st.session_state:
            st.session_state.legal_data = load_builtin_sample_data()
        if 'parcels_gdf' not in st.session_state:
            st.session_state.parcels_gdf = None
        if 'infra_gdf' not in st.session_state:
            st.session_state.infra_gdf = None
        if 'unmatched_parcels_gdf' not in st.session_state:
            st.session_state.unmatched_parcels_gdf = None
        if 'online_parcels_gdf' not in st.session_state:
            st.session_state.online_parcels_gdf = None
        if '_online_fetch_attempted' not in st.session_state:
            st.session_state._online_fetch_attempted = False
            
    def render_sidebar(self):
        with st.sidebar:
            st.markdown("# Settings & Import")
            st.markdown("---")

            # --- Section 1: Excel ---
            st.subheader("1. Legal/Commercial Data (Excel)")
            excel_file = st.file_uploader("Upload Parcel Data (.xlsx)", type=["xlsx"])
            if excel_file:
                df_raw, sheets = read_excel_file(excel_file)
                if not df_raw.empty:
                    if "Zusammengefasste Liste" in sheets:
                        if st.button("Process Steinhöfel Template", use_container_width=True, type="primary"):
                            processed = process_steinhoefel_template(df_raw)
                            if not processed.empty:
                                st.session_state.legal_data = processed
                                # Reset online fetch flag so new data triggers a fresh fetch
                                st.session_state._online_fetch_attempted = False
                                st.session_state.parcels_gdf = None
                                st.session_state.unmatched_parcels_gdf = None
                                st.session_state.online_parcels_gdf = None
                                st.success("Legal data automatically imported!")
                    else:
                        st.info("Steinhöfel template not detected automatically.")
                        
                    with st.expander("Advanced: manual column mapping fallback"):
                        auto_map = auto_detect_columns(df_raw)
                        final_map = render_column_mapping_ui(df_raw, auto_map)
                        if st.button("Process with Manual Mapping"):
                            processed = process_uploaded_excel(df_raw, final_map)
                            if not processed.empty:
                                st.session_state.legal_data = processed
                                st.success("Legal data updated manually!")
            
            st.markdown("---")

            # --- Section 2: GIS Geometry Data ---
            st.subheader("2. GIS Geometry Data")
            gis_file = st.file_uploader("Upload DXF/GeoJSON/SHP", type=["dxf", "geojson", "json", "kml", "zip"])
            if gis_file:
                crs = render_crs_selector()
                if st.button("Import GIS", use_container_width=True):
                    with st.spinner("Processing geometries..."):
                        gdf = import_gis_file(gis_file, gis_file.name, crs)
                        gdf = try_extract_parcel_ids_from_gis(gdf)
                        st.session_state._temp_gdf = gdf
                        
            if hasattr(st.session_state, '_temp_gdf') and st.session_state._temp_gdf is not None:
                with st.expander("Classify GIS Layers", expanded=True):
                    layer_map = render_layer_mapping_ui(st.session_state._temp_gdf)
                    if st.button("Apply Classification & Join"):
                        p_gdf, i_gdf = classify_and_split(st.session_state._temp_gdf, layer_map)
                        if not _is_gdf_valid(st.session_state.parcels_gdf):
                            st.session_state.parcels_gdf = p_gdf
                        else:
                            st.session_state.parcels_gdf = pd.concat([st.session_state.parcels_gdf, p_gdf], ignore_index=True)
                        if not _is_gdf_valid(st.session_state.infra_gdf):
                            st.session_state.infra_gdf = i_gdf
                        else:
                            st.session_state.infra_gdf = pd.concat([st.session_state.infra_gdf, i_gdf], ignore_index=True)
                        del st.session_state._temp_gdf
                        st.success("GIS data added to map!")
                        st.rerun()

            st.markdown("---")

            # --- Section 3: Online GIS Data ---
            st.subheader("3. Online GIS Data")
            st.session_state.auto_fetch_online_gis = st.checkbox(
                "Auto-fetch cadastral parcels", value=True,
                help="Automatically fetch official ALKIS parcel boundaries if no manual GIS file is uploaded."
            )
            st.session_state.online_gis_buffer = st.slider("Buffer distance (m)", 0, 2000, 500, 100)
            
            if st.button("Fetch / Refresh Online GIS", use_container_width=True):
                st.session_state._force_online_fetch = True
                st.session_state._online_fetch_attempted = False
            if st.button("Clear Cached Online Data", use_container_width=True):
                fetch_brandenburg_alkis_parcels.clear()
                st.session_state.online_parcels_gdf = None
                st.session_state.parcels_gdf = None
                st.session_state.unmatched_parcels_gdf = None
                st.session_state._online_fetch_attempted = False
                st.success("Cache cleared.")

            st.markdown("---")

            # --- Section 4: Data Export ---
            st.subheader("4. Data Export")
            if not st.session_state.legal_data.empty:
                render_download_buttons(st.session_state.legal_data, "Steinhoefel_Legal_Data", "sidebar_dl")

            st.markdown("---")

            # --- Reset ---
            if st.button("Reset All Data", type="secondary"):
                st.session_state.legal_data = pd.DataFrame()
                st.session_state.parcels_gdf = None
                st.session_state.infra_gdf = None
                st.session_state.unmatched_parcels_gdf = None
                st.session_state.online_parcels_gdf = None
                st.session_state._online_fetch_attempted = False
                st.rerun()

    def render_kpis(self, df: pd.DataFrame):
        kpis = get_sample_kpis(df)
        st.markdown("<h2 class='main-header'>Portfolio Overview</h2>", unsafe_allow_html=True)
        
        def card(col, val, label):
            col.markdown(f'<div class="kpi-card"><div class="kpi-value">{val}</div><div class="kpi-label">{label}</div></div>', unsafe_allow_html=True)
            
        c1, c2, c3, c4, c5 = st.columns(5)
        card(c1, kpis['total_rows'], "Total Rows")
        card(c2, kpis['unique_parcel_uids'], "Unique Parcels")
        card(c3, kpis['status_counts'].get('secured', 0), "Secured Parcels")
        card(c4, len(kpis['owner_counts']), "Unique Owners")
        card(c5, kpis['deed_rows'], "With Deed Ref")
        
        st.write("")
        c1, c2, c3, c4, c5 = st.columns(5)
        card(c1, kpis['cat_counts'].get('pv_plant', 0), "PV Rows")
        card(c2, kpis['cat_counts'].get('cable', 0), "Cable Rows")
        card(c3, kpis['land_reg_rows'], "With Land Reg Date")
        card(c4, kpis['rank_sub_rows'], "Rank Sub. Required")
        card(c5, kpis['baulast_rows'], "With Baulast")
        st.write("")

    def run(self):
        apply_custom_css()
        self.render_sidebar()
        
        df = st.session_state.legal_data
        if df.empty:
            st.info("Welcome! Please upload the Steinhöfel 'Zusammengefasste Liste' Excel file in the sidebar to begin.")
            return
            
        self.render_kpis(df)
        
        # --- Online GIS Workflow ---
        # Only attempt fetch if: auto-fetch is on, no parcels exist yet, we haven't already tried, and df has required columns
        needs_online_fetch = False
        if st.session_state.get('auto_fetch_online_gis', True):
            if not _is_gdf_valid(st.session_state.parcels_gdf) and not st.session_state._online_fetch_attempted:
                needs_online_fetch = True
        if st.session_state.get('_force_online_fetch', False):
            needs_online_fetch = True
            st.session_state._force_online_fetch = False
            
        if needs_online_fetch and not df.empty and 'parcel_uid' in df.columns:
            st.session_state._online_fetch_attempted = True  # Prevent infinite loop
            with st.spinner("Fetching official ALKIS geometries from Brandenburg INSPIRE WFS..."):
                try:
                    minx, miny, maxx, maxy, crs_urn = get_aoi_bounds(
                        st.session_state.infra_gdf,
                        st.session_state.get('online_gis_buffer', 500)
                    )
                    online_gdf = fetch_brandenburg_alkis_parcels(minx, miny, maxx, maxy, crs_urn)
                    if _is_gdf_valid(online_gdf):
                        st.session_state.online_parcels_gdf = normalize_online_parcel_identifiers(online_gdf, df)
                        
                        # Split into matched and unmatched
                        matched_gdf = st.session_state.online_parcels_gdf[
                            st.session_state.online_parcels_gdf['match_status'] == 'matched'
                        ].copy()
                        unmatched_gdf = st.session_state.online_parcels_gdf[
                            st.session_state.online_parcels_gdf['match_status'] != 'matched'
                        ].copy()
                        
                        st.session_state.parcels_gdf = matched_gdf if not matched_gdf.empty else None
                        st.session_state.unmatched_parcels_gdf = unmatched_gdf if not unmatched_gdf.empty else None
                    else:
                        st.error("Could not fetch online cadastral parcel geometries. Upload parcel boundary data manually or try again later.")
                except Exception as e:
                    st.error(f"WFS fetch failed: {e}")
        
        tab1, tab2, tab3 = st.tabs(["Interactive Map", "Validation & Issues", "Data Explorer"])
        
        with tab1:
            has_parcels = _is_gdf_valid(st.session_state.parcels_gdf)
            has_unmatched = _is_gdf_valid(st.session_state.unmatched_parcels_gdf)
            
            if not has_parcels and not has_unmatched:
                st.markdown('<div class="info-box">No GIS geometry available yet. Upload DXF, KML, SHP or GeoJSON in the sidebar, or enable auto-fetch to get official cadastral boundaries.</div>', unsafe_allow_html=True)
            
            # Simple controls
            c1, c2 = st.columns([1, 2])
            with c1:
                color_mode_label = st.selectbox("Color by", list(COLOR_MODES.keys()))
                color_mode = COLOR_MODES[color_mode_label]
            with c2:
                s_term = st.text_input("Search (Parcel ID, Owner, Gemarkung...)", "")
            
            map_df = search_data(df, s_term)
            
            # Combine matched + unmatched parcels
            all_parcels = st.session_state.parcels_gdf
            if has_unmatched:
                if all_parcels is not None:
                    all_parcels = pd.concat([all_parcels, st.session_state.unmatched_parcels_gdf], ignore_index=True)
                else:
                    all_parcels = st.session_state.unmatched_parcels_gdf
            
            # Render map + legend side by side
            map_col, legend_col = st.columns([5, 1])
            with map_col:
                m = render_map(map_df, all_parcels, st.session_state.infra_gdf, color_mode)
                st_folium(m, width=None, height=600, returned_objects=[])
            with legend_col:
                if color_mode == 'category':
                    items = CATEGORY_COLORS
                    title = "Category"
                elif color_mode == 'status':
                    items = STATUS_COLORS
                    title = "Secured Status"
                else:
                    items = {}
                    title = "Owner"
                
                legend_html = f'<div style="padding:10px; border:1px solid #ddd; border-radius:6px; background:rgba(255,255,255,0.05);">'
                legend_html += f'<b>{title}</b><br><br>'
                if items:
                    for key, color in items.items():
                        label = key.replace('_', ' ').title()
                        legend_html += (
                            f'<div style="margin-bottom:6px; display:flex; align-items:center;">'
                            f'<span style="background:{color}; width:16px; height:16px; '
                            f'display:inline-block; margin-right:8px; border-radius:3px; '
                            f'border:1px solid rgba(0,0,0,0.15); flex-shrink:0;"></span>'
                            f'<span style="font-size:13px;">{label}</span></div>'
                        )
                else:
                    legend_html += '<span style="font-size:12px; opacity:0.7;">Each owner gets a unique color.</span>'
                legend_html += '<br><div style="margin-top:4px; display:flex; align-items:center;">'
                legend_html += '<span style="background:#CCCCCC; width:16px; height:16px; display:inline-block; margin-right:8px; border-radius:3px; border:1px solid rgba(0,0,0,0.15); flex-shrink:0;"></span>'
                legend_html += '<span style="font-size:13px;">Not in Excel</span></div>'
                legend_html += '</div>'
                st.markdown(legend_html, unsafe_allow_html=True)
            
            # Online GIS info (collapsed by default)
            if _is_gdf_valid(st.session_state.online_parcels_gdf):
                ogdf = st.session_state.online_parcels_gdf
                with st.expander("Online GIS Info"):
                    n_matched = int((ogdf['match_status'] == 'matched').sum())
                    n_unmatched = int((ogdf['match_status'] != 'matched').sum())
                    st.write(f"Downloaded {len(ogdf)} parcels from Brandenburg ALKIS WFS. Matched: {n_matched}, Unmatched: {n_unmatched}")
            
            st.caption("Parcel geometries: GeoBasis-DE/LGB, dl-de/by-2-0. This tool does not replace legal review.")
                
        with tab2:
            st.markdown("<h2 class='main-header'>Validation & Financing Issues</h2>", unsafe_allow_html=True)
            with st.spinner("Running validations..."):
                issues_df = run_validations(df)
            if issues_df.empty:
                st.success("No issues found! The portfolio data is clean.")
            else:
                summary = get_issues_summary(issues_df)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total Issues", summary['total'])
                c2.metric("Critical", summary['severity'].get('Critical', 0))
                c3.metric("High", summary['severity'].get('High', 0))
                c4.metric("Medium", summary['severity'].get('Medium', 0))
                
                st.subheader("Financing Critical Issues (Critical & High)")
                st.dataframe(get_financing_critical(issues_df), use_container_width=True, hide_index=True)
                with st.expander("Show All Issues"):
                    st.dataframe(issues_df, use_container_width=True, hide_index=True)
                render_download_buttons(issues_df, "Steinhoefel_DD_Issues", "issues")

        with tab3:
            st.markdown("<h2 class='main-header'>Data Explorer</h2>", unsafe_allow_html=True)
            
            # Filters
            with st.expander("Filter Data", expanded=False):
                fc1, fc2, fc3, fc4 = st.columns(4)
                cat_filter = fc1.multiselect("Kategorie", df['category'].unique() if 'category' in df.columns else [])
                ba_filter = fc2.multiselect("Bauabschnitt", df['bauabschnitt'].unique() if 'bauabschnitt' in df.columns else [])
                owner_filter = fc3.multiselect("Eigentümer", df['owner_name'].dropna().unique() if 'owner_name' in df.columns else [])
                status_filter = fc4.multiselect("Secured Status", df['secured_status'].unique() if 'secured_status' in df.columns else [])
                
                fc5, fc6, fc7, fc8 = st.columns(4)
                ctype_filter = fc5.multiselect("Contract Type", df['contract_type'].unique() if 'contract_type' in df.columns else [])
                ease_filter = fc6.multiselect("Easement Type", df['easement_type'].dropna().unique() if 'easement_type' in df.columns else [])
                rs_filter = fc7.multiselect("Rank Subordination Req.", df['rank_subordination_required'].dropna().unique() if 'rank_subordination_required' in df.columns else [])
                pub_filter = fc8.multiselect("Public/Private", df['public_private'].dropna().unique() if 'public_private' in df.columns else [])
            
            filter_df = df.copy()
            if cat_filter: filter_df = filter_df[filter_df['category'].isin(cat_filter)]
            if ba_filter: filter_df = filter_df[filter_df['bauabschnitt'].isin(ba_filter)]
            if owner_filter: filter_df = filter_df[filter_df['owner_name'].isin(owner_filter)]
            if status_filter: filter_df = filter_df[filter_df['secured_status'].isin(status_filter)]
            if ctype_filter: filter_df = filter_df[filter_df['contract_type'].isin(ctype_filter)]
            if ease_filter: filter_df = filter_df[filter_df['easement_type'].isin(ease_filter)]
            if rs_filter: filter_df = filter_df[filter_df['rank_subordination_required'].isin(rs_filter)]
            if pub_filter: filter_df = filter_df[filter_df['public_private'].isin(pub_filter)]

            st.dataframe(filter_df, use_container_width=True, hide_index=True)
            render_download_buttons(filter_df, "Steinhoefel_Master_Data", "master")

if __name__ == "__main__":
    app = SteinhoefelApp()
    app.run()
