"""Configuration schema for the Esri UC conference catalog plugin."""

from pydantic import BaseModel, Field


class EsriUCPluginConfig(BaseModel):
    """Config for the esri_uc plugin.

    The catalog is a static snapshot bundled with the deployment (data/*.json,
    produced by scripts/snapshot.py). There are no upstream endpoints and no
    credentials — the only knobs are the data file locations (overridable for
    tests) and display names.
    """

    conference_name: str = Field(
        default="2026 Esri User Conference",
        description="Display name of the conference",
    )
    sessions_file: str = Field(
        default="data/sessions.json",
        description="Path to the normalized sessions snapshot (repo-relative or absolute)",
    )
    exhibitors_file: str = Field(
        default="data/exhibitors.json",
        description="Path to the normalized exhibitors snapshot (repo-relative or absolute)",
    )
    rooms_file: str = Field(
        default="data/rooms.json",
        description="Path to the event-map rooms snapshot (repo-relative or absolute)",
    )

    model_config = {"extra": "ignore"}
