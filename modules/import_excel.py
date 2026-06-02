import pandas as pd
import numpy as np
import streamlit as st
import re

EXPECTED_COLUMNS = {
    "Kategorie": "category",
    "Planteil": "plan_section",
    "Bauabschnitt": "bauabschnitt",
    "Gemarkung": "gemarkung",
    "Flur": "flur",
    "Flurstück": "flurstueck",
    "Aktueller Pächter/Gestattungsnehmer": "current_lessee_or_permit_holder",
    "Zielpächter/Gestattungsnehmer": "target_lessee_or_permit_holder",
    "Eigentümer": "owner_name",
    "Vertraglich gesichert": "contractually_secured_raw",
    "Gesichert durch": "secured_by_contract_type",
    "Art d. Dienstbarkeit": "easement_type",
    "Urkunde": "deed_reference",
    "Datum Urkunde": "deed_date",
    "GB eingetragen am": "land_register_registration_date",
    "GB Blatt": "land_register_folio",
    "Notar": "notary",
    "Rangrücktritt notwendig (gem. 3. draft LDD Report)": "rank_subordination_required",
    "Status Rangrücktritt": "rank_subordination_status",
    "Öffentlich/privat": "public_private",
    "Trassenlänge": "cable_route_length_m",
    "Flurstücksgröße gem. Pachtvertrag (m²)": "parcel_size_lease_contract_m2",
    "Pachtfläche gem. Pachtvertrag (m²)": "leased_area_m2",
    "m² 2026 genutzt PV": "pv_area_used_2026_m2",
    "m² 2026 Grünfläche innerhalb Zaun": "green_area_inside_fence_2026_m2",
    "m² 2026 Grünfläche außerhalb Zaun": "green_area_outside_fence_2026_m2",
    "Baulast": "building_encumbrance"
}

def clean_string(val):
    if pd.isna(val):
        return ""
    val = str(val).strip()
    if val.lower() in ["n/a", "to-do", "-", "todo", "na", "n.a."]:
        return ""
    # Normalize multiple spaces
    val = re.sub(r'\s+', ' ', val)
    return val

def format_parcel_num(val):
    if pd.isna(val): return ""
    val = str(val).strip()
    # Remove .0 if it was parsed as float
    if val.endswith('.0'):
        val = val[:-2]
    val = re.sub(r'\s+', ' ', val)
    return val

def read_excel_file(uploaded_file) -> tuple[pd.DataFrame, list[str]]:
    """Read Excel file, return (df, sheet_names). Auto-select first sheet if target not found."""
    try:
        excel_file = pd.ExcelFile(uploaded_file)
        sheet_names = excel_file.sheet_names
        if not sheet_names:
            st.error("Excel file has no sheets.")
            return pd.DataFrame(), []
            
        target_sheet = "Zusammengefasste Liste"
        if target_sheet in sheet_names:
            # Read Steinhöfel template
            # Header is row 5 (index 4), data starts row 6
            # Usecols B to AB (1 to 27)
            df = pd.read_excel(excel_file, sheet_name=target_sheet, header=4, usecols="B:AB")
            
            # Remove row 6 if it contains explanatory notes (which would be index 0 after reading header=4)
            # Actually, "Row 4 contains explanatory category notes". Header is row 5. 
            # If header is row 5, then row 4 is index 3. If we read with header=4 (which is row 5), 
            # row 4 is ignored automatically by pandas header=4 (skips rows 0,1,2,3).
            # Data starts row 6, which is index 5. So header=4 handles it perfectly!
            
            df.columns = df.columns.astype(str).str.strip()
            return df, sheet_names
        else:
            # Fallback
            df = pd.read_excel(excel_file, sheet_name=sheet_names[0])
            df.columns = df.columns.astype(str).str.strip()
            return df, sheet_names
    except Exception as e:
        st.error(f"Error reading Excel file: {e}")
        return pd.DataFrame(), []

def process_steinhoefel_template(df: pd.DataFrame) -> pd.DataFrame:
    """Automatically process and normalize the Steinhöfel template."""
    out_df = pd.DataFrame()
    
    # Map exact columns
    for orig_col, new_col in EXPECTED_COLUMNS.items():
        if orig_col in df.columns:
            out_df[new_col] = df[orig_col].apply(clean_string)
        else:
            out_df[new_col] = ""
            
    # Normalize parcel_uid
    out_df['gemarkung'] = out_df['gemarkung'].apply(clean_string)
    out_df['flur'] = out_df['flur'].apply(format_parcel_num)
    out_df['flurstueck'] = out_df['flurstueck'].apply(format_parcel_num)
    
    out_df['parcel_uid'] = out_df['gemarkung'] + '|' + out_df['flur'] + '|' + out_df['flurstueck']
    
    # Normalize Vertraglich gesichert
    def normalize_status(val):
        v = str(val).lower().strip()
        if v == "ja": return "secured", True
        elif v == "nein": return "unsecured", False
        elif v == "in arbeit": return "in_progress", False
        elif v == "jein": return "partly_secured_or_unclear", False
        else: return "unclear", False

    statuses = out_df['contractually_secured_raw'].apply(normalize_status)
    out_df['secured_status'] = [x[0] for x in statuses]
    out_df['secured_bool'] = [x[1] for x in statuses]
    
    # Normalize Kategorie
    def normalize_category(val):
        v = str(val).strip()
        vl = v.lower()
        if "kabel" in vl: return "cable"
        if "pv" in vl: return "pv_plant"
        if "zuwegung" in vl: return "access_road"
        if "ausgleich" in vl: return "compensation_area"
        if "uw" in vl or "umspannwerk" in vl: return "substation"
        return "other"
        
    out_df['category'] = out_df['category'].apply(normalize_category)
    
    # Normalize Gesichert durch -> contract_type
    def normalize_contract(val):
        v = str(val).strip()
        vl = v.lower()
        if v == "PuGV": return "land_usage_agreement_pv"
        if v == "GuEV": return "cable_use_or_access_agreement"
        if "kabel" in vl: return "cable_use_agreement"
        if "zuwegung" in vl: return "access_road_agreement"
        if "pv" in vl: return "pv_land_usage_agreement"
        if not v: return "unclear"
        return "unclear"
        
    out_df['contract_type'] = out_df['secured_by_contract_type'].apply(normalize_contract)
    
    # Keep raw text for contract type just in case
    out_df['contract_type_raw'] = out_df['secured_by_contract_type']
    
    # Convert numerical columns
    num_cols = [
        'cable_route_length_m', 'parcel_size_lease_contract_m2', 'leased_area_m2',
        'pv_area_used_2026_m2', 'green_area_inside_fence_2026_m2', 'green_area_outside_fence_2026_m2'
    ]
    for c in num_cols:
        out_df[c] = pd.to_numeric(out_df[c].replace('', np.nan), errors='coerce')
        
    # Convert Dates
    date_cols = ['deed_date', 'land_register_registration_date']
    for c in date_cols:
        out_df[c] = pd.to_datetime(out_df[c], errors='coerce').dt.strftime('%Y-%m-%d').fillna(out_df[c])

    # To maintain compatibility with some existing renderer logic if needed, we define:
    out_df['planteil_tokens'] = out_df['plan_section'].apply(lambda x: ', '.join([t.strip() for t in str(x).split(',') if t.strip()]))
    
    # Remove completely empty rows if they exist
    out_df = out_df[out_df['parcel_uid'] != "||"]
    
    return out_df

# Fallback UI stuff (kept for advanced usage)
def auto_detect_columns(df: pd.DataFrame) -> dict[str, str]:
    """Auto-detect column mapping."""
    return {}

def render_column_mapping_ui(df: pd.DataFrame, auto_mapping: dict) -> dict:
    """Streamlit UI for column mapping."""
    mapping = {}
    options = ["(not mapped)"] + list(df.columns)
    
    # Create simple mapping UI for fallback
    cols = list(EXPECTED_COLUMNS.values())
    for col in cols:
        mapping[col] = st.selectbox(f"Map {col}", options, index=0, key=f'map_{col}')
    
    for k, v in mapping.items():
        if v == "(not mapped)":
            mapping[k] = None
    return mapping

def process_uploaded_excel(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """Fallback manual processing."""
    out_df = pd.DataFrame()
    for app_field, excel_col in mapping.items():
        if excel_col: out_df[app_field] = df[excel_col]
        else: out_df[app_field] = ''
    # Very basic UID for fallback
    if 'gemarkung' in out_df and 'flur' in out_df and 'flurstueck' in out_df:
        out_df['parcel_uid'] = out_df['gemarkung'].astype(str) + '|' + out_df['flur'].astype(str) + '|' + out_df['flurstueck'].astype(str)
    return out_df
