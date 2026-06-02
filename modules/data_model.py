LAYER_TYPES = [
    "PV plant", "compensation area", "cable route", "cable tray",
    "access road", "temporary access road", "substation", "coupling station",
    "other BoP equipment", "fence", "building", "security equipment",
    "building plan / zoning plan", "other zoning", "parcel boundary",
    "parcel label", "parcel owner label", "ignore / drop"
]

def create_parcel_uid(gemarkung: str, flur: str, flurstueck: str) -> str:
    """Create canonical parcel uid."""
    return f"{str(gemarkung).strip()}|{str(flur).strip()}|{str(flurstueck).strip()}"
