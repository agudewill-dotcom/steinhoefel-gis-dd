import pandas as pd
import io

def get_sample_kpis(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            'total_rows': 0, 'unique_parcel_uids': 0, 'cat_counts': {}, 'ba_counts': {},
            'status_counts': {}, 'owner_counts': {}, 'current_lessee_counts': {},
            'target_lessee_counts': {}, 'contract_type_counts': {}, 'deed_rows': 0,
            'land_reg_rows': 0, 'rank_sub_rows': 0, 'baulast_rows': 0
        }

    return {
        'total_rows': len(df),
        'unique_parcel_uids': df['parcel_uid'].nunique() if 'parcel_uid' in df else 0,
        'cat_counts': df['category'].value_counts().to_dict() if 'category' in df else {},
        'ba_counts': df['bauabschnitt'].value_counts().to_dict() if 'bauabschnitt' in df else {},
        'status_counts': df['secured_status'].value_counts().to_dict() if 'secured_status' in df else {},
        'owner_counts': df['owner_name'].value_counts().to_dict() if 'owner_name' in df else {},
        'current_lessee_counts': df['current_lessee_or_permit_holder'].value_counts().to_dict() if 'current_lessee_or_permit_holder' in df else {},
        'target_lessee_counts': df['target_lessee_or_permit_holder'].value_counts().to_dict() if 'target_lessee_or_permit_holder' in df else {},
        'contract_type_counts': df['secured_by_contract_type'].value_counts().to_dict() if 'secured_by_contract_type' in df else {},
        
        'deed_rows': int(df['deed_reference'].apply(lambda x: pd.notna(x) and str(x).strip() not in ['', 'n/a', '-', 'To-Do']).sum()) if 'deed_reference' in df else 0,
        'land_reg_rows': int(df['land_register_registration_date'].apply(lambda x: pd.notna(x) and str(x).strip() not in ['', 'n/a', '-', 'To-Do']).sum()) if 'land_register_registration_date' in df else 0,
        'rank_sub_rows': int(df['rank_subordination_required'].apply(lambda x: pd.notna(x) and str(x).strip() not in ['', 'n/a', '-', 'To-Do']).sum()) if 'rank_subordination_required' in df else 0,
        'baulast_rows': int(df['building_encumbrance'].apply(lambda x: pd.notna(x) and str(x).strip() not in ['', 'n/a', '-', 'To-Do']).sum()) if 'building_encumbrance' in df else 0
    }

def load_builtin_sample_data() -> pd.DataFrame:
    # We return an empty dataframe. The new default is that the user uploads the Steinhöfel Excel file.
    # The previous embedded sample was for the old schema. 
    # The user specifically requested: "The app must automatically recognize this structure and normalize it into the internal data model... Build this as the new default import workflow."
    return pd.DataFrame()
