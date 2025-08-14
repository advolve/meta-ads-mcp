"""Ad and Creative-related functionality for Meta Ads API."""

import json
from typing import Optional, Dict, Any, List
import io
from PIL import Image as PILImage
from mcp.server.fastmcp import Image
import os
import time

from .api import meta_api_tool, make_api_request
from .accounts import get_ad_accounts
from .utils import download_image, try_multiple_download_methods, ad_creative_images, extract_creative_image_urls
from .server import mcp_server


@mcp_server.tool()
@meta_api_tool
async def get_ads(access_token: str = None, account_id: str = None, limit: int = 10, 
                 campaign_id: str = "", adset_id: str = "") -> str:
    """
    Get ads for a Meta Ads account with optional filtering.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
        limit: Maximum number of ads to return (default: 10)
        campaign_id: Optional campaign ID to filter by
        adset_id: Optional ad set ID to filter by
    """
    # If no account ID is specified, try to get the first one for the user
    if not account_id:
        accounts_json = await get_ad_accounts("me", json.dumps({"limit": 1}), access_token)
        accounts_data = json.loads(accounts_json)
        
        if "data" in accounts_data and accounts_data["data"]:
            account_id = accounts_data["data"][0]["id"]
        else:
            return json.dumps({"error": "No account ID specified and no accounts found for user"}, indent=2)
    
    # Prioritize adset_id over campaign_id - use adset-specific endpoint
    if adset_id:
        endpoint = f"{adset_id}/ads"
        params = {
            "fields": "id,name,adset_id,campaign_id,status,creative,created_time,updated_time,bid_amount,conversion_domain,tracking_specs",
            "limit": limit
        }
    # Use campaign-specific endpoint if campaign_id is provided
    elif campaign_id:
        endpoint = f"{campaign_id}/ads"
        params = {
            "fields": "id,name,adset_id,campaign_id,status,creative,created_time,updated_time,bid_amount,conversion_domain,tracking_specs",
            "limit": limit
        }
    else:
        # Default to account-level endpoint if no specific filters
        endpoint = f"{account_id}/ads"
        params = {
            "fields": "id,name,adset_id,campaign_id,status,creative,created_time,updated_time,bid_amount,conversion_domain,tracking_specs",
            "limit": limit
        }

    data = await make_api_request(endpoint, access_token, params)
    
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_ad_details(access_token: str = None, ad_id: str = None) -> str:
    """
    Get detailed information about a specific ad.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        ad_id: Meta Ads ad ID
    """
    if not ad_id:
        return json.dumps({"error": "No ad ID provided"}, indent=2)
        
    endpoint = f"{ad_id}"
    params = {
        "fields": "id,name,adset_id,campaign_id,status,creative,created_time,updated_time,bid_amount,conversion_domain,tracking_specs,preview_shareable_link"
    }
    
    data = await make_api_request(endpoint, access_token, params)
    
    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_ad(
    account_id: str | None = None,
    name: str | None = None,
    adset_id: str | None = None,
    status: str = "PAUSED",

    creative_id: str | None = None,  # If provided, we will use it as-is
    # Inline creative builder (ignored if creative_id is set)
    page_id: str | None = None,                      # Facebook Page identity
    instagram_actor_id: str | None = None,           # IG identity
    format: str | None = None,                       # "SINGLE_IMAGE" | "SINGLE_VIDEO" | "CAROUSEL" | "EXISTING_POST"
    message: str | None = None,                      # Primary text
    headline: str | None = None,                     # Headline (where applicable)
    description: str | None = None,                  # Description (where applicable)
    link_url: str | None = None,                     # Destination URL for link ads
    call_to_action_type: str | None = None,          # e.g., "LEARN_MORE", "SHOP_NOW", "SIGN_UP"
    template_url_spec: dict | None = None,           # UTM builder / URL params (API: template_url_spec)
    image_hash: str | None = None,                   # From /adimages
    video_id: str | None = None,                     # From /advideos
    carousel_attachments: list[dict] | None = None,  # [{link, image_hash|video_id, name, description}, ...] (>=2)
    object_story_id: str | None = None,              # Boost an existing Page/IG post
    # Deep links to app (API: link_data.app_link_spec)
    app_link_spec: dict | None = None,               # {"ios":[{app_store_id, url, app_name}], "android":[{package, url, app_name}]}
    instant_experience_id: str | None = None,        # Attach Instant Experience (Canvas) (API: post_click_configuration.instant_experience_id)
    partnership_ad_code: str | None = None,          # IG Partnership Ad code (adcode-XXXX) – see notes
    branded_content_sponsor_page_id: str | None = None,  # Facebook branded content sponsor (non-code flow)
    sponsor_ig_user_id: str | None = None,               # IG sponsor user (non-code flow)
    multi_advertiser_enroll_status: str = "OPT_OUT",     # "OPT_OUT" (default) or "OPT_IN"
    standard_enhancements_enroll_status: str | None = "OPT_OUT",  # avoid “enroll_status not provided” errors
    bid_amount: int | None = None,                       # Not recommended unless you must override
    tracking_specs: list[dict] | None = None,            # Legacy; pass-through only
    creative_overrides: dict | None = None,              # Any raw creative fields to merge last
    degrees_of_freedom_spec: dict | None = None,         # Pass-through in case API requires it
    access_token: str | None = None
) -> str:
    """
    Create an Ad under an Ad Set, supporting identity, partnership (ad code),
    single image / video / carousel formats, deep links, Instant Experience,
    UTM tagging, and explicit opt-in/out to Multi-advertiser Ads.

    IMPORTANT:
    - If `creative_id` is provided we will use that creative as-is and ignore inline builder params.
    - If building inline, you should set `page_id` (FB) and/or `instagram_actor_id` (IG) to define identity.
    - For CAROUSEL provide `carousel_attachments` with at least 2 items.
    - For deep links use `app_link_spec`. For Instant Experience provide `instant_experience_id`.
    - `multi_advertiser_enroll_status` defaults to OPT_OUT to satisfy “default off”; override to OPT_IN if desired.
    - `tracking_specs` is legacy; in most cases rely on pixel/app/offline events configured at Ad Set / account level.

    Returns:
        JSON string with API response or structured error.
    """
    # Check required parameters
    if not account_id:
        return json.dumps({"error": "No account ID provided"}, indent=2)
    if not name:
        return json.dumps({"error": "No ad name provided"}, indent=2)
    if not adset_id:
        return json.dumps({"error": "No ad set ID provided"}, indent=2)
    if creative_id and (format or page_id or instagram_actor_id or image_hash or video_id or carousel_attachments or object_story_id):
        return json.dumps({"error": "Provide either creative_id OR inline creative params, not both."}, indent=2)
    
    creative: dict = {}
    if creative_id:
        creative = {"creative_id": creative_id}
    else:
        # Inline creative requires identity for delivery on most placements.
        if not (page_id or instagram_actor_id):
            return json.dumps({
                "error": "Missing identity for inline creative",
                "details": "Provide page_id (Facebook) and/or instagram_actor_id (Instagram)."
            }, indent=2)

        # Normalize format
        if format not in ( "SINGLE_IMAGE", "SINGLE_VIDEO", "CAROUSEL", "EXISTING_POST" ):
            return json.dumps({
                "error": "Invalid or missing 'format'",
                "valid_values": ["SINGLE_IMAGE", "SINGLE_VIDEO", "CAROUSEL", "EXISTING_POST"]
            }, indent=2)

        object_story_spec: dict = {}
        if page_id: object_story_spec["page_id"] = page_id
        if instagram_actor_id: object_story_spec["instagram_actor_id"] = instagram_actor_id

        # EXISTING_POST / Boost
        if format == "EXISTING_POST":
            if not object_story_id:
                return json.dumps({"error": "object_story_id is required for format=EXISTING_POST"}, indent=2)
            creative["object_story_id"] = object_story_id

        # SINGLE_IMAGE or CAROUSEL – use link_data
        if format in ("SINGLE_IMAGE", "CAROUSEL"):
            if not link_url:
                return json.dumps({"error": "link_url is required for image/carousel link ads"}, indent=2)
            link_data: dict = {"link": link_url}
            if message: link_data["message"] = message
            if headline: link_data["name"] = headline
            if description: link_data["description"] = description

            if call_to_action_type:
                link_data["call_to_action"] = {
                    "type": call_to_action_type,
                    "value": {"link": link_url}
                }

            if template_url_spec:
                # UTM / URL params: pass-through as provided
                link_data["template_url_spec"] = template_url_spec

            if app_link_spec:
                # Deep links into the app (iOS/Android)
                link_data["app_link_spec"] = app_link_spec

            # Single image needs an image
            if format == "SINGLE_IMAGE":
                if not image_hash:
                    return json.dumps({"error": "image_hash is required for SINGLE_IMAGE"}, indent=2)
                link_data["image_hash"] = image_hash

            # Carousel: 2+ child attachments with link + media
            if format == "CAROUSEL":
                if not carousel_attachments or len(carousel_attachments) < 2:
                    return json.dumps({"error": "carousel_attachments must contain at least 2 items"}, indent=2)
                children = []
                for idx, att in enumerate(carousel_attachments, start=1):
                    if "link" not in att:
                        return json.dumps({"error": f"carousel_attachments[{idx}] missing 'link'"}, indent=2)
                    if not any(k in att for k in ("image_hash", "video_id")):
                        return json.dumps({"error": f"carousel_attachments[{idx}] needs 'image_hash' or 'video_id'"}, indent=2)
                    child = {"link": att["link"]}
                    if "image_hash" in att: child["image_hash"] = att["image_hash"]
                    if "video_id" in att: child["video_id"] = att["video_id"]
                    if "name" in att: child["name"] = att["name"]
                    if "description" in att: child["description"] = att["description"]
                    if call_to_action_type:
                        child["call_to_action"] = {"type": call_to_action_type, "value": {"link": att["link"]}}
                    children.append(child)
                link_data["child_attachments"] = children

            object_story_spec["link_data"] = link_data

        # SINGLE_VIDEO – use video_data
        if format == "SINGLE_VIDEO":
            if not video_id:
                return json.dumps({"error": "video_id is required for SINGLE_VIDEO"}, indent=2)
            video_data: dict = {"video_id": video_id}
            if message: video_data["message"] = message
            if call_to_action_type and link_url:
                video_data["call_to_action"] = {"type": call_to_action_type, "value": {"link": link_url}}
            if link_url:
                # Some video link ads still use destination link via CTA value.link; keep link_url for clarity
                video_data["link_description"] = description or ""
            object_story_spec["video_data"] = video_data

        # Instant Experience (Canvas) is attached at creative via post_click_configuration
        post_click_configuration = None
        if instant_experience_id:
            post_click_configuration = {"instant_experience_id": instant_experience_id}

        # Partnership / Branded Content:
        # NOTE: Partner 'ad code' (IG Partnership Ads) must be attached in the creative.
        # Depending on the surface, this lives under sponsorship/branded-content specs.
        sponsorship_blocks = {}
        if partnership_ad_code:
            # The API expects the ad code in the partnership/branded content spec.
            # We forward it in a sponsorship spec; the exact key is handled by the API layer.
            sponsorship_blocks["partnership_ad_code"] = partnership_ad_code  # forwarded (API layer should map)
        if branded_content_sponsor_page_id:
            sponsorship_blocks["branded_content_sponsor_page_id"] = branded_content_sponsor_page_id
        if sponsor_ig_user_id:
            sponsorship_blocks["sponsor_ig_user_id"] = sponsor_ig_user_id

        # Build final creative from object_story_spec + optional sponsorship and post_click config
        creative = {"object_story_spec": object_story_spec}
        if sponsorship_blocks:
            # Prefer link_data.sponsorship_info_spec when using link_data;
            # fall back to top-level branded content spec as needed by API version.
            if "link_data" in object_story_spec:
                creative.setdefault("object_story_spec", {}) \
                        .setdefault("link_data", {})["sponsorship_info_spec"] = sponsorship_blocks
            else:
                # For video_data/existing post, use branded content creative fields.
                creative.update({"branded_content": sponsorship_blocks})

        if post_click_configuration:
            creative["post_click_configuration"] = post_click_configuration

        # URL/UTM template at creative level (if caller prefers)
        if template_url_spec and "object_story_spec" in creative:
            creative["template_url_spec"] = template_url_spec

        # Creative features: set Multi-advertiser enroll_status explicitly (default OPT_OUT)
        features = {"contextual_multi_ads": {"enroll_status": multi_advertiser_enroll_status}}
        if standard_enhancements_enroll_status:
            features["standard_enhancements"] = {"enroll_status": standard_enhancements_enroll_status}
        creative["creative_features_spec"] = features  # Modern field family

        # Escape hatch: merge raw creative overrides last
        if creative_overrides:
            # Shallow merge only; callers can pass full subtrees if needed.
            creative.update(creative_overrides)

    endpoint = f"{account_id}/ads"
    
    params = {
        "name": name,
        "adset_id": adset_id,
        "status": status,
        "creative": json.dumps(creative)
    }
    
    # Add bid amount if provided
    if bid_amount is not None:
        params["bid_amount"] = str(bid_amount)
        
    # Add tracking specs if provided
    if tracking_specs is not None:
        params["tracking_specs"] = json.dumps(tracking_specs) # Needs to be JSON encoded string

    # Pass-through global degrees_of_freedom_spec if the account requires it
    if degrees_of_freedom_spec:
        params["degrees_of_freedom_spec"] = json.dumps(degrees_of_freedom_spec)
    
    try:
        data = await make_api_request(endpoint, access_token, params, method="POST")
        return json.dumps(data, indent=2)
    except Exception as e:
        error_msg = str(e)
        # Helpful error special-cases
        if "enroll_status" in error_msg.lower():
            return json.dumps({
                "error": "Creative features require explicit enroll_status (OPT_IN/OPT_OUT).",
                "details": error_msg,
                "hint": "Try setting standard_enhancements_enroll_status='OPT_OUT' and/or multi_advertiser_enroll_status='OPT_OUT'.",
                "params_sent": params
            }, indent=2)
        return json.dumps({
            "error": "Failed to create ad",
            "details": error_msg,
            "params_sent": params
        }, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_ad_creatives(access_token: str = None, ad_id: str = None) -> str:
    """
    Get creative details for a specific ad. Best if combined with get_ad_image to get the full image.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        ad_id: Meta Ads ad ID
    """
    if not ad_id:
        return json.dumps({"error": "No ad ID provided"}, indent=2)
        
    endpoint = f"{ad_id}/adcreatives"
    params = {
        "fields": "id,name,status,thumbnail_url,image_url,image_hash,object_story_spec,asset_feed_spec,image_urls_for_viewing"
    }
    
    data = await make_api_request(endpoint, access_token, params)
    
    # Add image URLs for direct viewing if available
    if 'data' in data:
        for creative in data['data']:
            creative['image_urls_for_viewing'] = extract_creative_image_urls(creative)

    return json.dumps(data, indent=2)


@mcp_server.tool()
@meta_api_tool
async def get_ad_image(access_token: str = None, ad_id: str = None) -> Image:
    """
    Get, download, and visualize a Meta ad image in one step. Useful to see the image in the LLM.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        ad_id: Meta Ads ad ID
    
    Returns:
        The ad image ready for direct visual analysis
    """
    if not ad_id:
        return "Error: No ad ID provided"
        
    print(f"Attempting to get and analyze creative image for ad {ad_id}")
    
    # First, get creative and account IDs
    ad_endpoint = f"{ad_id}"
    ad_params = {
        "fields": "creative{id},account_id"
    }
    
    ad_data = await make_api_request(ad_endpoint, access_token, ad_params)
    
    if "error" in ad_data:
        return f"Error: Could not get ad data - {json.dumps(ad_data)}"
    
    # Extract account_id
    account_id = ad_data.get("account_id", "")
    if not account_id:
        return "Error: No account ID found"
    
    # Extract creative ID
    if "creative" not in ad_data:
        return "Error: No creative found for this ad"
        
    creative_data = ad_data.get("creative", {})
    creative_id = creative_data.get("id")
    if not creative_id:
        return "Error: No creative ID found"
    
    # Get creative details to find image hash
    creative_endpoint = f"{creative_id}"
    creative_params = {
        "fields": "id,name,image_hash,asset_feed_spec"
    }
    
    creative_details = await make_api_request(creative_endpoint, access_token, creative_params)
    
    # Identify image hashes to use from creative
    image_hashes = []
    
    # Check for direct image_hash on creative
    if "image_hash" in creative_details:
        image_hashes.append(creative_details["image_hash"])
    
    # Check asset_feed_spec for image hashes - common in Advantage+ ads
    if "asset_feed_spec" in creative_details and "images" in creative_details["asset_feed_spec"]:
        for image in creative_details["asset_feed_spec"]["images"]:
            if "hash" in image:
                image_hashes.append(image["hash"])
    
    if not image_hashes:
        # If no hashes found, try to extract from the first creative we found in the API
        # and also check for direct URLs as fallback
        creative_json = await get_ad_creatives(access_token=access_token, ad_id=ad_id)
        creative_data = json.loads(creative_json)
        
        # Try to extract hash from data array
        if "data" in creative_data and creative_data["data"]:
            for creative in creative_data["data"]:
                # Check object_story_spec for image hash
                if "object_story_spec" in creative and "link_data" in creative["object_story_spec"]:
                    link_data = creative["object_story_spec"]["link_data"]
                    if "image_hash" in link_data:
                        image_hashes.append(link_data["image_hash"])
                # Check direct image_hash on creative
                elif "image_hash" in creative:
                    image_hashes.append(creative["image_hash"])
                # Check asset_feed_spec for image hashes
                elif "asset_feed_spec" in creative and "images" in creative["asset_feed_spec"]:
                    images = creative["asset_feed_spec"]["images"]
                    if images and len(images) > 0 and "hash" in images[0]:
                        image_hashes.append(images[0]["hash"])
        
        # If still no image hashes found, try direct URL fallback approach
        if not image_hashes:
            print("No image hashes found, trying direct URL fallback...")
            
            image_url = None
            if "data" in creative_data and creative_data["data"]:
                creative = creative_data["data"][0]
                
                # Prioritize higher quality image URLs in this order:
                # 1. image_urls_for_viewing (usually highest quality)
                # 2. image_url (direct field)
                # 3. object_story_spec.link_data.picture (usually full size)
                # 4. thumbnail_url (last resort - often profile thumbnail)
                
                if "image_urls_for_viewing" in creative and creative["image_urls_for_viewing"]:
                    image_url = creative["image_urls_for_viewing"][0]
                    print(f"Using image_urls_for_viewing: {image_url}")
                elif "image_url" in creative and creative["image_url"]:
                    image_url = creative["image_url"]
                    print(f"Using image_url: {image_url}")
                elif "object_story_spec" in creative and "link_data" in creative["object_story_spec"]:
                    link_data = creative["object_story_spec"]["link_data"]
                    if "picture" in link_data and link_data["picture"]:
                        image_url = link_data["picture"]
                        print(f"Using object_story_spec.link_data.picture: {image_url}")
                elif "thumbnail_url" in creative and creative["thumbnail_url"]:
                    image_url = creative["thumbnail_url"]
                    print(f"Using thumbnail_url (fallback): {image_url}")
            
            if not image_url:
                return "Error: No image URLs found in creative"
            
            # Download the image directly
            print(f"Downloading image from direct URL: {image_url}")
            image_bytes = await download_image(image_url)
            
            if not image_bytes:
                return "Error: Failed to download image from direct URL"
            
            try:
                # Convert bytes to PIL Image
                img = PILImage.open(io.BytesIO(image_bytes))
                
                # Convert to RGB if needed
                if img.mode != "RGB":
                    img = img.convert("RGB")
                    
                # Create a byte stream of the image data
                byte_arr = io.BytesIO()
                img.save(byte_arr, format="JPEG")
                img_bytes = byte_arr.getvalue()
                
                # Return as an Image object that LLM can directly analyze
                return Image(data=img_bytes, format="jpeg")
                
            except Exception as e:
                return f"Error processing image from direct URL: {str(e)}"
    
    print(f"Found image hashes: {image_hashes}")
    
    # Now fetch image data using adimages endpoint with specific format
    image_endpoint = f"act_{account_id}/adimages"
    
    # Format the hashes parameter exactly as in our successful curl test
    hashes_str = f'["{image_hashes[0]}"]'  # Format first hash only, as JSON string array
    
    image_params = {
        "fields": "hash,url,width,height,name,status",
        "hashes": hashes_str
    }
    
    print(f"Requesting image data with params: {image_params}")
    image_data = await make_api_request(image_endpoint, access_token, image_params)
    
    if "error" in image_data:
        return f"Error: Failed to get image data - {json.dumps(image_data)}"
    
    if "data" not in image_data or not image_data["data"]:
        return "Error: No image data returned from API"
    
    # Get the first image URL
    first_image = image_data["data"][0]
    image_url = first_image.get("url")
    
    if not image_url:
        return "Error: No valid image URL found"
    
    print(f"Downloading image from URL: {image_url}")
    
    # Download the image
    image_bytes = await download_image(image_url)
    
    if not image_bytes:
        return "Error: Failed to download image"
    
    try:
        # Convert bytes to PIL Image
        img = PILImage.open(io.BytesIO(image_bytes))
        
        # Convert to RGB if needed
        if img.mode != "RGB":
            img = img.convert("RGB")
            
        # Create a byte stream of the image data
        byte_arr = io.BytesIO()
        img.save(byte_arr, format="JPEG")
        img_bytes = byte_arr.getvalue()
        
        # Return as an Image object that LLM can directly analyze
        return Image(data=img_bytes, format="jpeg")
        
    except Exception as e:
        return f"Error processing image: {str(e)}"


@mcp_server.tool()
@meta_api_tool
async def save_ad_image_locally(access_token: str = None, ad_id: str = None, output_dir: str = "ad_images") -> str:
    """
    Get, download, and save a Meta ad image locally, returning the file path.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        ad_id: Meta Ads ad ID
        output_dir: Directory to save the image file (default: 'ad_images')
    
    Returns:
        The file path to the saved image, or an error message string.
    """
    if not ad_id:
        return json.dumps({"error": "No ad ID provided"}, indent=2)
        
    print(f"Attempting to get and save creative image for ad {ad_id}")
    
    # First, get creative and account IDs
    ad_endpoint = f"{ad_id}"
    ad_params = {
        "fields": "creative{id},account_id"
    }
    
    ad_data = await make_api_request(ad_endpoint, access_token, ad_params)
    
    if "error" in ad_data:
        return json.dumps({"error": f"Could not get ad data - {json.dumps(ad_data)}"}, indent=2)
    
    account_id = ad_data.get("account_id")
    if not account_id:
        return json.dumps({"error": "No account ID found for ad"}, indent=2)
    
    if "creative" not in ad_data:
        return json.dumps({"error": "No creative found for this ad"}, indent=2)
        
    creative_data = ad_data.get("creative", {})
    creative_id = creative_data.get("id")
    if not creative_id:
        return json.dumps({"error": "No creative ID found"}, indent=2)
    
    # Get creative details to find image hash
    creative_endpoint = f"{creative_id}"
    creative_params = {
        "fields": "id,name,image_hash,asset_feed_spec"
    }
    creative_details = await make_api_request(creative_endpoint, access_token, creative_params)
    
    image_hashes = []
    if "image_hash" in creative_details:
        image_hashes.append(creative_details["image_hash"])
    if "asset_feed_spec" in creative_details and "images" in creative_details["asset_feed_spec"]:
        for image in creative_details["asset_feed_spec"]["images"]:
            if "hash" in image:
                image_hashes.append(image["hash"])
    
    if not image_hashes:
        # Fallback attempt (as in get_ad_image)
        creative_json = await get_ad_creatives(ad_id=ad_id, access_token=access_token) # Ensure ad_id is passed correctly
        creative_data_list = json.loads(creative_json)
        if 'data' in creative_data_list and creative_data_list['data']:
             first_creative = creative_data_list['data'][0]
             if 'object_story_spec' in first_creative and 'link_data' in first_creative['object_story_spec'] and 'image_hash' in first_creative['object_story_spec']['link_data']:
                 image_hashes.append(first_creative['object_story_spec']['link_data']['image_hash'])
             elif 'image_hash' in first_creative: # Check direct hash on creative data
                  image_hashes.append(first_creative['image_hash'])


    if not image_hashes:
        return json.dumps({"error": "No image hashes found in creative or fallback"}, indent=2)

    print(f"Found image hashes: {image_hashes}")
    
    # Fetch image data using the first hash
    image_endpoint = f"act_{account_id}/adimages"
    hashes_str = f'["{image_hashes[0]}"]'
    image_params = {
        "fields": "hash,url,width,height,name,status",
        "hashes": hashes_str
    }
    
    print(f"Requesting image data with params: {image_params}")
    image_data = await make_api_request(image_endpoint, access_token, image_params)
    
    if "error" in image_data:
        return json.dumps({"error": f"Failed to get image data - {json.dumps(image_data)}"}, indent=2)
    
    if "data" not in image_data or not image_data["data"]:
        return json.dumps({"error": "No image data returned from API"}, indent=2)
        
    first_image = image_data["data"][0]
    image_url = first_image.get("url")
    
    if not image_url:
        return json.dumps({"error": "No valid image URL found in API response"}, indent=2)
        
    print(f"Downloading image from URL: {image_url}")
    
    # Download and Save Image
    image_bytes = await download_image(image_url)
    
    if not image_bytes:
        return json.dumps({"error": "Failed to download image"}, indent=2)
        
    try:
        # Ensure output directory exists
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        # Create a filename (e.g., using ad_id and image hash)
        file_extension = ".jpg" # Default extension, could try to infer from headers later
        filename = f"{ad_id}_{image_hashes[0]}{file_extension}"
        filepath = os.path.join(output_dir, filename)
        
        # Save the image bytes to the file
        with open(filepath, "wb") as f:
            f.write(image_bytes)
            
        print(f"Image saved successfully to: {filepath}")
        return json.dumps({"filepath": filepath}, indent=2) # Return JSON with filepath

    except Exception as e:
        return json.dumps({"error": f"Failed to save image: {str(e)}"}, indent=2)


@mcp_server.tool()
@meta_api_tool
async def update_ad(
    ad_id: str,
    status: str = None,
    bid_amount: int = None,
    tracking_specs = None,
    creative_id: str = None,
    access_token: str = None
) -> str:
    """
    Update an ad with new settings.
    
    Args:
        ad_id: Meta Ads ad ID
        status: Update ad status (ACTIVE, PAUSED, etc.)
        bid_amount: Bid amount in account currency (in cents for USD)
        tracking_specs: Optional tracking specifications (e.g., for pixel events).
        creative_id: ID of the creative to associate with this ad (changes the ad's image/content)
        access_token: Meta API access token (optional - will use cached token if not provided)
    """
    if not ad_id:
        return json.dumps({"error": "Ad ID is required"}, indent=2)

    params = {}
    if status:
        params["status"] = status
    if bid_amount is not None:
        # Ensure bid_amount is sent as a string if it's not null
        params["bid_amount"] = str(bid_amount)
    if tracking_specs is not None: # Add tracking_specs to params if provided
        params["tracking_specs"] = json.dumps(tracking_specs) # Needs to be JSON encoded string
    if creative_id is not None:
        # Creative parameter needs to be a JSON object containing creative_id
        params["creative"] = json.dumps({"creative_id": creative_id})

    if not params:
        return json.dumps({"error": "No update parameters provided (status, bid_amount, tracking_specs, or creative_id)"}, indent=2)

    endpoint = f"{ad_id}"
    try:
        data = await make_api_request(endpoint, access_token, params, method='POST')
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Failed to update ad: {str(e)}"}, indent=2)


@mcp_server.tool()
@meta_api_tool
async def upload_ad_image(
        access_token: str = None,
        account_id: str = None,
        image_path: str = None,
        name: str = None,
        ad_image_crops: Optional[Dict[str, List[List[int]]]] = None,
) -> str:
    """
    Upload an image to use in Meta Ads creatives.

    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
        image_path: Path to the image file to upload
        name: Optional name for the image (default: filename)
        ad_image_crops (dict, optional): Optional crop map in the format:
            {
                "100x100": [[0, 0, 100, 100]],
                "191x100": [[12, 0, 204, 100]]
            }

    Returns:
        JSON response with image details including hash for creative creation
    """
    # Check required parameters
    if not account_id:
        return json.dumps({"error": "No account ID provided"}, indent=2)

    if not image_path:
        return json.dumps({"error": "No image path provided"}, indent=2)

    # Ensure account_id has the 'act_' prefix for API compatibility
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"

    # Check if image file exists
    if not os.path.exists(image_path):
        return json.dumps({"error": f"Image file not found: {image_path}"}, indent=2)

    try:
        # Read image file
        with open(image_path, "rb") as img_file:
            image_bytes = img_file.read()

        # Get image filename if name not provided
        if not name:
            name = os.path.basename(image_path)

        # Prepare the API endpoint for uploading images
        endpoint = f"{account_id}/adimages"

        # We need to convert the binary data to base64 for API upload
        import base64
        encoded_image = base64.b64encode(image_bytes).decode('utf-8')

        # Prepare POST parameters
        params = {
            "bytes": encoded_image,
            "name": name
        }

        if ad_image_crops:
            if not isinstance(ad_image_crops, dict):
                return json.dumps({"error": "Invalid format: ad_image_crops must be a dictionary"}, indent=2)
            try:
                for key, val in ad_image_crops.items():
                    if not isinstance(key, str) or not isinstance(val, list):
                        raise ValueError
                params["ad_image_crops"] = json.dumps(ad_image_crops)
            except (TypeError, ValueError):
                return json.dumps({"error": "Invalid ad_image_crops structure"}, indent=2)

        # Make API request to upload the image
        print(f"Uploading image to Facebook Ad Account {account_id}")
        data = await make_api_request(endpoint, access_token, params, method="POST")

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps({
            "error": "Failed to upload image",
            "details": str(e)
        }, indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_ad_creative_basic(
    access_token: str = None,
    account_id: str = None,
    name: str = None,
    image_hash: str = None,
    page_id: str = None,
    link_url: str = None,
    message: str = None,
    headline: str = None,
    headlines: List[str] = None,
    description: str = None,
    descriptions: List[str] = None,
    dynamic_creative_spec: Dict[str, Any] = None,
    call_to_action_type: str = None,
    instagram_actor_id: str = None
) -> str:
    """
    Create a new ad creative using an uploaded image hash.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
        name: Creative name
        image_hash: Hash of the uploaded image
        page_id: Facebook Page ID to be used for the ad
        link_url: Destination URL for the ad
        message: Ad copy/text
        headline: Single headline for simple ads (cannot be used with headlines)
        headlines: List of headlines for dynamic creative testing (cannot be used with headline)
        description: Single description for simple ads (cannot be used with descriptions)
        descriptions: List of descriptions for dynamic creative testing (cannot be used with description)
        dynamic_creative_spec: Dynamic creative optimization settings
        call_to_action_type: Call to action button type (e.g., 'LEARN_MORE', 'SIGN_UP', 'SHOP_NOW')
        instagram_actor_id: Optional Instagram account ID for Instagram placements
    
    Returns:
        JSON response with created creative details
    """
    # Check required parameters
    if not account_id:
        return json.dumps({"error": "No account ID provided"}, indent=2)
    
    if not image_hash:
        return json.dumps({"error": "No image hash provided"}, indent=2)
    
    if not name:
        name = f"Creative {int(time.time())}"
    
    # Ensure account_id has the 'act_' prefix
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"
    
    # Enhanced page discovery: If no page ID is provided, use robust discovery methods
    if not page_id:
        try:
            # Use the comprehensive page discovery logic from get_account_pages
            page_discovery_result = await _discover_pages_for_account(account_id, access_token)
            
            if page_discovery_result.get("success"):
                page_id = page_discovery_result["page_id"]
                page_name = page_discovery_result.get("page_name", "Unknown")
                print(f"Auto-discovered page ID: {page_id} ({page_name})")
            else:
                return json.dumps({
                    "error": "No page ID provided and no suitable pages found for this account",
                    "details": page_discovery_result.get("message", "Page discovery failed"),
                    "suggestions": [
                        "Use get_account_pages to see available pages",
                        "Use search_pages_by_name to find specific pages",
                        "Provide a page_id parameter manually"
                    ]
                }, indent=2)
        except Exception as e:
            return json.dumps({
                "error": "Error during page discovery",
                "details": str(e),
                "suggestion": "Please provide a page_id parameter or use get_account_pages to find available pages"
            }, indent=2)
    
    # Validate headline/description parameters - cannot mix simple and complex
    if headline and headlines:
        return json.dumps({"error": "Cannot specify both 'headline' and 'headlines'. Use 'headline' for single headline or 'headlines' for multiple."}, indent=2)
    
    if description and descriptions:
        return json.dumps({"error": "Cannot specify both 'description' and 'descriptions'. Use 'description' for single description or 'descriptions' for multiple."}, indent=2)
    
    # Convert simple parameters to complex format for internal processing
    final_headlines = None
    final_descriptions = None
    
    if headline:
        final_headlines = [headline]
    elif headlines:
        final_headlines = headlines
        
    if description:
        final_descriptions = [description]
    elif descriptions:
        final_descriptions = descriptions
    
    # Validate dynamic creative parameters
    if final_headlines:
        if len(final_headlines) > 5:
            return json.dumps({"error": "Maximum 5 headlines allowed for dynamic creatives"}, indent=2)
        for i, h in enumerate(final_headlines):
            if len(h) > 40:
                return json.dumps({"error": f"Headline {i+1} exceeds 40 character limit"}, indent=2)
    
    if final_descriptions:
        if len(final_descriptions) > 5:
            return json.dumps({"error": "Maximum 5 descriptions allowed for dynamic creatives"}, indent=2)
        for i, d in enumerate(final_descriptions):
            if len(d) > 125:
                return json.dumps({"error": f"Description {i+1} exceeds 125 character limit"}, indent=2)
    
    # Prepare the creative data
    creative_data = {
        "name": name
    }
    
    # Choose between asset_feed_spec (dynamic creative) or object_story_spec (traditional)
    if final_headlines or final_descriptions:
        # Use asset_feed_spec for dynamic creatives
        asset_feed_spec = {
            "ad_formats": ["SINGLE_IMAGE"],
            "images": [{"hash": image_hash}],
            "link_urls": [{"website_url": link_url if link_url else "https://facebook.com"}]
        }
        
        # Handle headlines
        if final_headlines:
            asset_feed_spec["headlines"] = [{"text": headline_text} for headline_text in final_headlines]
            
        # Handle descriptions  
        if final_descriptions:
            asset_feed_spec["descriptions"] = [{"text": description_text} for description_text in final_descriptions]
        
        # Add message as primary_texts if provided
        if message:
            asset_feed_spec["primary_texts"] = [{"text": message}]
        
        # Add call_to_action_types if provided
        if call_to_action_type:
            asset_feed_spec["call_to_action_types"] = [call_to_action_type]
        
        creative_data["asset_feed_spec"] = asset_feed_spec
        
        # For dynamic creatives, we need a simplified object_story_spec
        creative_data["object_story_spec"] = {
            "page_id": page_id
        }
    else:
        # Use traditional object_story_spec for single creative
        creative_data["object_story_spec"] = {
            "page_id": page_id,
            "link_data": {
                "image_hash": image_hash,
                "link": link_url if link_url else "https://facebook.com"
            }
        }
        
        # Add optional parameters if provided
        if message:
            creative_data["object_story_spec"]["link_data"]["message"] = message
    
    # Add dynamic creative spec if provided
    if dynamic_creative_spec:
        creative_data["dynamic_creative_spec"] = dynamic_creative_spec
    
    # Only add call_to_action to object_story_spec if we're not using asset_feed_spec
    if call_to_action_type and "asset_feed_spec" not in creative_data:
        creative_data["object_story_spec"]["link_data"]["call_to_action"] = {
            "type": call_to_action_type
        }
    
    if instagram_actor_id:
        creative_data["instagram_actor_id"] = instagram_actor_id
    
    # Prepare the API endpoint for creating a creative
    endpoint = f"{account_id}/adcreatives"
    
    try:
        # Make API request to create the creative
        data = await make_api_request(endpoint, access_token, creative_data, method="POST")
        
        # If successful, get more details about the created creative
        if "id" in data:
            creative_id = data["id"]
            creative_endpoint = f"{creative_id}"
            creative_params = {
                "fields": "id,name,status,thumbnail_url,image_url,image_hash,object_story_spec,asset_feed_spec,url_tags,link_url"
            }
            
            creative_details = await make_api_request(creative_endpoint, access_token, creative_params)
            return json.dumps({
                "success": True,
                "creative_id": creative_id,
                "details": creative_details
            }, indent=2)
        
        return json.dumps(data, indent=2)
    
    except Exception as e:
        return json.dumps({
            "error": "Failed to create ad creative",
            "details": str(e),
            "creative_data_sent": creative_data
        }, indent=2)


@mcp_server.tool()
@meta_api_tool
async def update_ad_creative(
    access_token: str = None,
    creative_id: str = None,
    name: str = None,
    message: str = None,
    headline: str = None,
    headlines: List[str] = None,
    description: str = None,
    descriptions: List[str] = None,
    dynamic_creative_spec: Dict[str, Any] = None,
    call_to_action_type: str = None
) -> str:
    """
    Update an existing ad creative with new content or settings.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        creative_id: Meta Ads creative ID to update
        name: New creative name
        message: New ad copy/text
        headline: Single headline for simple ads (cannot be used with headlines)
        headlines: New list of headlines for dynamic creative testing (cannot be used with headline)
        description: Single description for simple ads (cannot be used with descriptions)
        descriptions: New list of descriptions for dynamic creative testing (cannot be used with description)
        dynamic_creative_spec: New dynamic creative optimization settings
        call_to_action_type: New call to action button type
    
    Returns:
        JSON response with updated creative details
    """
    # Check required parameters
    if not creative_id:
        return json.dumps({"error": "No creative ID provided"}, indent=2)
    
    # Validate headline/description parameters - cannot mix simple and complex
    if headline and headlines:
        return json.dumps({"error": "Cannot specify both 'headline' and 'headlines'. Use 'headline' for single headline or 'headlines' for multiple."}, indent=2)
    
    if description and descriptions:
        return json.dumps({"error": "Cannot specify both 'description' and 'descriptions'. Use 'description' for single description or 'descriptions' for multiple."}, indent=2)
    
    # Convert simple parameters to complex format for internal processing
    final_headlines = None
    final_descriptions = None
    
    if headline:
        final_headlines = [headline]
    elif headlines:
        final_headlines = headlines
        
    if description:
        final_descriptions = [description]
    elif descriptions:
        final_descriptions = descriptions
    
    # Validate dynamic creative parameters
    if final_headlines:
        if len(final_headlines) > 5:
            return json.dumps({"error": "Maximum 5 headlines allowed for dynamic creatives"}, indent=2)
        for i, h in enumerate(final_headlines):
            if len(h) > 40:
                return json.dumps({"error": f"Headline {i+1} exceeds 40 character limit"}, indent=2)
    
    if final_descriptions:
        if len(final_descriptions) > 5:
            return json.dumps({"error": "Maximum 5 descriptions allowed for dynamic creatives"}, indent=2)
        for i, d in enumerate(final_descriptions):
            if len(d) > 125:
                return json.dumps({"error": f"Description {i+1} exceeds 125 character limit"}, indent=2)
    
    # Prepare the update data
    update_data = {}
    
    if name:
        update_data["name"] = name
    
    if message:
        update_data["object_story_spec"] = {"link_data": {"message": message}}
    
    # Handle dynamic creative assets via asset_feed_spec
    if final_headlines or final_descriptions or dynamic_creative_spec:
        asset_feed_spec = {}
        
        # Add required ad_formats field for dynamic creatives
        asset_feed_spec["ad_formats"] = ["SINGLE_IMAGE"]
        
        # Handle headlines
        if final_headlines:
            asset_feed_spec["headlines"] = [{"text": headline_text} for headline_text in final_headlines]
            
        # Handle descriptions  
        if final_descriptions:
            asset_feed_spec["descriptions"] = [{"text": description_text} for description_text in final_descriptions]
        
        # Add message as primary_texts if provided
        if message:
            asset_feed_spec["primary_texts"] = [{"text": message}]
        
        update_data["asset_feed_spec"] = asset_feed_spec
    
    # Add dynamic creative spec if provided
    if dynamic_creative_spec:
        update_data["dynamic_creative_spec"] = dynamic_creative_spec
    
    # Handle call_to_action - add to asset_feed_spec if using dynamic creative, otherwise to object_story_spec
    if call_to_action_type:
        if "asset_feed_spec" in update_data:
            update_data["asset_feed_spec"]["call_to_action_types"] = [call_to_action_type]
        else:
            if "object_story_spec" not in update_data:
                update_data["object_story_spec"] = {"link_data": {}}
            update_data["object_story_spec"]["link_data"]["call_to_action"] = {
                "type": call_to_action_type
            }
    
    # Prepare the API endpoint for updating the creative
    endpoint = f"{creative_id}"
    
    try:
        # Make API request to update the creative
        data = await make_api_request(endpoint, access_token, update_data, method="POST")
        
        # If successful, get more details about the updated creative
        if "id" in data:
            creative_endpoint = f"{creative_id}"
            creative_params = {
                "fields": "id,name,status,thumbnail_url,image_url,image_hash,object_story_spec,url_tags,link_url,dynamic_creative_spec"
            }
            
            creative_details = await make_api_request(creative_endpoint, access_token, creative_params)
            return json.dumps({
                "success": True,
                "creative_id": creative_id,
                "details": creative_details
            }, indent=2)
        
        return json.dumps(data, indent=2)
    
    except Exception as e:
        return json.dumps({
            "error": "Failed to update ad creative",
            "details": str(e),
            "update_data_sent": update_data
        }, indent=2)


async def _discover_pages_for_account(account_id: str, access_token: str) -> dict:
    """
    Internal function to discover pages for an account using multiple approaches.
    Returns the best available page ID for ad creation.
    """
    try:
        # Approach 1: Extract page IDs from tracking_specs in ads (most reliable)
        endpoint = f"{account_id}/ads"
        params = {
            "fields": "id,name,adset_id,campaign_id,status,creative,created_time,updated_time,bid_amount,conversion_domain,tracking_specs",
            "limit": 100
        }
        
        tracking_ads_data = await make_api_request(endpoint, access_token, params)
        
        tracking_page_ids = set()
        if "data" in tracking_ads_data:
            for ad in tracking_ads_data.get("data", []):
                tracking_specs = ad.get("tracking_specs", [])
                if isinstance(tracking_specs, list):
                    for spec in tracking_specs:
                        if isinstance(spec, dict) and "page" in spec:
                            page_list = spec["page"]
                            if isinstance(page_list, list):
                                for page_id in page_list:
                                    if isinstance(page_id, (str, int)) and str(page_id).isdigit():
                                        tracking_page_ids.add(str(page_id))
        
        if tracking_page_ids:
            # Get details for the first page found
            page_id = list(tracking_page_ids)[0]
            page_endpoint = f"{page_id}"
            page_params = {
                "fields": "id,name,username,category,fan_count,link,verification_status,picture"
            }
            
            page_data = await make_api_request(page_endpoint, access_token, page_params)
            if "id" in page_data:
                return {
                    "success": True,
                    "page_id": page_id,
                    "page_name": page_data.get("name", "Unknown"),
                    "source": "tracking_specs",
                    "note": "Page ID extracted from existing ads - most reliable for ad creation"
                }
        
        # Approach 2: Try client_pages endpoint
        endpoint = f"{account_id}/client_pages"
        params = {
            "fields": "id,name,username,category,fan_count,link,verification_status,picture"
        }
        
        client_pages_data = await make_api_request(endpoint, access_token, params)
        
        if "data" in client_pages_data and client_pages_data["data"]:
            page = client_pages_data["data"][0]
            return {
                "success": True,
                "page_id": page["id"],
                "page_name": page.get("name", "Unknown"),
                "source": "client_pages"
            }
        
        # Approach 3: Try assigned_pages endpoint
        pages_endpoint = f"{account_id}/assigned_pages"
        pages_params = {
            "fields": "id,name",
            "limit": 1 
        }
        
        pages_data = await make_api_request(pages_endpoint, access_token, pages_params)
        
        if "data" in pages_data and pages_data["data"]:
            page = pages_data["data"][0]
            return {
                "success": True,
                "page_id": page["id"],
                "page_name": page.get("name", "Unknown"),
                "source": "assigned_pages"
            }
        
        # If all approaches failed
        return {
            "success": False,
            "message": "No suitable pages found for this account",
            "note": "Try using get_account_pages to see all available pages or provide page_id manually"
        }
        
    except Exception as e:
        return {
            "success": False,
            "message": f"Error during page discovery: {str(e)}"
        }


async def _search_pages_by_name_core(access_token: str, account_id: str, search_term: str = None) -> str:
    """
    Core logic for searching pages by name.
    
    Args:
        access_token: Meta API access token
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
        search_term: Search term to find pages by name (optional - returns all pages if not provided)
    
    Returns:
        JSON string with search results
    """
    # Ensure account_id has the 'act_' prefix
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"
    
    try:
        # Use the internal discovery function directly
        page_discovery_result = await _discover_pages_for_account(account_id, access_token)
        
        if not page_discovery_result.get("success"):
            return json.dumps({
                "data": [],
                "message": "No pages found for this account",
                "details": page_discovery_result.get("message", "Page discovery failed")
            }, indent=2)
        
        # Create a single page result
        page_data = {
            "id": page_discovery_result["page_id"],
            "name": page_discovery_result.get("page_name", "Unknown"),
            "source": page_discovery_result.get("source", "unknown")
        }
        
        all_pages_data = {"data": [page_data]}
        
        # Filter pages by search term if provided
        if search_term:
            search_term_lower = search_term.lower()
            filtered_pages = []
            
            for page in all_pages_data["data"]:
                page_name = page.get("name", "").lower()
                if search_term_lower in page_name:
                    filtered_pages.append(page)
            
            return json.dumps({
                "data": filtered_pages,
                "search_term": search_term,
                "total_found": len(filtered_pages),
                "total_available": len(all_pages_data["data"])
            }, indent=2)
        else:
            # Return all pages if no search term provided
            return json.dumps({
                "data": all_pages_data["data"],
                "total_available": len(all_pages_data["data"]),
                "note": "Use search_term parameter to filter pages by name"
            }, indent=2)
    
    except Exception as e:
        return json.dumps({
            "error": "Failed to search pages by name",
            "details": str(e)
        }, indent=2)


@mcp_server.tool()
@meta_api_tool
async def search_pages_by_name(access_token: str = None, account_id: str = None, search_term: str = None) -> str:
    """
    Search for pages by name within an account.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
        search_term: Search term to find pages by name (optional - returns all pages if not provided)
    
    Returns:
        JSON response with matching pages
    """
    # Check required parameters
    if not account_id:
        return json.dumps({"error": "No account ID provided"}, indent=2)
    
    # Call the core function
    result = await _search_pages_by_name_core(access_token, account_id, search_term)
    return result


@mcp_server.tool()
@meta_api_tool
async def get_account_pages(access_token: str = None, account_id: str = None) -> str:
    """
    Get pages associated with a Meta Ads account.
    
    Args:
        access_token: Meta API access token (optional - will use cached token if not provided)
        account_id: Meta Ads account ID (format: act_XXXXXXXXX)
    
    Returns:
        JSON response with pages associated with the account
    """
    # Check required parameters
    if not account_id:
        return json.dumps({"error": "No account ID provided"}, indent=2)
    
    # Handle special case for 'me'
    if account_id == "me":
        try:
            endpoint = "me/accounts"
            params = {
                "fields": "id,name,username,category,fan_count,link,verification_status,picture"
            }
            
            user_pages_data = await make_api_request(endpoint, access_token, params)
            return json.dumps(user_pages_data, indent=2)
        except Exception as e:
            return json.dumps({
                "error": "Failed to get user pages",
                "details": str(e)
            }, indent=2)
    
    # Ensure account_id has the 'act_' prefix for regular accounts
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"
    
    try:
        # Try all approaches that might work
        
        # Approach 1: Get active ads and extract page IDs
        endpoint = f"{account_id}/ads"
        params = {
            "fields": "creative{object_story_spec{page_id}}",
            "limit": 100
        }
        
        ads_data = await make_api_request(endpoint, access_token, params)
        
        # Extract unique page IDs from ads
        page_ids = set()
        if "data" in ads_data:
            for ad in ads_data.get("data", []):
                if "creative" in ad and "creative" in ad and "object_story_spec" in ad["creative"] and "page_id" in ad["creative"]["object_story_spec"]:
                    page_ids.add(ad["creative"]["object_story_spec"]["page_id"])
        
        # If we found page IDs, get details for each
        if page_ids:
            page_details = {"data": []}
            
            for page_id in page_ids:
                page_endpoint = f"{page_id}"
                page_params = {
                    "fields": "id,name,username,category,fan_count,link,verification_status,picture"
                }
                
                page_data = await make_api_request(page_endpoint, access_token, page_params)
                if "id" in page_data:
                    page_details["data"].append(page_data)
            
            if page_details["data"]:
                return json.dumps(page_details, indent=2)
        
        # Approach 2: Try client_pages endpoint
        endpoint = f"{account_id}/client_pages"
        params = {
            "fields": "id,name,username,category,fan_count,link,verification_status,picture"
        }
        
        client_pages_data = await make_api_request(endpoint, access_token, params)
        
        if "data" in client_pages_data and client_pages_data["data"]:
            return json.dumps(client_pages_data, indent=2)
        
        # Approach 3: Try promoted_objects endpoint to find page IDs
        endpoint = f"{account_id}/promoted_objects"
        params = {
            "fields": "page_id"
        }
        
        promoted_objects_data = await make_api_request(endpoint, access_token, params)
        
        if "data" in promoted_objects_data and promoted_objects_data["data"]:
            page_ids = set()
            for obj in promoted_objects_data["data"]:
                if "page_id" in obj:
                    page_ids.add(obj["page_id"])
            
            if page_ids:
                page_details = {"data": []}
                for page_id in page_ids:
                    page_endpoint = f"{page_id}"
                    page_params = {
                        "fields": "id,name,username,category,fan_count,link,verification_status,picture"
                    }
                    
                    page_data = await make_api_request(page_endpoint, access_token, page_params)
                    if "id" in page_data:
                        page_details["data"].append(page_data)
                
                if page_details["data"]:
                    return json.dumps(page_details, indent=2)
        
        # Approach 4: Extract page IDs from tracking_specs in ads
        # Inspired by praveen92y's implementation for robust page detection
        # This approach is often the most reliable as confirmed by community feedback
        endpoint = f"{account_id}/ads"
        params = {
            "fields": "id,name,adset_id,campaign_id,status,creative,created_time,updated_time,bid_amount,conversion_domain,tracking_specs",
            "limit": 100
        }
        
        tracking_ads_data = await make_api_request(endpoint, access_token, params)
        
        tracking_page_ids = set()
        if "data" in tracking_ads_data:
            for ad in tracking_ads_data.get("data", []):
                tracking_specs = ad.get("tracking_specs", [])
                if isinstance(tracking_specs, list):
                    for spec in tracking_specs:
                        # If 'page' key exists, add all page IDs
                        if isinstance(spec, dict) and "page" in spec:
                            page_list = spec["page"]
                            if isinstance(page_list, list):
                                for page_id in page_list:
                                    # Validate page ID format (should be numeric string)
                                    if isinstance(page_id, (str, int)) and str(page_id).isdigit():
                                        tracking_page_ids.add(str(page_id))
        
        if tracking_page_ids:
            page_details = {"data": [], "source": "tracking_specs", "note": "Page IDs extracted from active ads - these are the most reliable for ad creation"}
            for page_id in tracking_page_ids:
                page_endpoint = f"{page_id}"
                page_params = {
                    "fields": "id,name,username,category,fan_count,link,verification_status,picture"
                }
                
                page_data = await make_api_request(page_endpoint, access_token, page_params)
                if "id" in page_data:
                    # Add additional context about this page ID being suitable for ads
                    page_data["_meta"] = {
                        "suitable_for_ads": True,
                        "found_in_tracking_specs": True,
                        "recommended_for_create_ad_creative": True
                    }
                    page_details["data"].append(page_data)
                else:
                    page_details["data"].append({
                        "id": page_id, 
                        "error": "Page details not found",
                        "_meta": {
                            "suitable_for_ads": True,
                            "found_in_tracking_specs": True,
                            "note": "Page ID exists in ads but details not accessible - you can still use this ID for ad creation"
                        }
                    })
            
            if page_details["data"]:
                return json.dumps(page_details, indent=2)
        
        # If all approaches failed, return empty data with a message
        return json.dumps({
            "data": [],
            "message": "No pages found associated with this account using automated methods",
            "troubleshooting": {
                "suggestion_1": "If you have existing ads, run 'get_ads' and look for page IDs in the 'tracking_specs' field",
                "suggestion_2": "Use the exact page ID from existing ads' tracking_specs for creating new ad creatives",
                "suggestion_3": "Verify your page ID format - it should be a numeric string (e.g., '123456789')",
                "suggestion_4": "Check for digit transpositions or formatting errors in your page ID"
            },
            "note": "Based on community feedback, page IDs from existing ads' tracking_specs are the most reliable for ad creation"
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": "Failed to get account pages",
            "details": str(e)
        }, indent=2)

@mcp_server.tool()
@meta_api_tool
async def upload_ad_video(
    access_token: str,
    account_id: str,
    video_path: str,
    name: Optional[str] = None,
    description: Optional[str] = None
) -> str:
    """
    Upload a video file to Meta Ads account for use in video creatives.

    Args:
        access_token (str): Meta API access token
        account_id (str): Meta Ads account ID (format: act_XXXXXXXXX)
        video_path (str): Path to local video file (MP4/MOV)
        name (str, optional): Optional name for the video
        description (str, optional): Optional video description

    Returns:
        JSON response with video_id and status
    """
    import aiohttp

    if not os.path.exists(video_path):
        return json.dumps({"error": f"Video file not found: {video_path}"}, indent=2)

    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"

    url = f"https://graph-video.facebook.com/v18.0/{account_id}/advideos"

    try:
        async with aiohttp.ClientSession() as session:
            with open(video_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("source", f, filename=os.path.basename(video_path))
                if name:
                    data.add_field("name", name)
                if description:
                    data.add_field("description", description)
                data.add_field("access_token", access_token)

                async with session.post(url, data=data) as resp:
                    resp_json = await resp.json()
                    return json.dumps(resp_json, indent=2)
    except Exception as e:
        return json.dumps({"error": "Video upload failed", "details": str(e)}, indent=2)

@mcp_server.tool()
@meta_api_tool
async def create_ad_creative_from_video(
    access_token: str,
    account_id: str,
    page_id: str,
    video_id: str,
    name: Optional[str] = None,
    message: Optional[str] = None,
    call_to_action_type: Optional[str] = "LEARN_MORE",
    link_url: Optional[str] = None,
    image_hash: Optional[str] = None
) -> str:
    """
    Create a video ad creative using a previously uploaded video.

    Args:
        access_token: Meta API token
        account_id: Ad account ID (e.g., act_123...)
        page_id: Facebook Page ID that sponsors the ad
        video_id: ID returned from video upload
        name: Optional creative name
        message: Optional text to appear above the video
        call_to_action_type: CTA type (e.g., LEARN_MORE, SHOP_NOW)
        link_url: Destination URL for CTA
        image_hash: Required thumbnail image hash for the video

    Returns:
        JSON response with creative_id and details
    """
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"

    if not image_hash:
        return json.dumps({
            "error": "Missing image_hash. Meta Ads requires a video thumbnail for all video creatives."
        }, indent=2)

    endpoint = f"{account_id}/adcreatives"

    payload = {
        "name": name or "Video Creative",
        "object_story_spec": {
            "page_id": page_id,
            "video_data": {
                "video_id": video_id,
                "message": message or "Check this out!",
                "call_to_action": {
                    "type": call_to_action_type,
                    "value": {
                        "link": link_url or "https://example.com"
                    }
                },
                "image_hash": image_hash
            }
        }
    }

    result = await make_api_request(endpoint, access_token, payload, method="POST")
    return json.dumps(result, indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_ad_from_creative(
    access_token: str,
    account_id: str,
    adset_id: str,
    creative_id: str,
    name: Optional[str] = None,
    status: Optional[str] = "PAUSED"
) -> str:
    """
    Create an ad from an existing creative.

    Args:
        access_token: Meta API token
        account_id: Ad account ID (e.g., act_123...)
        adset_id: ID of the ad set to attach this ad to
        creative_id: ID of the creative (e.g., from image or video)
        name: Ad name
        status: Ad status (PAUSED, ACTIVE, etc.)

    Returns:
        JSON response with ad_id and details
    """
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"

    endpoint = f"{account_id}/ads"

    payload = {
        "name": name or "Ad from creative",
        "adset_id": adset_id,
        "creative": {"creative_id": creative_id},
        "status": status
    }

    result = await make_api_request(endpoint, access_token, payload, method="POST")
    return json.dumps(result, indent=2)

@mcp_server.tool()
@meta_api_tool
async def create_ad_from_video_file(
    access_token: str,
    account_id: str,
    adset_id: str,
    page_id: str,
    video_path: str,
    ad_name: Optional[str] = None,
    creative_name: Optional[str] = None,
    message: Optional[str] = None,
    call_to_action_type: Optional[str] = "LEARN_MORE",
    link_url: Optional[str] = None,
    status: Optional[str] = "PAUSED",
    image_hash: Optional[str] = None
) -> str:
    """
    Create a complete video ad (video upload → creative → ad) in one step.

    Args:
        access_token: Meta API token
        account_id: Ad account ID (e.g., act_123...)
        adset_id: Ad set ID to associate the ad
        page_id: Facebook Page ID sponsoring the ad
        video_path: Local path to video (MP4/MOV)
        ad_name: Name for the ad (optional)
        creative_name: Name for the creative (optional)
        message: Text above the video (optional)
        call_to_action_type: CTA type (e.g., LEARN_MORE, SHOP_NOW)
        link_url: URL for the CTA
        status: Initial status of the ad (PAUSED/ACTIVE)

    Returns:
        JSON response with ad_id, creative_id, and video_id
    """
    if not os.path.exists(video_path):
        return json.dumps({"error": f"Video file not found: {video_path}"}, indent=2)

    video_result = await upload_ad_video(
        access_token=access_token,
        account_id=account_id,
        video_path=video_path,
        name=creative_name,
        description=message,
    )

    video_data = json.loads(video_result)
    if "id" not in video_data:
        return json.dumps({"error": "Video upload failed", "details": video_data}, indent=2)

    video_id = video_data["id"]

    creative_result = await create_ad_creative_from_video(
        access_token=access_token,
        account_id=account_id,
        page_id=page_id,
        video_id=video_id,
        name=creative_name,
        message=message,
        call_to_action_type=call_to_action_type,
        link_url=link_url,
        image_hash=image_hash
    )

    creative_data = json.loads(creative_result)
    if "id" not in creative_data:
        return json.dumps({"error": "Creative creation failed", "details": creative_data}, indent=2)

    creative_id = creative_data["id"]

    ad_result = await create_ad_from_creative(
        access_token=access_token,
        account_id=account_id,
        adset_id=adset_id,
        creative_id=creative_id,
        name=ad_name,
        status=status,
    )

    ad_data = json.loads(ad_result)
    if "id" not in ad_data:
        return json.dumps({"error": "Ad creation failed", "details": ad_data}, indent=2)

    return json.dumps({
        "video_id": video_id,
        "creative_id": creative_id,
        "ad_id": ad_data["id"]
    }, indent=2)

@mcp_server.tool()
@meta_api_tool
async def upload_multiple_ad_images(
    access_token: str,
    account_id: str,
    images: List[Dict[str, Optional[str]]],
) -> str:
    """
    Upload multiple images to Meta Ads account using MCP.

    Each image dict must contain:
      - image_path: Local file path
      - name: Optional image name
      - ad_image_crops: Optional crop dictionary

    Example input:
    images=[
      {
        "image_path": "/tmp/img1.jpg",
        "name": "creative1",
        "ad_image_crops": {"1000x1000": [[0, 0, 1000, 1000]]}
      },
      {
        "image_path": "/tmp/img2.jpg",
        "name": "creative2"
      }
    ]

    Returns:
        JSON string with list of results per image
    """

    if not access_token or not account_id:
        return json.dumps({"error": "Missing access_token or account_id"}, indent=2)

    if not images:
        return json.dumps({"error": "No images provided"}, indent=2)

    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"

    results = []
    for i, img in enumerate(images):
        image_path = img.get("image_path")
        name = img.get("name")
        ad_image_crops = img.get("ad_image_crops")

        if not image_path or not os.path.exists(image_path):
            results.append({"index": i, "error": f"File not found: {image_path}"})
            continue

        result = await upload_ad_image(
            access_token=access_token,
            account_id=account_id,
            image_path=image_path,
            name=name,
            ad_image_crops=ad_image_crops,
        )

        results.append(json.loads(result))

    return json.dumps(results, indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_lookalike_audience(
    account_id: str,
    access_token: Optional[str] = None,
    name: Optional[str] = None,
    lookalike_spec: Optional[Dict[str, Any]] = None,
    origin_audience_id: Optional[str] = None,
    country: Optional[str] = None,    # ISO-2 (e.g., "BR", "US")
    ratio: Optional[float] = None,    # e.g., 0.01 for 1%
    description: Optional[str] = None,
    is_private: Optional[bool] = None,
    subtype: str = "LOOKALIKE"
) -> str:
    """
    Create a Lookalike Audience on the Ad Account.

    You can either:
      - Provide a full 'lookalike_spec' (Option A), or
      - Provide 'origin_audience_id' + 'country' + 'ratio' (Option B), and we assemble a minimal spec.

    Notes:
      - The Marketing API accepts several shapes for lookalike_spec. This tool keeps it flexible:
        if you pass 'lookalike_spec' we forward it; else we build a simple similarity spec from the origin.
      - 'ratio' is a float (e.g., 0.01 for 1%). If you omit it, the platform will pick defaults.

    Args:
        account_id: Ad Account in the form "act_<ID>".
        access_token: Graph token.
        name: Optional audience name (recommended).
        lookalike_spec: Raw lookalike spec to forward to the API.
        origin_audience_id: Custom Audience seed for Option B.
        country: ISO-2 country for Option B.
        ratio: Similarity ratio for Option B (e.g., 0.01).
        description: Optional description.
        is_private: Optional privacy flag.
        subtype: Must be "LOOKALIKE".

    Returns:
        JSON with the created Custom Audience or a structured error.
    """
    if not account_id:
        return json.dumps({"error": "account_id is required (e.g., 'act_123')" }, indent=2)
    if not name:
        return json.dumps({"error": "name is recommended to identify the lookalike"}, indent=2)
    if not lookalike_spec:
        if not origin_audience_id or not country:
            return json.dumps({
                "error": "Provide either a full lookalike_spec OR origin_audience_id + country",
                "hint": "For a simple 1% LAL: origin_audience_id='<CA_ID>', country='BR', ratio=0.01"
            }, indent=2)
        lookalike_spec = {
            # Keep keys minimal and forward-compatible; platform may extend/ignore specifics
            "country": country,
            "origin_audience_id": origin_audience_id
        }
        if ratio is not None:
            lookalike_spec["ratio"] = float(ratio)

    endpoint = f"{account_id}/customaudiences"
    params: Dict[str, Any] = {
        "name": name,
        "subtype": subtype,
        "lookalike_spec": json.dumps(lookalike_spec)
    }
    if description is not None:
        params["description"] = description
    if is_private is not None:
        params["is_private"] = "true" if is_private else "false"

    try:
        data = await make_api_request(endpoint, access_token, params, method="POST")
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": "Failed to create lookalike audience", "details": str(e), "params_sent": params}, indent=2)


@mcp_server.tool()
@meta_api_tool
async def create_ad_creative(
    account_id: str,
    access_token: Optional[str] = None,
    name: Optional[str] = None,
    page_id: Optional[str] = None,
    instagram_actor_id: Optional[str] = None,
    format: str = "SINGLE_IMAGE",                     # SINGLE_IMAGE | SINGLE_VIDEO | CAROUSEL | EXISTING_POST
    object_story_id: Optional[str] = None,            # for EXISTING_POST
    message: Optional[str] = None,
    headline: Optional[str] = None,
    description: Optional[str] = None,
    call_to_action_type: Optional[str] = None,        # e.g., LEARN_MORE, SHOP_NOW
    link_url: Optional[str] = None,
    template_url_spec: Optional[Dict[str, Any]] = None, # UTM builder
    app_link_spec: Optional[Dict[str, Any]] = None,     # deep links
    instant_experience_id: Optional[str] = None,        # Canvas
    image_hash: Optional[str] = None,
    video_id: Optional[str] = None,
    carousel_attachments: Optional[List[Dict[str, Any]]] = None,  # [{link, image_hash|video_id, name?, description?}, ...]
    additional_image_variants: Optional[List[Dict[str, Any]]] = None,
    sponsorship_info_spec: Optional[Dict[str, Any]] = None,       # e.g., {"partnership_ad_code": "...", ...}
    creative_overrides: Optional[Dict[str, Any]] = None
) -> str:
    """
    Pre-create an Ad Creative. This is useful when you want to:
      - Build creatives ahead of time and reference them via creative_id in `create_ad`.
      - Generate additional variants (square/landscape) for the same asset, returning multiple creative IDs.

    Behavior:
      - If format == EXISTING_POST: requires object_story_id (boost).
      - SINGLE_IMAGE: requires image_hash and link_url (for link ads).
      - SINGLE_VIDEO: requires video_id; link_url optional (CTA may carry the link).
      - CAROUSEL: requires >= 2 attachments with link + media.
      - Instant Experience is attached via post_click_configuration.instant_experience_id.
      - If additional_image_variants is provided, we will create extra creatives for each image variant
        (e.g., square/landscape), returning a list of created creative IDs.

    Returns:
        JSON:
        {
          "primary_creative": {"id":"..."},
          "variant_creatives": [{"id":"..."}, ...]   # only when additional_image_variants provided
        }
    """
    if not account_id:
        return json.dumps({"error":"account_id is required (act_<ID>)"}, indent=2)

    # Build base creative payload
    creative: Dict[str, Any] = {}
    if format == "EXISTING_POST":
        if not object_story_id:
            return json.dumps({"error":"object_story_id is required for EXISTING_POST"}, indent=2)
        creative["object_story_id"] = object_story_id
    else:
        if not (page_id or instagram_actor_id):
            return json.dumps({"error":"Provide page_id and/or instagram_actor_id for inline creative identity"}, indent=2)

        object_story_spec: Dict[str, Any] = {}
        if page_id: object_story_spec["page_id"] = page_id
        if instagram_actor_id: object_story_spec["instagram_actor_id"] = instagram_actor_id

        if format in ("SINGLE_IMAGE", "CAROUSEL"):
            if not link_url:
                return json.dumps({"error":"link_url is required for image/carousel link creatives"}, indent=2)
            link_data: Dict[str, Any] = {"link": link_url}
            if message: link_data["message"] = message
            if headline: link_data["name"] = headline
            if description: link_data["description"] = description
            if call_to_action_type:
                link_data["call_to_action"] = {"type": call_to_action_type, "value": {"link": link_url}}
            if template_url_spec:
                link_data["template_url_spec"] = template_url_spec
            if app_link_spec:
                link_data["app_link_spec"] = app_link_spec

            if format == "SINGLE_IMAGE":
                if not image_hash:
                    return json.dumps({"error":"image_hash is required for SINGLE_IMAGE"}, indent=2)
                link_data["image_hash"] = image_hash

            if format == "CAROUSEL":
                if not carousel_attachments or len(carousel_attachments) < 2:
                    return json.dumps({"error":"carousel_attachments must have at least 2 items"}, indent=2)
                children = []
                for idx, att in enumerate(carousel_attachments, start=1):
                    if "link" not in att:
                        return json.dumps({"error": f"carousel_attachments[{idx}] needs 'link'"}, indent=2)
                    if not any(k in att for k in ("image_hash","video_id")):
                        return json.dumps({"error": f"carousel_attachments[{idx}] needs 'image_hash' or 'video_id'"}, indent=2)
                    child = {"link": att["link"]}
                    if "image_hash" in att: child["image_hash"] = att["image_hash"]
                    if "video_id" in att: child["video_id"] = att["video_id"]
                    if "name" in att: child["name"] = att["name"]
                    if "description" in att: child["description"] = att["description"]
                    if call_to_action_type:
                        child["call_to_action"] = {"type": call_to_action_type, "value": {"link": att["link"]}}
                    children.append(child)
                link_data["child_attachments"] = children

            object_story_spec["link_data"] = link_data

        if format == "SINGLE_VIDEO":
            if not video_id:
                return json.dumps({"error":"video_id is required for SINGLE_VIDEO"}, indent=2)
            video_data: Dict[str, Any] = {"video_id": video_id}
            if message: video_data["message"] = message
            if call_to_action_type and link_url:
                video_data["call_to_action"] = {"type": call_to_action_type, "value": {"link": link_url}}
            if link_url:
                video_data["link_description"] = description or ""
            object_story_spec["video_data"] = video_data

        # Instant Experience
        if instant_experience_id:
            creative["post_click_configuration"] = {"instant_experience_id": instant_experience_id}

        # Sponsorship / branded content
        if sponsorship_info_spec:
            if "link_data" in object_story_spec:
                object_story_spec.setdefault("link_data", {})["sponsorship_info_spec"] = sponsorship_info_spec
            else:
                creative["branded_content"] = sponsorship_info_spec

        creative["object_story_spec"] = object_story_spec

    if name:
        creative["name"] = name

    # Apply overrides last
    if creative_overrides:
        creative.update(creative_overrides)

    # Create primary creative
    endpoint = f"{account_id}/adcreatives"
    try:
        primary = await make_api_request(endpoint, access_token, {"object_story_spec": json.dumps(creative.get("object_story_spec", {})),
                                                                  **({ "name": name } if name else {}),
                                                                  **({ "post_click_configuration": json.dumps(creative["post_click_configuration"]) } if "post_click_configuration" in creative else {}),
                                                                  **({ "branded_content": json.dumps(creative["branded_content"]) } if "branded_content" in creative else {}),
                                                                  **({k: v for k, v in creative.items() if k not in ("object_story_spec","post_click_configuration","branded_content","name")} )}, method="POST")
    except Exception as e:
        return json.dumps({"error":"Failed to create primary creative","details": str(e)}, indent=2)

    # Optionally create variant creatives (e.g., square/landscape alternatives)
    variants: List[Dict[str, Any]] = []
    if additional_image_variants and format == "SINGLE_IMAGE":
        for var in additional_image_variants:
            # var: {"image_hash": "...", "name_suffix": "[Square]"} (link_url/headline/cta reused)
            alt_hash = var.get("image_hash")
            if not alt_hash:
                variants.append({"error": "variant missing image_hash"})
                continue
            alt_name = f"{name} {var.get('name_suffix','[Variant]')}" if name else None
            alt_spec = json.loads(json.dumps(creative))  # deep copy
            # Replace the image in link_data
            try:
                alt_spec["object_story_spec"]["link_data"]["image_hash"] = alt_hash
            except Exception:
                variants.append({"error":"could not rewrite variant link_data"})
                continue

            try:
                payload = {
                    "object_story_spec": json.dumps(alt_spec["object_story_spec"]),
                }
                if alt_name:
                    payload["name"] = alt_name
                if "post_click_configuration" in alt_spec:
                    payload["post_click_configuration"] = json.dumps(alt_spec["post_click_configuration"])
                if "branded_content" in alt_spec:
                    payload["branded_content"] = json.dumps(alt_spec["branded_content"])
                v = await make_api_request(endpoint, access_token, payload, method="POST")
                variants.append(v)
            except Exception as e:
                variants.append({"error":"Failed to create variant creative","details": str(e)})

    return json.dumps({
        "primary_creative": primary,
        **({"variant_creatives": variants} if variants else {})
    }, indent=2)
