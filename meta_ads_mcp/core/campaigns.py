"""Campaign-related functionality for Meta Ads API."""

import json
from typing import List

from .api import meta_api_tool, make_api_request
from .accounts import get_ad_accounts
from .server import mcp_server


@mcp_server.tool()
@meta_api_tool
async def get_campaigns(access_token: str = None, account_id: str = None, limit: int = 50, status_filter: str = "", after: str = "") -> str:
    """
    Get campaigns for a Meta Ads account with optional filtering.
    
    Note: By default, the Meta API returns a subset of available fields. 
    Other fields like 'effective_status', 'special_ad_categories', 
    'lifetime_budget', 'spend_cap', 'budget_remaining', 'promoted_object', 
    'source_campaign_id', etc., might be available but require specifying them
    in the API call (currently not exposed by this tool's parameters).
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
        limit: Maximum number of campaigns to return (default: 50)
        status_filter: Filter by effective status (e.g., 'ACTIVE', 'PAUSED', 'ARCHIVED').
                       Maps to the 'effective_status' API parameter, which expects an array
                       (this function handles the required JSON formatting). Leave empty for all statuses.
        after: Pagination cursor to get the next set of results
    """
    # If no account ID is specified, try to get the first one for the user
    if not account_id:
        accounts_json = await get_ad_accounts("me", json.dumps({"limit": 1}), access_token)
        accounts_data = json.loads(accounts_json)
        
        if "data" in accounts_data and accounts_data["data"]:
            account_id = accounts_data["data"][0]["id"]
        else:
            return json.dumps({"error": "No account ID specified and no accounts found for user"}, indent=2)
    
    endpoint = f"{account_id}/campaigns"
    params = {
        "fields": "id,name,objective,status,daily_budget,lifetime_budget,buying_type,start_time,stop_time,created_time,updated_time,bid_strategy",
        "limit": limit
    }
    
    if status_filter:
        # API expects an array, encode it as a JSON string
        params["effective_status"] = json.dumps([status_filter])
    
    if after:
        params["after"] = after
    
    data = await make_api_request(endpoint, access_token, params)
    
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_campaign_details(access_token: str = None, campaign_id: str = None) -> str:
    """
    Get detailed information about a specific campaign.

    Note: This function requests a specific set of fields ('id,name,objective,status,...'). 
    The Meta API offers many other fields for campaigns (e.g., 'effective_status', 'source_campaign_id', etc.) 
    that could be added to the 'fields' parameter in the code if needed.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        campaign_id: Meta Ads campaign ID
    """
    if not campaign_id:
        return json.dumps({"error": "No campaign ID provided"}, indent=2)
    
    endpoint = f"{campaign_id}"
    params = {
        "fields": "id,name,objective,status,daily_budget,lifetime_budget,buying_type,start_time,stop_time,created_time,updated_time,bid_strategy,special_ad_categories,special_ad_category_country,budget_remaining,configured_status"
    }
    
    data = await make_api_request(endpoint, access_token, params)
    
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_campaign(
    access_token: str | None = None,
    account_id: str | None = None,                 # Expected format: "act_<ID>"
    name: str | None = None,
    objective: str | None = None,                  # Will be normalized to ODAX
    status: str = "PAUSED",                        # "ACTIVE" | "PAUSED"
    special_ad_categories: list[str] | None = None,
    special_ad_category_country: list[str] | None = None,  # required if IEP is present
    buying_type: str | None = None,                # "AUCTION" | "RESERVED"
    # Campaign-level budget (CBO). Leave both None when using ad set level budgets.
    daily_budget: str | None = None,               # integer string in minor units (e.g., cents)
    lifetime_budget: str | None = None,            # integer string in minor units
    # Campaign-level bid strategy (allowed only when CBO is used):
    bid_strategy: str | None = None,               # "LOWEST_COST" | "LOWEST_COST_WITH_BID_CAP" | "COST_CAP"
    spend_cap: str | None = None,                  # integer string in minor units
    use_adset_level_budgets: bool = False
) -> str:
    """
        Create a Meta Ads Campaign aligned with v23 Marketing API constraints.

        Key behavior:
          - Accepted objectives.
          - Budget can live either at Campaign level (CBO) or at Ad Set level (ABO).
            * If CBO (campaign budgets): you must provide exactly one of daily_budget OR lifetime_budget.
              Optionally, bid_strategy may be set at the Campaign level.
            * If ABO (ad set budgets): do NOT send campaign budgets and do NOT send bid_strategy here.
          - Special Ad Categories: array is required (can be []), and if it includes
            ISSUES_ELECTIONS_POLITICS, special_ad_category_country must be provided.

        Args:
            access_token: Meta API access token (if omitted, the implementation may use a cached token).
            account_id: Ad account ID in the form "act_<ID>".
            name: Campaign name.
            objective: Campaign objective (legacy values will be normalized to ODAX).
                       Allowed after normalization: OUTCOME_LEADS, OUTCOME_APP_PROMOTION, OUTCOME_SALES.
            status: Initial campaign status. Default is "PAUSED".
            special_ad_categories: List of special ad categories (e.g., [], ["CREDIT"], ["ISSUES_ELECTIONS_POLITICS"]).
            special_ad_category_country: Required when using ISSUES_ELECTIONS_POLITICS (e.g., ["BR"]).
            buying_type: Buying type, typically "AUCTION". Use "RESERVED" only if you know the constraints.
            daily_budget: Campaign daily budget (minor units) for CBO. Mutually exclusive with lifetime_budget.
            lifetime_budget: Campaign lifetime budget (minor units) for CBO. Mutually exclusive with daily_budget.
            bid_strategy: Campaign-level bid strategy — only valid when using CBO.
                          Recommended values for your use cases: "LOWEST_COST" (highest volume) or "COST_CAP".
                          Optionally "LOWEST_COST_WITH_BID_CAP" if you will set per-adset caps later.
            spend_cap: Optional campaign spend cap (minor units).
            use_adset_level_budgets: When True, budgets must be set on Ad Sets instead of the Campaign.

        Returns:
            JSON string with the Graph API response or an error payload with details and the parameters sent.

        Notes:
            - Dayparting/scheduling by hours is configured at the Ad Set level (requires lifetime budget there).
            - A/B testing (Experiments/Split test) is handled via separate endpoints, not in Campaign creation.
        """
    # Check required parameters
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_1234567890')"}, indent=2)
    if not name:
        return json.dumps({"error": "name is required"}, indent=2)
    if not objective:
        return json.dumps({"error": "objective is required (use ODAX OUTCOME_*)"}, indent=2)

    # Special_ad_categories is required by the API, set default if not provided
    if special_ad_categories is None:
        special_ad_categories = []
    # Special Ad Categories & IEP country requirement
    if "ISSUES_ELECTIONS_POLITICS" in special_ad_categories and not special_ad_category_country:
        return json.dumps(
            {"error": "special_ad_category_country is required when using ISSUES_ELECTIONS_POLITICS"},
            indent=2
        )

    # Budgeting rules (CBO vs ABO) and bid strategy gating
    if use_adset_level_budgets:
        # ABO: budgets must not be set on the Campaign; bid_strategy must be applied at Ad Set level.
        if daily_budget or lifetime_budget:
            return json.dumps(
                {"error": "Do not set campaign budgets when use_adset_level_budgets=True"},
                indent=2
            )
        if bid_strategy:
            return json.dumps(
                {"error": "With ad set level budgets, set bid_strategy at the Ad Set level"},
                indent=2
            )
    else:
        # CBO: exactly one budget must be provided at Campaign level.
        if (daily_budget is None) == (lifetime_budget is None):
            return json.dumps(
                {"error": "Provide exactly one: daily_budget OR lifetime_budget for campaign-level budget (CBO)"},
                indent=2
            )
        if bid_strategy:
            allowed = {"LOWEST_COST", "LOWEST_COST_WITHOUT_CAP", "LOWEST_COST_WITH_BID_CAP", "COST_CAP"}
            if bid_strategy not in allowed:
                return json.dumps(
                    {"error": f"Invalid bid_strategy '{bid_strategy}'. Allowed: {sorted(allowed)}"},
                    indent=2
                )

    if buying_type and buying_type not in ("AUCTION", "RESERVED"):
        return json.dumps({"error": "buying_type must be AUCTION or RESERVED"}, indent=2)

    # Build Graph API request
    endpoint = f"{account_id}/campaigns"
    params = {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": json.dumps(special_ad_categories)  # Properly format as JSON string
    }
    if special_ad_category_country:
        params["special_ad_category_country"] = json.dumps(special_ad_category_country)
    if buying_type:
        params["buying_type"] = buying_type
    if spend_cap is not None:
        params["spend_cap"] = str(spend_cap)

    # Add campaign-level budget fields only when using CBO
    if not use_adset_level_budgets:
        if daily_budget is not None:
            params["daily_budget"] = str(daily_budget)
        if lifetime_budget is not None:
            params["lifetime_budget"] = str(lifetime_budget)
        if bid_strategy:
            params["bid_strategy"] = bid_strategy  # Valid at Campaign level only when using CBO

    # Execute request
    try:
        data = await make_api_request(endpoint, access_token, params, method="POST")
        
        # Annotate budget scope for convenience
        data["budget_scope"] = "adset" if use_adset_level_budgets else "campaign"
        return json.dumps(data, indent=2)

    except Exception as e:
        error_msg = str(e)
        return json.dumps({
            "error": "Failed to create campaign",
            "details": error_msg,
            "params_sent": params
        }, indent=2)


@mcp_server.tool()
@meta_api_tool
async def update_campaign(
    access_token: str = None,
    campaign_id: str = None,
    name: str = None,
    status: str = None,
    special_ad_categories: List[str] = None,
    daily_budget = None,
    lifetime_budget = None,
    bid_strategy: str = None,
    bid_cap = None,
    spend_cap = None,
    campaign_budget_optimization: bool = None,
    objective: str = None,  # Add objective if it's updatable
    use_adset_level_budgets: bool = None,  # Add other updatable fields as needed based on API docs
) -> str:
    """
    Update an existing campaign in a Meta Ads account.

    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        campaign_id: Meta Ads campaign ID (required)
        name: New campaign name
        status: New campaign status (e.g., 'ACTIVE', 'PAUSED')
        special_ad_categories: List of special ad categories if applicable
        daily_budget: New daily budget in account currency (in cents) as a string. 
                     Set to empty string "" to remove the daily budget.
        lifetime_budget: New lifetime budget in account currency (in cents) as a string.
                        Set to empty string "" to remove the lifetime budget.
        bid_strategy: New bid strategy
        bid_cap: New bid cap in account currency (in cents) as a string
        spend_cap: New spending limit for the campaign in account currency (in cents) as a string
        campaign_budget_optimization: Enable/disable campaign budget optimization
        objective: New campaign objective (Note: May not always be updatable)
        use_adset_level_budgets: If True, removes campaign-level budgets to switch to ad set level budgets
    """
    if not campaign_id:
        return json.dumps({"error": "No campaign ID provided"}, indent=2)

    endpoint = f"{campaign_id}"
    
    params = {}
    
    # Add parameters to the request only if they are provided
    if name is not None:
        params["name"] = name
    if status is not None:
        params["status"] = status
    if special_ad_categories is not None:
        # Note: Updating special_ad_categories might have specific API rules or might not be allowed after creation.
        # The API might require an empty list `[]` to clear categories. Check Meta Docs.
        params["special_ad_categories"] = json.dumps(special_ad_categories)
    
    # Handle budget parameters based on use_adset_level_budgets setting
    if use_adset_level_budgets is not None:
        if use_adset_level_budgets:
            # Remove campaign-level budgets when switching to ad set level budgets
            params["daily_budget"] = ""
            params["lifetime_budget"] = ""
            if campaign_budget_optimization is not None:
                params["campaign_budget_optimization"] = "false"
            if bid_strategy is not None:
                return json.dumps(
                    {"error": "When switching to ad set level budgets (ABO), do not set campaign-level bid_strategy."},
                    indent=2
                )
        else:
            # If switching back to campaign-level budgets, use the provided budget values
            if daily_budget is not None:
                if daily_budget == "":
                    params["daily_budget"] = ""
                else:
                    params["daily_budget"] = str(daily_budget)
            if lifetime_budget is not None:
                if lifetime_budget == "":
                    params["lifetime_budget"] = ""
                else:
                    params["lifetime_budget"] = str(lifetime_budget)
            if campaign_budget_optimization is not None:
                params["campaign_budget_optimization"] = "true" if campaign_budget_optimization else "false"
            if daily_budget is not None and lifetime_budget is not None and daily_budget != "" and lifetime_budget != "":
                return json.dumps(
                    {"error": "For CBO, provide exactly one: daily_budget OR lifetime_budget."},
                    indent=2
                )
    else:
        # Normal budget updates when not changing budget strategy
        if daily_budget is not None:
            # To remove budget, set to empty string
            if daily_budget == "":
                params["daily_budget"] = ""
            else:
                params["daily_budget"] = str(daily_budget)
        if lifetime_budget is not None:
            # To remove budget, set to empty string
            if lifetime_budget == "":
                params["lifetime_budget"] = ""
            else:
                params["lifetime_budget"] = str(lifetime_budget)
        if campaign_budget_optimization is not None:
            params["campaign_budget_optimization"] = "true" if campaign_budget_optimization else "false"
    
    if bid_strategy is not None:
        params["bid_strategy"] = bid_strategy
    if bid_cap is not None:
        params["bid_cap"] = str(bid_cap)
    if spend_cap is not None:
        params["spend_cap"] = str(spend_cap)
    if objective is not None:
        params["objective"] = objective # Caution: Objective changes might reset learning or be restricted

    if not params:
        return json.dumps({"error": "No update parameters provided"}, indent=2)

    try:
        # Use POST method for updates as per Meta API documentation
        data = await make_api_request(endpoint, access_token, params, method="POST")
        
        # Add a note about budget strategy if switching to ad set level budgets
        if use_adset_level_budgets is not None and use_adset_level_budgets:
            data["budget_strategy"] = "ad_set_level"
            data["note"] = "Campaign updated to use ad set level budgets. Set budgets when creating ad sets within this campaign."
        
        return json.dumps(data, indent=2)
    except Exception as e:
        error_msg = str(e)
        # Include campaign_id in error for better context
        return json.dumps({
            "error": f"Failed to update campaign {campaign_id}",
            "details": error_msg,
            "params_sent": params # Be careful about logging sensitive data if any
        }, indent=2)
