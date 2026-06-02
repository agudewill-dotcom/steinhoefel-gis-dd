import pandas as pd
import numpy as np

def run_validations(df: pd.DataFrame) -> pd.DataFrame:
    """Run the 15 specific validation rules."""
    issues = []
    issue_counter = 1

    def add_issue(row, severity, category_check, desc):
        nonlocal issue_counter
        issues.append({
            'issue_id': f'ISS-{issue_counter:04d}',
            'severity': severity,
            'category_check': category_check,
            'parcel_uid': row.get('parcel_uid', ''),
            'gemarkung': row.get('gemarkung', ''),
            'flur': row.get('flur', ''),
            'flurstueck': row.get('flurstueck', ''),
            'owner': row.get('owner_name', ''),
            'contract_type': row.get('contract_type_raw', ''),
            'issue_description': desc
        })
        issue_counter += 1

    # Pre-compute some aggregations for multi-row rules
    uid_owners = df.groupby('parcel_uid')['owner_name'].nunique()
    
    # Compute lessee uniqueness safely
    uid_current_lessees = df.groupby('parcel_uid')['current_lessee_or_permit_holder'].nunique()
    uid_target_lessees = df.groupby('parcel_uid')['target_lessee_or_permit_holder'].nunique()
    
    uid_ba = df.groupby('parcel_uid')['bauabschnitt'].nunique()

    def is_empty_or_todo(val):
        if pd.isna(val): return True
        s = str(val).strip().lower()
        if not s or "todo" in s or "to-do" in s or s in ["n/a", "na", "-"]: return True
        return False

    for _, row in df.iterrows():
        uid = str(row.get('parcel_uid', '||'))
        g = str(row.get('gemarkung', '')).strip()
        f = str(row.get('flur', '')).strip()
        fs = str(row.get('flurstueck', '')).strip()

        # 1. Missing Gemarkung, Flur or Flurstück
        if not g or not f or not fs:
            add_issue(row, 'Critical', 'Data Quality', 'Missing Gemarkung, Flur, or Flurstück')

        # 2. Duplicate parcel_uid with conflicting owner_name
        if uid in uid_owners and uid_owners[uid] > 1:
            add_issue(row, 'High', 'Data Quality', 'Duplicate parcel_uid with conflicting owner_name')

        # 3. Duplicate parcel_uid with conflicting lessee
        if uid in uid_current_lessees.index and uid in uid_target_lessees.index:
            if uid_current_lessees.get(uid, 0) > 1 or uid_target_lessees.get(uid, 0) > 1:
                add_issue(row, 'High', 'SPV / Entity', 'Duplicate parcel_uid with conflicting lessee or permit holder')

        cat = row.get('category')
        c_type = row.get('contract_type')
        s_status = row.get('secured_status')

        # 4. Kabel but contract does not indicate cable rights
        if cat == 'cable' and c_type not in ['cable_use_or_access_agreement', 'cable_use_agreement']:
            add_issue(row, 'High', 'Contract', 'Category is Kabel but contract does not indicate cable rights')

        # 5. Zuwegung but contract does not indicate access rights
        if cat == 'access_road' and c_type not in ['cable_use_or_access_agreement', 'access_road_agreement']:
            add_issue(row, 'High', 'Contract', 'Category is Zuwegung but contract does not indicate access rights')

        # 6. PV not secured
        if cat == 'pv_plant' and s_status != 'secured':
            add_issue(row, 'Critical', 'Contract', 'Category is PV but status is not secured')

        # 7. Kabel not secured
        if cat == 'cable' and s_status != 'secured':
            add_issue(row, 'Critical', 'Contract', 'Category is Kabel but status is not secured')

        # 8. Zuwegung not secured
        if cat == 'access_road' and s_status != 'secured':
            add_issue(row, 'Critical', 'Contract', 'Category is Zuwegung but status is not secured')

        # 9. jein
        if s_status == 'partly_secured_or_unclear':
            add_issue(row, 'High', 'Legal DD', 'Vertraglich gesichert is "jein"')

        # 10. In Arbeit
        if s_status == 'in_progress':
            add_issue(row, 'High', 'Legal DD', 'Vertraglich gesichert is "In Arbeit"')

        # 11. Urkunde missing although secured
        if s_status == 'secured' and is_empty_or_todo(row.get('deed_reference')):
            add_issue(row, 'Medium', 'Legal DD', 'Urkunde is empty or To-Do although secured')

        # 12. GB eingetragen am missing although Dienstbarkeit expected
        ease = str(row.get('easement_type', '')).lower()
        if not is_empty_or_todo(row.get('easement_type')) and "nicht" not in ease and is_empty_or_todo(row.get('land_register_registration_date')):
            add_issue(row, 'High', 'Easement', 'GB eingetragen am is empty or To-Do although a Dienstbarkeit is expected')

        # 13. Rangrücktritt missing
        if not is_empty_or_todo(row.get('rank_subordination_required')) and is_empty_or_todo(row.get('rank_subordination_status')):
            add_issue(row, 'High', 'Lender DD', 'Rangrücktritt notwendig is set but Status is empty or To-Do')

        # 14. Baulast exists
        if not is_empty_or_todo(row.get('building_encumbrance')):
            add_issue(row, 'Medium', 'Building Encumbrance', 'Baulast is not empty and not n/a')

        # 15. Flurstück in multiple Bauabschnitte
        if uid in uid_ba and uid_ba[uid] > 1:
            add_issue(row, 'Medium', 'Bauabschnitt Allocation', 'Flurstück is used in multiple Bauabschnitte')

    # Remove duplicates from issues (because row-based loop causes multiple identical multi-row issues to appear for each row of a duplicate uid)
    if issues:
        idf = pd.DataFrame(issues)
        idf = idf.drop_duplicates(subset=['parcel_uid', 'issue_description'])
        return idf

    return pd.DataFrame(columns=[
        'issue_id', 'severity', 'category_check', 'parcel_uid', 'gemarkung', 'flur', 'flurstueck', 'owner', 'contract_type', 'issue_description'
    ])

def get_issues_summary(issues_df: pd.DataFrame) -> dict:
    if issues_df.empty:
        return {'total': 0, 'severity': {}, 'category': {}}
    return {
        'total': len(issues_df),
        'severity': issues_df['severity'].value_counts().to_dict(),
        'category': issues_df['category_check'].value_counts().to_dict()
    }

def get_financing_critical(issues_df: pd.DataFrame) -> pd.DataFrame:
    if issues_df.empty:
        return issues_df
    return issues_df[issues_df['severity'].isin(['Critical', 'High'])].copy()
