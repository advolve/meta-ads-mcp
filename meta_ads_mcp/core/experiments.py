"""A/B Experiments (AdStudy) for Meta Ads API."""

import json
from typing import List, Optional, Dict, Any, Literal

from .api import meta_api_tool, make_api_request
from .server import mcp_server

SplitVariable = Literal["CREATIVE", "AUDIENCE", "PLACEMENT", "CUSTOM"]
EntityLevel = Literal["CAMPAIGN", "AD_SET"]
OwnerType = Literal["USER", "BUSINESS"]

def _strip_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy without None values."""
    return {k: v for k, v in d.items() if v is not None}


@mcp_server.tool()
@meta_api_tool
async def create_split_test(
    access_token: str | None = None,
    owner_type: OwnerType = "USER",
    owner_id: str | None = None,  # if None and owner_type=="USER", uses 'me'
    name: str | None = None,
    description: str | None = None,
    variable: SplitVariable = "CUSTOM",  # purely descriptive
    start_time: str | None = None,       # ISO 8601, e.g. "2025-08-14T12:00:00-0300"
    end_time: str | None = None,         # ISO 8601
    confidence_level: float | None = None,  # e.g., 0.8, 0.9, 0.95 (optional; API may ignore)
    cells: Optional[List[Dict[str, Any]]] = None,
    # cells example:
    # [
    #   {
    #     "name": "A",
    #     "treatment_percentage": 50,               # integer 1..99
    #     "campaign_ids": ["123","456"],            # OR
    #     "adset_ids": ["789","101112"]            # choose one entity level per study for clarity
    #   },
    #   {
    #     "name": "B",
    #     "treatment_percentage": 50,
    #     "adset_ids": ["131415"]
    #   }
    # ]
    entity_level: EntityLevel | None = None,  # "CAMPAIGN" or "AD_SET" (recommended for validation)
    winner_criteria: str | None = None,       # metadata only; winner calc is done in insights layer
    study_type: str = "SPLIT_TEST_V2"        # current split-testing type used in UI/API
) -> str:
    """
    Create an A/B test (AdStudy) with cells and attach campaigns or ad sets.

    What this does
    --------------
    1) POST an AdStudy to `{owner}/ad_studies` (owner is a USER or BUSINESS).
       According to Meta docs, AdStudy underpins official split testing / experiments.
       The creation edge exists for users and businesses. In practice, user-scoped creation
       is commonly used. See Marketing API docs on Split Testing and AdStudy.
       (We keep 'study_type' default as "SPLIT_TEST_V2").
    2) For each requested cell, POST to `/{ad_study_id}/cells` with a name and
       treatment_percentage. Once the study is running, `treatment_percentage` cannot be
       changed, per docs.
    3) Attach entities to each cell: POST to `/{ad_study_cell_id}/campaigns` or
       `/{ad_study_cell_id}/adsets` with the IDs provided.

    Notes & limitations
    -------------------
    * The choice of "variable" (CREATIVE/AUDIENCE/PLACEMENT/CUSTOM) is a label saved in the
      study description payload. The *actual* experimental variable is determined by what
      differs between the attached campaigns/ad sets (e.g., creatives, targeting, placements).
    * Duration is controlled by `start_time`/`end_time`. The API accepts ISO timestamps.
    * Winner criteria here is metadata. Determining significance/winner is typically done from
      Insights/Lift/Experiments analysis rather than at creation time.

    Args:
        owner_type: Where to create the study. "USER" or "BUSINESS".
        owner_id: The owner id. If None and owner_type="USER", uses "me".
        name: Study name (required).
        description: Optional study description, we enrich it with metadata.
        variable: Label describing what you test (creative, audience, placement, or custom).
        start_time: ISO8601 start timestamp.
        end_time: ISO8601 end timestamp.
        confidence_level: Optional confidence level (may be ignored by API depending on type).
        cells: List of cells with percentages and entity ids (campaigns OR ad sets).
        entity_level: "CAMPAIGN" or "AD_SET". If omitted, inferred from first cell keys.
        winner_criteria: Free-text label for how you plan to compare outcomes (metadata only).
        study_type: Graph 'type' for AdStudy. Default "SPLIT_TEST_V2".

    Returns:
        JSON string including the created study, created cells, and entity attachments; or
        an error payload with context.

    References:
        - Split Testing guide (Marketing API).
        - AdStudy object reference (creation and immutability rules).
        - Business/User Ad Studies edges.
        - Cell attachments (campaigns/adsets edges).
    """
    # Required fields
    if not name:
        return json.dumps({"error": "name is required"}, indent=2)
    if not cells or not isinstance(cells, list) or len(cells) < 2:
        return json.dumps({"error": "Provide at least two cells for a valid A/B test"}, indent=2)

    # Infer default owner path
    if owner_type not in ("USER", "BUSINESS"):
        return json.dumps({"error": "owner_type must be USER or BUSINESS"}, indent=2)
    if owner_type == "USER":
        owner_path = f"{owner_id}/ad_studies" if owner_id else "me/ad_studies"
    else:
        if not owner_id:
            return json.dumps({"error": "owner_id is required when owner_type='BUSINESS'"}, indent=2)
        owner_path = f"{owner_id}/ad_studies"

    # Validate cells & percentages
    total_pct = 0
    inferred_level: EntityLevel | None = entity_level
    normalized_cells = []

    for idx, c in enumerate(cells, start=1):
        cname = (c.get("name") or f"Cell {idx}").strip()
        pct = c.get("treatment_percentage")
        if not isinstance(pct, int) or pct <= 0 or pct >= 100:
            return json.dumps({"error": f"Cell '{cname}' must have integer treatment_percentage 1..99"}, indent=2)

        has_campaigns = bool(c.get("campaign_ids"))
        has_adsets = bool(c.get("adset_ids"))

        if has_campaigns and has_adsets:
            return json.dumps({"error": f"Cell '{cname}' must reference campaigns OR ad sets, not both"}, indent=2)
        if not has_campaigns and not has_adsets:
            return json.dumps({"error": f"Cell '{cname}' must include 'campaign_ids' or 'adset_ids'"}, indent=2)

        level = "CAMPAIGN" if has_campaigns else "AD_SET"
        if inferred_level is None:
            inferred_level = level  # infer from first valid cell
        elif inferred_level != level:
            return json.dumps({"error": "All cells must use the same entity_level (all campaigns OR all ad sets)"}, indent=2)

        total_pct += pct
        normalized_cells.append({
            "name": cname,
            "treatment_percentage": pct,
            "campaign_ids": c.get("campaign_ids") or [],
            "adset_ids": c.get("adset_ids") or []
        })

    if total_pct != 100:
        return json.dumps({"error": f"Sum of treatment_percentage must be 100; got {total_pct}"}, indent=2)

    # Build study creation payload
    # Certain AdStudy fields are product-guarded; we only include safe fields.
    # If unsupported, Graph returns a clear error which we surface.
    study_params = {
        "name": name,
        "type": study_type,  # SPLIT_TEST_V2 per current guidance
    }
    if start_time:
        study_params["start_time"] = start_time
    if end_time:
        study_params["end_time"] = end_time

    # Persist metadata in description for operator clarity
    meta = {
        "variable": inferred_level and f"{variable}@{inferred_level}" or variable,
        "winner_criteria": winner_criteria
    }
    if confidence_level is not None:
        # Some versions use 'confidence' field; we store also in description to avoid loss.
        study_params["confidence"] = confidence_level
        meta["confidence_level"] = confidence_level

    if description:
        meta["notes"] = description
    study_params["description"] = json.dumps(meta, ensure_ascii=False)

    # Create AdStudy
    try:
        study = await make_api_request(owner_path, access_token, study_params, method="POST")
        study_id = study.get("id")
        if not study_id:
            return json.dumps({"error": "Study creation failed (no id returned)", "response": study}, indent=2)
    except Exception as e:
        return json.dumps({
            "error": "Failed to create AdStudy (A/B test)",
            "details": str(e),
            "owner_path": owner_path,
            "params_sent": study_params
        }, indent=2)

    created_cells: List[Dict[str, Any]] = []
    attachments: List[Dict[str, Any]] = []

    # Create cells and attach entities
    for c in normalized_cells:
        cell_body = {"name": c["name"], "treatment_percentage": c["treatment_percentage"]}
        try:
            cell = await make_api_request(f"{study_id}/cells", access_token, cell_body, method="POST")
            cell_id = cell.get("id")
            if not cell_id:
                raise RuntimeError(f"No cell id returned for {c['name']}")
            created_cells.append(cell)

            # Attach entities to cell
            if inferred_level == "CAMPAIGN":
                for cid in c["campaign_ids"]:
                    a = await make_api_request(f"{cell_id}/campaigns", access_token, {"campaign_id": cid}, method="POST")
                    attachments.append(_strip_none({"cell_id": cell_id, "campaign_id": cid, "attachment": a}))
            else:
                for aid in c["adset_ids"]:
                    a = await make_api_request(f"{cell_id}/adsets", access_token, {"adset_id": aid}, method="POST")
                    attachments.append(_strip_none({"cell_id": cell_id, "adset_id": aid, "attachment": a}))

        except Exception as e:
            return json.dumps({
                "error": "Failed while creating cells or attaching entities",
                "details": str(e),
                "study_id": study_id,
                "cell_attempt": c
            }, indent=2)

    result = {
        "study": study,
        "cells": created_cells,
        "attachments": attachments,
        "entity_level": inferred_level
    }
    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_split_test(access_token: str | None = None, study_id: str | None = None) -> str:
    """
    Retrieve an AdStudy (A/B test) with a useful field set.

    Args:
        study_id: The AdStudy id.

    Returns:
        JSON string with study details.
    """
    if not study_id:
        return json.dumps({"error": "study_id is required"}, indent=2)

    fields = ",".join([
        "id", "name", "type", "description", "start_time", "end_time",
        "created_time", "updated_time", "cooldown_start_time", "business",
        "confidence", "client_business"
    ])
    try:
        data = await make_api_request(study_id, access_token, {"fields": fields}, method="GET")
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": "Failed to fetch AdStudy", "details": str(e), "study_id": study_id}, indent=2)


@mcp_server.tool()
@meta_api_tool
async def list_split_tests(
    access_token: str | None = None,
    owner_type: OwnerType = "USER",
    owner_id: str | None = None,
    limit: int = 50,
    after: str = ""
) -> str:
    """
    List AdStudies (A/B tests) for a USER or BUSINESS.

    Args:
        owner_type: "USER" or "BUSINESS".
        owner_id: If USER and omitted, uses 'me'. BUSINESS requires explicit id.
        limit: Page size.
        after: Cursor for pagination.

    Returns:
        JSON string with items and paging.
    """
    if owner_type not in ("USER", "BUSINESS"):
        return json.dumps({"error": "owner_type must be USER or BUSINESS"}, indent=2)

    if owner_type == "USER":
        path = f"{owner_id}/ad_studies" if owner_id else "me/ad_studies"
    else:
        if not owner_id:
            return json.dumps({"error": "owner_id is required when owner_type='BUSINESS'"}, indent=2)
        path = f"{owner_id}/ad_studies"

    params = {"limit": limit}
    if after:
        params["after"] = after

    try:
        data = await make_api_request(path, access_token, params, method="GET")
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": "Failed to list AdStudies", "details": str(e), "path": path}, indent=2)
