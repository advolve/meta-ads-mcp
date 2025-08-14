"""Ad Set-related functionality for Meta Ads API."""

import json
from typing import Optional, Dict, Any, List, Set
from .api import meta_api_tool, make_api_request
from .accounts import get_ad_accounts
from .server import mcp_server

async def _get_business_id_for_account(
    account_id: str,
    access_token: Optional[str]
) -> Optional[str]:
    """
    Resolve the Business ID that owns the given Ad Account.
    Returns None when the Ad Account isn't attached to a Business or when the caller
    lacks permission to read the business edge.
    """
    try:
        # GET /act_<ID>?fields=business
        endpoint = f"{account_id}"
        params = {"fields": "business"}
        data = await make_api_request(endpoint, access_token, params, method="GET")
        biz = (data or {}).get("business")
        return biz.get("id") if isinstance(biz, dict) else None
    except Exception:
        # Swallow and let callers attempt fallbacks
        return None


async def _fetch_all_pages(
    endpoint: str,
    access_token: Optional[str],
    params: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Generic cursor-based pagination loop for Graph API.
    Expects 'data' list and 'paging' with 'next' or 'cursors.after'.
    Returns a flat list of items across pages.
    """
    params = params.copy() if params else {}
    # If caller didn't pass limit, choose a sane default
    params.setdefault("limit", 100)

    all_items: List[Dict[str, Any]] = []
    next_url: Optional[str] = None

    while True:
        try:
            if next_url:
                # When continuing with 'next' URLs, we pass full URL and ignore params
                resp = await make_api_request(next_url, access_token, {}, method="GET", is_full_url=True)
            else:
                resp = await make_api_request(endpoint, access_token, params, method="GET")
        except Exception as e:
            # Break on first failure (callers can decide how to proceed)
            break

        if not isinstance(resp, dict):
            break

        data = resp.get("data", [])
        if not isinstance(data, list):
            data = []

        all_items.extend(data)

        paging = resp.get("paging", {})
        next_url = paging.get("next")
        if not next_url:
            break

    return all_items


def _dedupe_by_id(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate a list of dicts by the 'id' field, preserving order."""
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        _id = str(it.get("id")) if it and "id" in it else None
        if _id and _id not in seen:
            seen.add(_id)
            out.append(it)
    return out


@mcp_server.tool()
@meta_api_tool
async def list_linked_pages(
    account_id: str,
    access_token: Optional[str] = None,
    include_client_pages: bool = True,
    include_owned_pages: bool = True,
    fallback_via_me_accounts: bool = True,
    fields: Optional[List[str]] = None,
    limit: int = 200
) -> str:
    """
    List Facebook Pages linked to the ad account/business.

    Strategy:
      1) Resolve Business from the Ad Account: GET /act_<ID>?fields=business
      2) If a Business exists, fetch:
         - /<BUSINESS_ID>/owned_pages
         - /<BUSINESS_ID>/client_pages (optional via include_client_pages)
      3) If no Business (or insufficient permission), fallback to /me/accounts (optional).
      4) Deduplicate by Page ID, and return compact objects with requested fields.

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph API token with sufficient scopes.
        include_client_pages: Include client_pages when business exists.
        include_owned_pages: Include owned_pages when business exists.
        fallback_via_me_accounts: If no business or permission, fallback to /me/accounts.
        fields: Optional list of Page fields to request (e.g., ["id","name","category","link"]).
                Defaults to ["id","name"] for safety.
        limit: Page size for each request (pagination is handled until exhaustion).

    Returns:
        JSON string:
        {
          "pages": [{"id":"...","name":"...", ...}, ...],
          "source": {
            "business_id": "<ID or null>",
            "used_edges": ["owned_pages","client_pages"] or [],
            "fallback_used": true|false
          }
        }
    """
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_1234567890')"}, indent=2)

    biz_id = await _get_business_id_for_account(account_id, access_token)

    # Build fields param
    req_fields = fields[:] if fields else ["id", "name"]
    fields_param = ",".join(req_fields)

    collected: List[Dict[str, Any]] = []
    used_edges: List[str] = []
    fallback_used = False

    # Prefer Business edges when available
    if biz_id:
        if include_owned_pages:
            endpoint = f"{biz_id}/owned_pages"
            items = await _fetch_all_pages(endpoint, access_token, {"fields": fields_param, "limit": limit})
            if items:
                used_edges.append("owned_pages")
                collected.extend(items)

        if include_client_pages:
            endpoint = f"{biz_id}/client_pages"
            items = await _fetch_all_pages(endpoint, access_token, {"fields": fields_param, "limit": limit})
            if items:
                used_edges.append("client_pages")
                collected.extend(items)

    # Fallback: /me/accounts (user-bound)
    if (not collected) and fallback_via_me_accounts:
        endpoint = "me/accounts"
        items = await _fetch_all_pages(endpoint, access_token, {"fields": fields_param, "limit": limit})
        if items:
            fallback_used = True
            collected.extend(items)

    result = {
        "pages": _dedupe_by_id(collected),
        "source": {
            "business_id": biz_id,
            "used_edges": used_edges,
            "fallback_used": fallback_used
        }
    }
    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def list_linked_instagram_accounts(
    account_id: str,
    access_token: Optional[str] = None,
    include_client_accounts: bool = True,
    include_owned_accounts: bool = True,
    fallback_via_pages: bool = True,
    page_fields_for_fallback: Optional[List[str]] = None,
    limit: int = 200
) -> str:
    """
    List Instagram Business Accounts linked to the ad account/business.

    Strategy:
      1) Resolve Business from the Ad Account: GET /act_<ID>?fields=business
      2) If a Business exists, fetch:
         - /<BUSINESS_ID>/owned_instagram_accounts
         - /<BUSINESS_ID>/client_instagram_accounts (optional)
      3) If none found (or insufficient permission) and fallback is enabled:
         - Enumerate pages (via list_linked_pages-like logic) and for each Page:
           GET /<PAGE_ID>?fields=connected_instagram_account{id,username}
         (Some integrations use 'instagram_accounts' edge; we try 'connected_instagram_account' first.)
      4) Deduplicate by IG user id and return id/username pairs.

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph API token with sufficient scopes.
        include_client_accounts: Include client_instagram_accounts (when Business exists).
        include_owned_accounts: Include owned_instagram_accounts (when Business exists).
        fallback_via_pages: Fallback via pages' connected Instagram account if business edges fail/empty.
        page_fields_for_fallback: Fields to request for pages during fallback (defaults to ["id","name"]).
        limit: Page size for each request (pagination is handled until exhaustion).

    Returns:
        JSON string:
        {
          "instagram_accounts": [{"id":"...","username":"..."}, ...],
          "source": {
            "business_id": "<ID or null>",
            "used_edges": ["owned_instagram_accounts","client_instagram_accounts"] or [],
            "fallback_used": true|false
          }
        }

    Notes:
        Required scopes vary (e.g., business_management, pages_show_list, instagram_basic).
        In some setups, IG accounts are surfaced only via Page bindings (connected_instagram_account).
    """
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_1234567890')"}, indent=2)

    biz_id = await _get_business_id_for_account(account_id, access_token)

    ig_items: List[Dict[str, Any]] = []
    used_edges: List[str] = []
    fallback_used = False

    # Try Business edges first
    if biz_id:
        if include_owned_accounts:
            endpoint = f"{biz_id}/owned_instagram_accounts"
            items = await _fetch_all_pages(endpoint, access_token, {"fields": "id,username", "limit": limit})
            if items:
                used_edges.append("owned_instagram_accounts")
                ig_items.extend(items)

        if include_client_accounts:
            endpoint = f"{biz_id}/client_instagram_accounts"
            items = await _fetch_all_pages(endpoint, access_token, {"fields": "id,username", "limit": limit})
            if items:
                used_edges.append("client_instagram_accounts")
                ig_items.extend(items)

    # Fallback via pages (connected_instagram_account per page)
    if (not ig_items) and fallback_via_pages:
        # Reuse the list_linked_pages logic to enumerate candidate pages
        page_fields = page_fields_for_fallback[:] if page_fields_for_fallback else ["id", "name"]
        pages_resp = await list_linked_pages(
            account_id=account_id,
            access_token=access_token,
            include_client_pages=True,
            include_owned_pages=True,
            fallback_via_me_accounts=True,
            fields=page_fields,
            limit=limit
        )
        try:
            pages_data = json.loads(pages_resp)
            pages = pages_data.get("pages", [])
        except Exception:
            pages = []

        # For each page, attempt to fetch the connected IG account
        for p in pages:
            pid = p.get("id")
            if not pid:
                continue
            try:
                endpoint = f"{pid}"
                params = {"fields": "connected_instagram_account{id,username}"}
                data = await make_api_request(endpoint, access_token, params, method="GET")
                cia = (data or {}).get("connected_instagram_account")
                if isinstance(cia, dict) and "id" in cia:
                    ig_items.append({"id": str(cia.get("id")), "username": cia.get("username")})
            except Exception:
                # Ignore errors per page to keep best-effort behavior
                continue

        if ig_items:
            fallback_used = True

    result = {
        "instagram_accounts": _dedupe_by_id(ig_items),
        "source": {
            "business_id": biz_id,
            "used_edges": used_edges,
            "fallback_used": fallback_used
        }
    }
    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def list_custom_audiences(
    account_id: str,
    access_token: Optional[str] = None,
    include_saved_audiences: bool = True,
    include_lookalikes_bucket: bool = True,
    ca_fields: Optional[List[str]] = None,
    sa_fields: Optional[List[str]] = None,
    limit: int = 200,
    name_contains: Optional[str] = None,
    subtype_in: Optional[List[str]] = None,   # e.g., ["CUSTOM", "WEBSITE", "APP", "LOOKALIKE", "PARTNER"]
) -> str:
    """
    List audiences available on the Ad Account:
      - Custom Audiences (including all subtypes)
      - Saved Audiences (optional)
      - A separate "lookalike_audiences" bucket derived from Custom Audiences (optional)

    Notes:
      - Custom Audiences are read from:  GET /act_<ID>/customaudiences
      - Saved Audiences are read from:   GET /act_<ID>/saved_audiences
      - Lookalikes are Custom Audiences with `subtype == LOOKALIKE` or a non-empty `lookalike_spec`.
      - We do not mutate/soft-filter by "availability" or "delivery_status" beyond what the API returns.

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph API token with required scopes.
        include_saved_audiences: If True, also fetch Saved Audiences.
        include_lookalikes_bucket: If True, return a separate "lookalike_audiences" list for convenience.
        ca_fields: Fields to request for Custom Audiences. If None, a safe default is used.
        sa_fields: Fields to request for Saved Audiences. If None, a safe default is used.
        limit: Page size for pagination (we keep fetching until exhausted).
        name_contains: Optional case-insensitive substring filter on audience name (client-side).
        subtype_in: Optional whitelist of custom audience subtypes (client-side filter).

    Returns:
        JSON string:
        {
          "custom_audiences": [...],
          "lookalike_audiences": [...],   # only if include_lookalikes_bucket
          "saved_audiences": [...],       # only if include_saved_audiences
          "counts": {"custom": N, "lookalike": L, "saved": S}
        }
    """
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_1234567890')"}, indent=2)

    # Default fields chosen to be informative yet compact.
    ca_fields = ca_fields or [
        "id", "name", "subtype", "approximate_count",
        "delivery_status", "operation_status", "lookalike_spec",
        "is_value_based", "retention_days", "time_updated"
    ]
    sa_fields = sa_fields or [
        "id", "name", "approximate_count", "time_updated", "description"
    ]

    # Fetch Custom Audiences
    ca_items = await _fetch_all_pages(
        endpoint=f"{account_id}/customaudiences",
        access_token=access_token,
        params={"fields": ",".join(ca_fields), "limit": limit}
    )

    # Optional client-side filters
    def _match_filters(obj: Dict[str, Any]) -> bool:
        if name_contains and name_contains.lower() not in (obj.get("name") or "").lower():
            return False
        if subtype_in:
            if str(obj.get("subtype") or "").upper() not in {s.upper() for s in subtype_in}:
                return False
        return True

    ca_items = [it for it in ca_items if _match_filters(it)]
    ca_items = _dedupe_by_id(ca_items)

    # Derive Lookalikes bucket
    lookalikes: List[Dict[str, Any]] = []
    if include_lookalikes_bucket:
        for it in ca_items:
            subtype = str(it.get("subtype") or "").upper()
            if subtype == "LOOKALIKE" or (it.get("lookalike_spec") not in (None, {}, [])):
                lookalikes.append(it)

    # Fetch Saved Audiences (optional)
    saved_items: List[Dict[str, Any]] = []
    if include_saved_audiences:
        saved_items = await _fetch_all_pages(
            endpoint=f"{account_id}/saved_audiences",
            access_token=access_token,
            params={"fields": ",".join(sa_fields), "limit": limit}
        )
        saved_items = _dedupe_by_id(saved_items)

    result = {
        "custom_audiences": ca_items,
        **({"lookalike_audiences": lookalikes} if include_lookalikes_bucket else {}),
        **({"saved_audiences": saved_items} if include_saved_audiences else {}),
        "counts": {
            "custom": len(ca_items),
            "lookalike": len(lookalikes) if include_lookalikes_bucket else 0,
            "saved": len(saved_items) if include_saved_audiences else 0
        }
    }
    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def list_pixels(
    account_id: str,
    access_token: Optional[str] = None,
    # If provided, fetch exactly these pixels (GET /<PIXEL_ID>); otherwise use /act_<ID>/adspixels
    pixel_ids: Optional[List[str]] = None,
    # Fields for pixels; a compact, useful default is provided
    fields: Optional[List[str]] = None,
    limit: int = 200,
    # Optional client-side filters
    name_contains: Optional[str] = None,
    # Optionally include observed event counts per pixel (same aggregation used by Ads Manager)
    with_event_counts: bool = False,
    aggregation: str = "event_total_counts"
) -> str:
    """
    List Pixels available to an Ad Account, with optional per-pixel event counts.

    Endpoints used:
      - When pixel_ids is None:   GET /act_<ID>/adspixels
      - When pixel_ids provided:  GET /<PIXEL_ID>
      - When with_event_counts:   GET /<PIXEL_ID>/stats?aggregation=event_total_counts

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph API token with sufficient scopes.
        pixel_ids: Optional explicit list of pixel IDs to fetch individually.
        fields: Pixel fields to request. Defaults to a compact set if None.
        limit: Page size for list calls; pagination continues until exhausted.
        name_contains: Optional case-insensitive substring filter on pixel name (client-side).
        with_event_counts: If True, also fetch observed event counts for each pixel.
        aggregation: Aggregation used for stats (default "event_total_counts").

    Returns:
        JSON string:
        {
          "pixels": [{"id":"...","name":"...","code":"...","last_fired_time":"...", ...}, ...],
          "pixel_event_counts": { "<PIXEL_ID>": [{"event":"Purchase","count":123}, ...], ... }  # only if with_event_counts
        }

    Notes:
        - Event counts are observational (past fires), not a guarantee for future availability.
        - Permissions: typically requires ads_management plus appropriate business/page permissions.
    """
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_1234567890')"}, indent=2)

    # Default pixel fields: informative yet compact. Add/remove as needed.
    fields = fields or [
        "id",
        "name",
        "code",
        "creation_time",
        "last_fired_time",
        "owner_ad_account",
        "owner_business"
    ]
    fields_param = ",".join(fields)

    pixels: List[Dict[str, Any]] = []

    # Case 1: explicit pixel_ids provided → fetch individually
    if pixel_ids:
        unique_ids = []
        seen = set()
        for pid in pixel_ids:
            sid = str(pid).strip()
            if sid and sid not in seen:
                seen.add(sid)
                unique_ids.append(sid)
        for pid in unique_ids:
            try:
                endpoint = f"{pid}"
                data = await make_api_request(endpoint, access_token, {"fields": fields_param}, method="GET")
                if isinstance(data, dict) and data.get("id"):
                    pixels.append(data)
            except Exception as e:
                pixels.append({"id": str(pid), "error": f"Failed to fetch pixel: {str(e)}"})
    else:
        # Case 2: list all pixels via the Ad Account edge with pagination
        items = await _fetch_all_pages(
            endpoint=f"{account_id}/adspixels",
            access_token=access_token,
            params={"fields": fields_param, "limit": limit}
        )
        pixels = _dedupe_by_id(items)

    # Optional client-side filter by name substring
    if name_contains:
        q = name_contains.lower()
        pixels = [p for p in pixels if q in str(p.get("name", "")).lower()]

    result: Dict[str, Any] = {"pixels": pixels}

    # Optional: per-pixel event counts (observed)
    if with_event_counts and pixels:
        event_counts: Dict[str, List[Dict[str, Any]]] = {}
        for p in pixels:
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            try:
                endpoint = f"{pid}/stats"
                params = {"aggregation": aggregation}
                stats = await make_api_request(endpoint, access_token, params, method="GET")
                rows = []
                for row in (stats or {}).get("data", []):
                    evt = row.get("event") or row.get("name") or row.get("key")
                    val = row.get("value") or row.get("count") or row.get("total")
                    if evt is not None:
                        rows.append({
                            "event": str(evt),
                            "count": int(val) if isinstance(val, (int, float)) else val
                        })
                event_counts[pid] = rows
            except Exception as e:
                event_counts[pid] = [{"error": f"Failed to read pixel stats: {str(e)}"}]
        result["pixel_event_counts"] = event_counts

    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def list_events_for_conversions(
    account_id: str,
    access_token: Optional[str] = None,
    pixel_id: Optional[str] = None,
    include_pixels: bool = True,
    include_custom_conversions: bool = True,
    include_pixel_event_counts: bool = True,
    aggregation: str = "event_total_counts",   # per Ads Pixel Stats; surfaces observed event names
    application_id: Optional[str] = None,      # optional – see notes below
    limit: int = 200
) -> str:
    """
    Enumerate conversion sources/events you can optimize for (website & app).

    What this returns:
      - pixels: List of Pixels on the Ad Account (GET /act_<ID>/adspixels).
      - pixel_event_counts: If enabled, for each pixel listed (or only `pixel_id` if provided),
        query /<PIXEL_ID>/stats?aggregation=event_total_counts and return observed event names
        with counts (helps pick "configured" events).
      - custom_conversions: List of Custom Conversions on the Ad Account (GET /act_<ID>/customconversions),
        including their `custom_event_type` and linked `pixel` if present.
      - application: If `application_id` is provided, we return it verbatim and DO NOT attempt to fetch
        app events from Graph Marketing API, since there is no direct, reliable listing endpoint for
        app event names via the Marketing API. App-side event catalogs are typically managed via the
        Analytics/App SDK and surfaced in Ads Manager, not via this endpoint.

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph API token with required scopes.
        pixel_id: If provided, restrict pixel stats to this Pixel; otherwise include all account pixels.
        include_pixels: Include a list of pixels on the ad account.
        include_custom_conversions: Include ad account custom conversions.
        include_pixel_event_counts: If True, fetch observed event counts via Ads Pixel Stats.
        aggregation: Aggregation for Ads Pixel Stats (default "event_total_counts").
        application_id: Optional app id; returned for context only (see note above).
        limit: Page size for pagination where applicable.

    Returns:
        JSON string:
        {
          "pixels": [{"id":"...", "name":"...", ...}],
          "pixel_event_counts": {
            "<PIXEL_ID>": [{"event":"Purchase","count":1234}, ...],
            ...
          },
          "custom_conversions": [{"id":"...","name":"...","custom_event_type":"PURCHASE", "pixel":{"id":"...","name":"..."}}, ...],
          "application": {"id":"<APP_ID>"}   # only if provided
        }

    Caveats:
        - Ads Pixel Stats returns *observed* events (standard+custom) with counts; it does not guarantee
          future availability. Use this to guide selection of optimization events.
        - App events listing is not exposed as a simple Marketing API edge; selection for App campaigns
          is typically done by passing the correct application_id + store URL and choosing optimization
          goals supported for app installs (SKAdNetwork flows etc.).
    """
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_1234567890')"}, indent=2)

    out: Dict[str, Any] = {}

    # Pixels on account
    pixels: List[Dict[str, Any]] = []
    if include_pixels:
        pixels = await _fetch_all_pages(
            endpoint=f"{account_id}/adspixels",
            access_token=access_token,
            params={"fields": "id,name,code,last_fired_time", "limit": limit}
        )
        pixels = _dedupe_by_id(pixels)
        out["pixels"] = pixels

    # If a single pixel_id was requested, ensure it's present (or add a stub entry)
    pixel_ids: List[str] = []
    if pixel_id:
        pixel_ids = [str(pixel_id)]
        # Optionally include minimal pixel info if not already fetched above
        if include_pixels and all(p.get("id") != pixel_id for p in pixels):
            out.setdefault("pixels", []).append({"id": str(pixel_id)})
    else:
        # Use all account pixels if no specific pixel_id is provided
        if include_pixels:
            pixel_ids = [str(p.get("id")) for p in (pixels or []) if p.get("id")]

    # Pixel event counts via Ads Pixel Stats
    if include_pixel_event_counts and pixel_ids:
        # Returns observed events (standard/custom) + counts; good proxy for "configured" events.
        # Endpoint: GET /<PIXEL_ID>/stats?aggregation=event_total_counts
        event_counts: Dict[str, List[Dict[str, Any]]] = {}
        for pid in pixel_ids:
            try:
                endpoint = f"{pid}/stats"
                params = {"aggregation": aggregation}
                stats = await make_api_request(endpoint, access_token, params, method="GET")
                # Expecting something like: {"data":[{"event":"Purchase","value":123}, ...], ...}
                rows = []
                for row in (stats or {}).get("data", []):
                    evt = row.get("event") or row.get("name") or row.get("key")
                    val = row.get("value") or row.get("count") or row.get("total")
                    if evt:
                        rows.append({"event": str(evt), "count": int(val) if isinstance(val, (int, float)) else val})
                event_counts[pid] = rows
            except Exception as e:
                event_counts[pid] = [{"error": f"Failed to read pixel stats: {str(e)}"}]
        out["pixel_event_counts"] = event_counts

    # Custom Conversions on account
    if include_custom_conversions:
        cc_fields = [
            "id", "name", "custom_event_type", "is_archived",
            "creation_time", "last_fired_time", "event_source_type",
            "pixel{id,name}"
        ]
        custom_convs = await _fetch_all_pages(
            endpoint=f"{account_id}/customconversions",
            access_token=access_token,
            params={"fields": ",".join(cc_fields), "limit": limit}
        )
        out["custom_conversions"] = _dedupe_by_id(custom_convs)

    # Application context (optional)
    if application_id:
        # We intentionally do not attempt to list app events via Marketing API.
        # Returning the id provides context; creative/ad set creation should provide
        # application_id + store URL(s) for App Promotion flows.
        out["application"] = {"id": str(application_id)}

    return json.dumps(out, indent=2)


@mcp_server.tool()
@meta_api_tool
async def list_app_candidates(
    account_id: str,
    access_token: Optional[str] = None,
    include_owned_apps: bool = True,
    include_client_apps: bool = True,
    include_object_store_urls: bool = True,
    # Client-side filters:
    name_contains: Optional[str] = None,
    platform: Optional[str] = None,  # "ios" | "android" | None
    limit: int = 200
) -> str:
    """
    List Business apps that can be used as candidates for App Promotion flows.

    Strategy:
      1) Resolve the Business that owns the Ad Account (GET /act_<ID>?fields=business).
      2) If a Business exists, fetch apps from:
         - /<BUSINESS_ID>/owned_apps           (when include_owned_apps=True)
         - /<BUSINESS_ID>/client_apps          (when include_client_apps=True)
      3) If include_object_store_urls=True, attempt to include Application.object_store_urls.
         If not returned at the edge level, fetch per-app fallback: GET /<APP_ID>?fields=object_store_urls.
      4) Apply optional client-side filters (name substring, platform).
      5) Return a compact JSON with id, name, namespace, and normalized store URLs.

    Notes:
      - The Business edges `owned_apps` and `client_apps` are the canonical way to enumerate apps
        linked to a Business for advertising use cases.
      - When building Ad Sets with promoted_object for App Promotion, Meta requires that an
        object_store_url be associated with the same application_id. We surface `object_store_urls`
        to help you pick the correct store URL per platform.

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph API token with adequate scopes (e.g., business_management).
        include_owned_apps: Include apps owned by the business.
        include_client_apps: Include client apps accessible to the business.
        include_object_store_urls: Try to include Application.object_store_urls for each app.
        name_contains: Optional case-insensitive substring filter on app name.
        platform: Optional platform filter: "ios" or "android".
        limit: Page size for list endpoints. Pagination continues until exhaustion.

    Returns:
        JSON string:
        {
          "apps": [
            {
              "id": "123",
              "name": "My App",
              "namespace": "myapp",
              "store_urls": {
                "ios": "https://apps.apple.com/app/idXXXXXXXXX",       # when available
                "android": "https://play.google.com/store/apps/details?id=..."
              }
            },
            ...
          ],
          "source": {
            "business_id": "<ID or null>",
            "used_edges": ["owned_apps","client_apps"] or [],
            "fetched_object_store_urls": true|false
          }
        }
    """
    # Resolve Business from Ad Account
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_1234567890')"}, indent=2)

    biz_id = await _get_business_id_for_account(account_id, access_token)
    if not biz_id:
        # Without a business, there is no canonical Business apps listing.
        # We return a structured error so the caller can decide next steps.
        return json.dumps({
            "error": "No business found for this ad account",
            "details": "Apps are typically enumerated via Business edges (owned_apps/client_apps).",
            "account_id": account_id
        }, indent=2)

    # Fetch apps from Business edges
    apps: List[Dict[str, Any]] = []
    used_edges: List[str] = []

    fields = ["id", "name", "namespace"]
    if include_object_store_urls:
        # Application.object_store_urls is a typed field; if the edge omits it,
        # we will fallback per-app below.
        fields.append("object_store_urls")

    fields_param = ",".join(fields)

    if include_owned_apps:
        items = await _fetch_all_pages(
            endpoint=f"{biz_id}/owned_apps",
            access_token=access_token,
            params={"fields": fields_param, "limit": limit}
        )
        if items:
            used_edges.append("owned_apps")
            apps.extend(items)

    if include_client_apps:
        items = await _fetch_all_pages(
            endpoint=f"{biz_id}/client_apps",
            access_token=access_token,
            params={"fields": fields_param, "limit": limit}
        )
        if items:
            used_edges.append("client_apps")
            apps.extend(items)

    # Deduplicate by app id
    apps = _dedupe_by_id(apps)

    # Optional per-app fallback for object_store_urls
    fetched_per_app = False
    if include_object_store_urls:
        need_fetch = [a for a in apps if not a.get("object_store_urls")]
        if need_fetch:
            fetched_per_app = True
            for a in need_fetch:
                app_id = str(a.get("id"))
                if not app_id:
                    continue
                try:
                    # GET /<APP_ID>?fields=object_store_urls
                    resp = await make_api_request(
                        endpoint=f"{app_id}",
                        access_token=access_token,
                        params={"fields": "object_store_urls"},
                        method="GET"
                    )
                    if isinstance(resp, dict) and resp.get("object_store_urls"):
                        a["object_store_urls"] = resp["object_store_urls"]
                except Exception:
                    # Best-effort: ignore failures for some apps
                    continue

    # Normalize store URLs and apply client-side filters
    def _normalize_store_urls(app_obj: Dict[str, Any]) -> Dict[str, Optional[str]]:
        urls = app_obj.get("object_store_urls") or {}
        ios_url = None
        android_url = None

        # Depending on the API response shape, object_store_urls may be:
        # - {"ios": "...", "android": "..."} or similar typed object
        # We handle common cases defensively.
        if isinstance(urls, dict):
            ios_url = urls.get("ios") or urls.get("iphone") or urls.get("ipad")
            android_url = urls.get("android")
            # Some integrations embed lists; pick the first string if needed
            if isinstance(ios_url, list) and ios_url:
                ios_url = ios_url[0]
            if isinstance(android_url, list) and android_url:
                android_url = android_url[0]

        return {"ios": ios_url, "android": android_url}

    filtered: List[Dict[str, Any]] = []
    for a in apps:
        name = a.get("name") or ""
        if name_contains and name_contains.lower() not in name.lower():
            continue

        store_urls = _normalize_store_urls(a) if include_object_store_urls else {"ios": None, "android": None}

        if platform:
            p = platform.lower()
            if p == "ios" and not store_urls.get("ios"):
                continue
            if p == "android" and not store_urls.get("android"):
                continue

        filtered.append({
            "id": str(a.get("id")),
            "name": name,
            "namespace": a.get("namespace"),
            "store_urls": store_urls
        })

    result = {
        "apps": filtered,
        "source": {
            "business_id": biz_id,
            "used_edges": used_edges,
            "fetched_object_store_urls": fetched_per_app
        }
    }
    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_adsets(access_token: str = None, account_id: str = None, limit: int = 10, campaign_id: str = "") -> str:
    """
    Get ad sets for a Meta Ads account with optional filtering by campaign.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
        limit: Maximum number of ad sets to return (default: 10)
        campaign_id: Optional campaign ID to filter by
    """
    # If no account ID is specified, try to get the first one for the user
    if not account_id:
        accounts_json = await get_ad_accounts("me", json.dumps({"limit": 1}), access_token)
        accounts_data = json.loads(accounts_json)
        
        if "data" in accounts_data and accounts_data["data"]:
            account_id = accounts_data["data"][0]["id"]
        else:
            return json.dumps({"error": "No account ID specified and no accounts found for user"}, indent=2)
    
    # Change endpoint based on whether campaign_id is provided
    if campaign_id:
        endpoint = f"{campaign_id}/adsets"
        params = {
            "fields": "id,name,campaign_id,status,daily_budget,lifetime_budget,targeting,bid_amount,bid_strategy,optimization_goal,billing_event,start_time,end_time,created_time,updated_time,frequency_control_specs{event,interval_days,max_frequency}",
            "limit": limit
        }
    else:
        # Use account endpoint if no campaign_id is given
        endpoint = f"{account_id}/adsets"
        params = {
            "fields": "id,name,campaign_id,status,daily_budget,lifetime_budget,targeting,bid_amount,bid_strategy,optimization_goal,billing_event,start_time,end_time,created_time,updated_time,frequency_control_specs{event,interval_days,max_frequency}",
            "limit": limit
        }
        # Note: Removed the attempt to add campaign_id to params for the account endpoint case, 
        # as it was ineffective and the logic now uses the correct endpoint for campaign filtering.

    data = await make_api_request(endpoint, access_token, params)
    
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_adset_details(access_token: str = None, adset_id: str = None) -> str:
    """
    Get detailed information about a specific ad set.
    
    Args:
        adset_id: Meta Ads ad set ID (required)
        access_token: Meta API access token (optional - will use cached token if not provided)
    
    Example:
        To call this function through MCP, pass the adset_id as the first argument:
        {
            "args": "YOUR_ADSET_ID"
        }
    """
    if not adset_id:
        return json.dumps({"error": "No ad set ID provided"}, indent=2)
    
    endpoint = f"{adset_id}"
    # Explicitly prioritize frequency_control_specs in the fields request
    params = {
        "fields": "id,name,campaign_id,status,frequency_control_specs{event,interval_days,max_frequency},daily_budget,lifetime_budget,targeting,bid_amount,bid_strategy,optimization_goal,billing_event,start_time,end_time,created_time,updated_time,attribution_spec,destination_type,promoted_object,pacing_type,budget_remaining,dsa_beneficiary"
    }
    
    data = await make_api_request(endpoint, access_token, params)
    
    # For debugging - check if frequency_control_specs was returned
    if 'frequency_control_specs' not in data:
        data['_meta'] = {
            'note': 'No frequency_control_specs field was returned by the API. This means either no frequency caps are set or the API did not include this field in the response.'
        }
    
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_adset(
    account_id: str = None,
    campaign_id: str = None,
    name: str = None,
    status: str = "PAUSED",
    # Ad set-level budgets (ABO). If both are None, we assume CBO at the campaign.
    daily_budget: Optional[str] = None,           # integer string (minor units)
    lifetime_budget: Optional[str] = None,        # integer string (minor units)
    # Delivery & bidding
    targeting: Dict[str, Any] = None,             # Raw targeting spec (locations/age/gender/interests/placements/etc.)
    optimization_goal: str = None,                # e.g. LEAD_GENERATION, OFFSITE_CONVERSIONS, APP_INSTALLS
    billing_event: str = None,                    # e.g. IMPRESSIONS, LINK_CLICKS
    bid_amount: Optional[str] = None,             # minor units; depends on strategy
    bid_strategy: Optional[str] = None,           # only meaningful at Ad Set when using ABO
    # Flighting & dayparting
    start_time: Optional[str] = None,             # ISO-8601; required with lifetime_budget if scheduling
    end_time: Optional[str] = None,               # ISO-8601; required with lifetime_budget if scheduling
    adset_schedule: Optional[List[Dict[str, int]]] = None,  # dayparting blocks (requires lifetime_budget)
    # Compliance / regionals
    dsa_beneficiary: Optional[str] = None,        # DSA compliance (see API/SDK; region-specific)
    # Objective-specific
    promoted_object: Dict[str, Any] = None,       # JSON-encoded by us
    destination_type: Optional[str] = None,       # Leave as-is; API validates (WEBSITE/APP/WHATSAPP/ON_AD/SHOP_AUTOMATIC/…)
    # Less common knobs (accepted by API; keep pass-through but avoid over-validating):
    pacing_type: List[str] = None,
    time_based_ad_rotation_id_blocks: List[List[str]] = None,
    time_based_ad_rotation_intervals: List[int] = None,
    campaign_attribution: Dict[str, Any] = None,
    optimization_sub_event: str = None,
    attribution_spec: List[Dict[str, str]] = None,
    execution_options: List[str] = None,
    bid_constraints: Dict[str, Any] = None,
    access_token: str = None
) -> str:
    """
    Create a Meta Ads Ad Set with guardrails for budgets, scheduling (dayparting),
    bidding, and promoted_object depending on the optimization use case.

    Key behaviors:
      - Budget scope:
        * ABO (ad set budgets): provide exactly one of daily_budget OR lifetime_budget.
        * CBO (campaign budget): if both budgets are None, assume budget is at Campaign.
      - Dayparting:
        * adset_schedule is only allowed when lifetime_budget is provided at Ad Set level,
          and requires start_time and end_time (ISO-8601). This mirrors Ads Manager behavior.
      - Bidding:
        * With ABO, you may set bid_strategy/bid_amount here.
        * With CBO, prefer bid strategy on Campaign; per-ad set caps can be set later via
          campaign-level adset_bid_amounts if using COST_CAP / BID_CAP strategies.
      - Promoted object (best-practice constraints):
        * Lead Gen (optimization_goal=LEAD_GENERATION): require promoted_object.page_id.
        * App Promotion (optimization_goal=APP_INSTALLS or dest to app store): require promoted_object.application_id
          and a valid app store URL (Apple/Google).
        * Website Sales/Conversions (e.g., OFFSITE_CONVERSIONS): require promoted_object.pixel_id
          (or custom_conversion_id/custom_event_type) per your flow.

    Notes:
      - Destination type values vary by product (e.g., WEBSITE, APP, MESSENGER, WHATSAPP, SHOP_AUTOMATIC, ON_AD, ON_POST...).
        We forward what you provide and let the API validate to avoid false negatives.
      - Advantage+ placements: leaving placements unspecified in `targeting` keeps automatic placements on
        (you can still pass placements if you want to override).
    """
    # Required basics
    if not account_id:
        return json.dumps({"error": "No account ID provided"}, indent=2)
    if not campaign_id:
        return json.dumps({"error": "No campaign ID provided"}, indent=2)
    if not name:
        return json.dumps({"error": "No ad set name provided"}, indent=2)
    if not optimization_goal:
        return json.dumps({"error": "No optimization goal provided"}, indent=2)
    if not billing_event:
        return json.dumps({"error": "No billing event provided"}, indent=2)

    # Budget scope: ABO vs CBO
    abo = (daily_budget is not None) or (lifetime_budget is not None)
    if abo and (daily_budget is not None) and (lifetime_budget is not None):
        return json.dumps({"error": "Provide exactly one: daily_budget OR lifetime_budget (not both)"}, indent=2)

    # Dayparting guardrails
    # Ad scheduling is only supported with a lifetime budget at Ad Set level and requires start/end.
    if adset_schedule is not None:
        if not lifetime_budget:
            return json.dumps({"error": "adset_schedule requires lifetime_budget at Ad Set level"}, indent=2)
        if not start_time or not end_time:
            return json.dumps({"error": "adset_schedule requires both start_time and end_time (ISO-8601)"}, indent=2)

    # Bidding guardrails
    # With CBO, discourage setting bid_strategy at Ad Set level.
    if not abo and bid_strategy:
        return json.dumps({
            "error": "With campaign-level budgets (CBO), set bid_strategy on the Campaign; use adset_bid_amounts later if needed."
        }, indent=2)

    # Promoted object guardrails (best-effort; pragmatic)
    po = promoted_object or {}
    goal = (optimization_goal or "").strip().upper()

    if goal == "LEAD_GENERATION":
        if "page_id" not in po:
            return json.dumps({"error": "For LEAD_GENERATION, promoted_object.page_id is required"}, indent=2)

    # App Promotion: require application_id and a valid store URL
    if goal == "APP_INSTALLS" or (
    destination_type in {"APP", "APP_STORE", "DEEPLINK"} if destination_type else False):
        if not po:
            return json.dumps({
                "error": "promoted_object is required for App Promotion",
                "required_fields": ["application_id", "object_store_url"]
            }, indent=2)
        if "application_id" not in po:
            return json.dumps({"error": "promoted_object missing required field: application_id"}, indent=2)
        if "object_store_url" not in po:
            return json.dumps({"error": "promoted_object missing required field: object_store_url"}, indent=2)
        valid_store_patterns = ("apps.apple.com", "itunes.apple.com", "play.google.com")
        if not any(host in po["object_store_url"] for host in valid_store_patterns):
            return json.dumps({
                "error": "Invalid object_store_url; must point to Apple App Store or Google Play",
                "provided_url": po.get("object_store_url")
            }, indent=2)

    warnings: List[str] = []

    # Normalize targeting.user_os into a lowercased set {"ios", "android"}
    selected_os: Set[str] = set()
    try:
        raw_user_os = (targeting or {}).get("user_os", [])
        if isinstance(raw_user_os, str):
            raw_user_os = [raw_user_os]
        for v in raw_user_os:
            u = str(v).lower()
            if "ios" in u: selected_os.add("ios")
            if "android" in u: selected_os.add("android")
    except Exception:
        pass

    store_url = str((promoted_object or {}).get("object_store_url") or "")

    def _is_ios_store(u: str) -> bool:
        return ("apps.apple.com" in u.lower()) or ("itunes.apple.com" in u.lower())

    def _is_android_store(u: str) -> bool:
        return "play.google.com" in u.lower()

    if store_url:
        if selected_os == {"ios"} and not _is_ios_store(store_url):
            warnings.append("iOS-only targeting detected but object_store_url is not an Apple App Store link.")
        if selected_os == {"android"} and not _is_android_store(store_url):
            warnings.append("Android-only targeting detected but object_store_url is not a Google Play link.")
        if selected_os == {"ios", "android"} and (_is_ios_store(store_url) or _is_android_store(store_url)):
            warnings.append("Mixed iOS+Android targeting with a single store URL; consider splitting into two ad sets.")

    # Website Sales/Conversions: require pixel or custom conversion context
    if goal in {"OFFSITE_CONVERSIONS", "CONVERSIONS"}:
        if not any(k in po for k in ("pixel_id", "custom_conversion_id", "custom_event_type")):
            return json.dumps({
                "error": "For website conversions, supply promoted_object.pixel_id or a custom conversion/event context"
            }, indent=2)

    # Default targeting (keep permissive; Advantage+ placements by omission)
    if not targeting:
        targeting = {
            "age_min": 18,
            "age_max": 65,
            "geo_locations": {"countries": ["BR"]},
            "targeting_automation": {"advantage_audience": 1}
        }

    # Build request
    endpoint = f"{account_id}/adsets"
    params = {
        "name": name,
        "campaign_id": campaign_id,
        "status": status,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "targeting": json.dumps(targeting)
    }

    # Budgets & scheduling
    if daily_budget is not None:
        params["daily_budget"] = str(daily_budget)
    if lifetime_budget is not None:
        params["lifetime_budget"] = str(lifetime_budget)
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    if adset_schedule is not None:
        params["adset_schedule"] = json.dumps(adset_schedule)

    # Bidding (when ABO)
    if abo:
        if bid_amount is not None:
            params["bid_amount"] = str(bid_amount)
        if bid_strategy:
            params["bid_strategy"] = bid_strategy

    # Destination & promoted object
    if promoted_object:
        params["promoted_object"] = json.dumps(promoted_object)
    if destination_type:
        params["destination_type"] = destination_type

    # Less common/advanced fields (pass-through)
    if dsa_beneficiary:
        params["dsa_beneficiary"] = dsa_beneficiary
    if pacing_type:
        params["pacing_type"] = json.dumps(pacing_type)
    if time_based_ad_rotation_id_blocks:
        params["time_based_ad_rotation_id_blocks"] = json.dumps(time_based_ad_rotation_id_blocks)
    if time_based_ad_rotation_intervals:
        params["time_based_ad_rotation_intervals"] = json.dumps(time_based_ad_rotation_intervals)
    if campaign_attribution:
        params["campaign_attribution"] = json.dumps(campaign_attribution)
    if optimization_sub_event:
        params["optimization_sub_event"] = optimization_sub_event
    if attribution_spec:
        params["attribution_spec"] = json.dumps(attribution_spec)
    if execution_options:
        params["execution_options"] = json.dumps(execution_options)
    if bid_constraints:
        params["bid_constraints"] = json.dumps(bid_constraints)

    # Execute
    try:
        data = await make_api_request(endpoint, access_token, params, method="POST")
        data["budget_scope"] = "adset" if abo else "campaign"
        data["has_dayparting"] = bool(adset_schedule)
        if warnings:
            data["warnings"] = warnings  # non-API field for operator visibility
        return json.dumps(data, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "permission" in error_msg.lower() or "insufficient" in error_msg.lower():
            return json.dumps({
                "error": "Insufficient permissions (check account/system user roles or scoped token).",
                "details": error_msg,
                "params_sent": params
            }, indent=2)
        elif "dsa_beneficiary" in error_msg.lower():
            return json.dumps({
                "error": "DSA beneficiary parameter not supported or required.",
                "details": error_msg,
                "params_sent": params
            }, indent=2)
        elif "benefits from ads" in error_msg:
            return json.dumps({
                "error": "DSA beneficiary required for European compliance.",
                "details": error_msg,
                "params_sent": params
            }, indent=2)
        else:
            return json.dumps({
                "error": "Failed to create ad set",
                "details": error_msg,
                "params_sent": params
            }, indent=2)


@mcp_server.tool()
@meta_api_tool
async def update_adset(adset_id: str, frequency_control_specs: List[Dict[str, Any]] = None, bid_strategy: str = None, 
                        bid_amount: int = None, status: str = None, targeting: Dict[str, Any] = None, 
                        optimization_goal: str = None, daily_budget = None, lifetime_budget = None, 
                        access_token: str = None) -> str:
    """
    Update an ad set with new settings including frequency caps and budgets.
    
    Args:
        adset_id: Meta Ads ad set ID
        frequency_control_specs: List of frequency control specifications 
                                 (e.g. [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 3}])
        bid_strategy: Bid strategy (e.g., 'LOWEST_COST_WITH_BID_CAP')
        bid_amount: Bid amount in account currency (in cents for USD)
        status: Update ad set status (ACTIVE, PAUSED, etc.)
        targeting: Complete targeting specifications (will replace existing targeting)
                  (e.g. {"targeting_automation":{"advantage_audience":1}, "geo_locations": {"countries": ["US"]}})
        optimization_goal: Conversion optimization goal (e.g., 'LINK_CLICKS', 'CONVERSIONS', 'APP_INSTALLS', etc.)
        daily_budget: Daily budget in account currency (in cents) as a string
        lifetime_budget: Lifetime budget in account currency (in cents) as a string
        access_token: Meta API access token (optional - will use cached token if not provided)
    """
    if not adset_id:
        return json.dumps({"error": "No ad set ID provided"}, indent=2)
    
    params = {}
    
    if frequency_control_specs is not None:
        params['frequency_control_specs'] = json.dumps(frequency_control_specs)
    
    if bid_strategy is not None:
        params['bid_strategy'] = bid_strategy
        
    if bid_amount is not None:
        params['bid_amount'] = str(bid_amount)
        
    if status is not None:
        params['status'] = status
        
    if optimization_goal is not None:
        params['optimization_goal'] = optimization_goal

    if targeting is not None:
        params['targeting'] = json.dumps(targeting) if isinstance(targeting, dict) else targeting

    if (daily_budget is not None) and (lifetime_budget is not None):
        return json.dumps({"error": "Provide exactly one: daily_budget OR lifetime_budget (not both)"}, indent=2)

    # Add budget parameters if provided
    if daily_budget is not None:
        params['daily_budget'] = str(daily_budget)
    
    if lifetime_budget is not None:
        params['lifetime_budget'] = str(lifetime_budget)
    
    if not params:
        return json.dumps({"error": "No update parameters provided"}, indent=2)

    endpoint = f"{adset_id}"
    
    try:
        # Use POST method for updates as per Meta API documentation
        data = await make_api_request(endpoint, access_token, params, method="POST")
        return json.dumps(data, indent=2)
    except Exception as e:
        error_msg = str(e)
        # Include adset_id in error for better context
        return json.dumps({
            "error": f"Failed to update ad set {adset_id}",
            "details": error_msg,
            "params_sent": params
        }, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_app_details(
        app_id: str,
        access_token: str = None
) -> str:
    """
    Retrieve details for a Meta App (used for ad campaigns).

    Args:
        app_id: The Facebook App ID to look up.
        access_token: Access token for the Graph API.

    Returns:
        App metadata including name, store links, bundle ID, etc.
    """
    endpoint = f"{app_id}"
    params = {
        "fields": "id,name,linking_uri,ios_bundle_id,android_package_name,app_store_id,website"
    }

    try:
        data = await make_api_request(endpoint, access_token, params)
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp_server.tool()
@meta_api_tool
async def list_offline_event_sets(
    account_id: str,
    access_token: Optional[str] = None,
    fields: Optional[List[str]] = None,
    limit: int = 200,
    name_contains: Optional[str] = None
) -> str:
    """
    List Offline Event Sets available to the Ad Account.

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph token.
        fields: Fields to request. Defaults to a compact useful set.
        limit: Page size for pagination.
        name_contains: Optional case-insensitive substring filter on the set name.

    Returns:
        JSON:
        {
          "offline_event_sets": [{ "id": "...", "name": "...", ... }, ...],
          "count": N
        }
    """
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_123')"}, indent=2)

    fields = fields or [
        "id", "name", "description", "event_stats", "data_sources", "owner_business"
    ]
    items = await _fetch_all_pages(
        endpoint=f"{account_id}/offline_event_sets",
        access_token=access_token,
        params={"fields": ",".join(fields), "limit": limit}
    )
    if name_contains:
        q = name_contains.lower()
        items = [it for it in items if q in str(it.get("name","")).lower()]

    result = {"offline_event_sets": _dedupe_by_id(items), "count": len(items)}
    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def share_offline_event_set_with_adaccount(
    offline_event_set_id: str,
    ad_account_id: str,
    access_token: Optional[str] = None
) -> str:
    """
    Share/attach an Offline Event Set with an Ad Account so it becomes selectable for tracking/attribution.

    Endpoint pattern:
      POST /<OFFLINE_EVENT_SET_ID>/adaccounts
      { "adaccount_id": "act_<ID>" }

    Args:
        offline_event_set_id: The Offline Event Set ID.
        ad_account_id: The Ad Account to share with (format "act_<ID>").
        access_token: Graph token.

    Returns:
        JSON with API response or a structured error.
    """
    if not offline_event_set_id:
        return json.dumps({"error":"offline_event_set_id is required"}, indent=2)
    if not ad_account_id:
        return json.dumps({"error":"ad_account_id is required (act_<ID>)"}, indent=2)

    endpoint = f"{offline_event_set_id}/adaccounts"
    params = {"adaccount_id": ad_account_id}

    try:
        data = await make_api_request(endpoint, access_token, params, method="POST")
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error":"Failed to share offline event set with ad account","details": str(e),"params_sent": params}, indent=2)
