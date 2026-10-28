"""Versioned capture setup rules, separate from operator attestations."""
from __future__ import annotations


SETUP_POLICY = "configured_geometry_v1"


def setup_allows_trial(settings, *, requires_force_direction=True):
    """Use configured geometry without inventing an operator confirmation.

    Only new sessions explicitly recording this policy may omit the retired
    setup checkboxes. Historical sessions retain their recorded confirmation
    requirements; the policy does not qualify mounting or physical accuracy.
    """
    if settings.get("setup_policy") == SETUP_POLICY:
        return True
    coordinates = settings.get("coordinates_confirmed", settings.get("point_grid_confirmed")) is True
    direction = not requires_force_direction or settings.get("force_direction_confirmed") is True
    return coordinates and direction
