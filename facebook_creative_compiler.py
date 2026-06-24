"""Compiler: Layer 2 creative definition → Facebook AdCreative payload.

Pure function — no DB, no network. The service layer (facebook_ads.py) resolves
image hashes + the store row + promo coupon and hands them in.

Substitution discipline (load-bearing):
    ${...}   — OUR variables. Resolved here, at compile time, from the store row
               + creative definition + promo coupon.
    {{...}}  — FACEBOOK's own delivery-time macros (e.g. {{campaign.id}},
               {{product.name}}). Passed through UNTOUCHED.
    $75      — a bare "$" not forming a valid ${name} is left literal (string
               .Template.safe_substitute leaves invalid/unknown patterns as-is),
               so ad copy like "Get up to $75 off" survives unharmed.

v1 supports kind == "image" with three image modes:
    "uniform"        — one image. Single copy → object_story_spec.link_data; multiple
                       copy variations → DEGREES_OF_FREEDOM asset_feed_spec.
    "multi_image"    — several images (NOT placement-specific) → DEGREES_OF_FREEDOM
                       asset_feed_spec, so Facebook optimizes across the images and
                       the copy variations (Advantage+ creative). No
                       asset_customization_rules.
    "per_placement"  — one image per placement, mapped via asset_customization_rules
                       (optimization_type PLACEMENT — the fb25go offer_var shape).
carousel / video are future kinds — add a branch, no schema change.
"""

from string import Template

# Placement bucket → Facebook customization_spec positions. Used only in
# per_placement mode. Derived from fb25go's offer_var_1 asset_customization_rules.
PLACEMENT_SPECS = {
    "feed": {},  # default rule — age only, no position restriction
    "story_reels": {
        "publisher_platforms": ["facebook", "instagram", "messenger"],
        "facebook_positions": ["facebook_reels", "story"],
        "instagram_positions": ["ig_search", "story", "reels"],
        "messenger_positions": ["story"],
    },
    "right_column": {
        "publisher_platforms": ["facebook"],
        "facebook_positions": ["right_hand_column", "search"],
    },
}

# Canonical rule ordering in the payload (feed=1, story_reels=2, right_column=3).
# Placements not present in the creative's images are skipped.
PLACEMENT_PRIORITY = ["feed", "story_reels", "right_column"]

# Age targeting lives on the ad set / campaign (the single source of truth); these
# are only the fallback when a caller doesn't pass an explicit age (e.g. a creative
# compiled outside a campaign). Keep in sync with facebook_ads.DEFAULT_AGE_*.
DEFAULT_AGE_MIN = 18
DEFAULT_AGE_MAX = 65

# Objective → ad-set optimization_goal. NOT used by the creative compiler (the
# ad set is built at opt-in time in facebook_ads.py); exported here as the single
# source of truth for that mapping.
OBJECTIVE_OPTIMIZATION_GOAL = {
    "awareness": "REACH",
    # leads optimize for an offsite pixel conversion (the scheduler confirmation),
    # so the ad set carries a pixel promoted_object — not LEAD_GENERATION (which
    # is for on-Facebook instant forms and is incompatible with a pixel object).
    "leads": "OFFSITE_CONVERSIONS",
    "traffic": "LINK_CLICKS",
}

# "Start with all" optimization set — every creative_features_spec toggle from
# fb25go's offer_var_1. The form defaults to this; the definition may override.
ALL_CREATIVE_FEATURES = {
    "advantage_plus_creative": {"enroll_status": "OPT_OUT"},
    "cv_transformation": {"enroll_status": "OPT_IN"},
    "enhance_cta": {"enroll_status": "OPT_IN"},
    "image_animation": {"enroll_status": "OPT_IN"},
    "image_brightness_and_contrast": {"enroll_status": "OPT_IN"},
    "image_templates": {"enroll_status": "OPT_IN"},
    "image_touchups": {"enroll_status": "OPT_IN"},
    "inline_comment": {"enroll_status": "OPT_IN"},
    "pac_relaxation": {"enroll_status": "OPT_IN"},
    "product_extensions": {
        "customizations": {"pe_carousel": {"enroll_status": "OPT_IN"}},
        "enroll_status": "OPT_IN",
    },
    "reveal_details_over_time": {"enroll_status": "OPT_IN"},
    "site_extensions": {"enroll_status": "OPT_IN"},
    "text_optimizations": {"enroll_status": "OPT_IN"},
}

# Default optimization set for multi_image: opt into Advantage+ creative + standard
# enhancements so Facebook actively mixes the supplied images/text. The definition
# may override via "optimizations".
MULTI_IMAGE_CREATIVE_FEATURES = {
    "advantage_plus_creative": {"enroll_status": "OPT_IN"},
    "standard_enhancements": {"enroll_status": "OPT_IN"},
}


def _derive_store_vars(store, *, coupon):
    """Build the ${...} substitution map from a store_directory row + promo coupon."""
    name = store["store_name"]
    return {
        "store_name": name,
        "local_url_path": name.lower().replace(" ", "-"),
        "local_utm_parameter": name.lower().replace(" ", "_"),
        "page_id": store.get("facebook_id"),
        "instagram_user_id": store.get("page_backed_instagram_id"),
        "scorpion_id": store.get("scorpion_id"),
        "coupon": coupon,
    }


def _sub(text, mapping):
    """Substitute ${...} only; leave {{...}} macros and bare-$ literals intact."""
    if text is None:
        return None
    return Template(text).safe_substitute(mapping)


def _deep_sub(obj, mapping):
    """Recursively substitute ${...} across a nested dict/list/str structure."""
    if isinstance(obj, str):
        return _sub(obj, mapping)
    if isinstance(obj, list):
        return [_deep_sub(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_sub(v, mapping) for k, v in obj.items()}
    return obj


def _label(placement, asset_type):
    """Deterministic, internally-consistent placement-asset label."""
    return f"placement_asset_{placement}_{asset_type}"


def _build_uniform_link_data(definition, sub_map, image_hashes):
    """One image, single copy → a classic object_story_spec.link_data link ad,
    which carries the required `link` field (a single-image asset_feed_spec does
    not, and Facebook rejects it as 'link field is required'). link_data is
    single-text, so this uses the FIRST body/title/description — for multiple
    copy variations use per_placement (asset_feed_spec)."""
    images = definition["images"]
    if len(images) != 1:
        raise ValueError(f"uniform image_mode expects exactly 1 image, got {len(images)}")
    copy = definition["copy"]
    link = _sub(definition["destination_url"], sub_map)
    data = {
        "link": link,
        "image_hash": image_hashes[images[0]["image_id"]],
        "message": _sub(copy["bodies"][0], sub_map) if copy.get("bodies") else None,
        "name": _sub(copy["titles"][0], sub_map) if copy.get("titles") else None,
        "description": _sub(copy["descriptions"][0], sub_map) if copy.get("descriptions") else None,
        "call_to_action": {"type": definition["cta_type"], "value": {"link": link}},
    }
    return {k: v for k, v in data.items() if v is not None}


def _dof_base_link_data(definition, sub_map, image_hash):
    """Canonical object_story_spec.link_data for a DOF creative. Facebook's
    AdCreative POST (and generatepreviews) require link_data.link even when the
    asset_feed_spec already carries link_urls — without it the POST is rejected
    'The link field is required.' The asset_feed_spec still owns the optimization
    variations; this is the Advantage+ base story spec (link + primary image + first
    copy + CTA)."""
    copy = definition.get("copy", {})
    link = _sub(definition["destination_url"], sub_map)
    data = {
        "link": link,
        "image_hash": image_hash,
        "call_to_action": {"type": definition["cta_type"], "value": {"link": link}},
    }
    if copy.get("bodies"):
        data["message"] = _sub(copy["bodies"][0], sub_map)
    if copy.get("titles"):
        data["name"] = _sub(copy["titles"][0], sub_map)
    if copy.get("descriptions"):
        data["description"] = _sub(copy["descriptions"][0], sub_map)
    return data


def _build_dof_asset_feed_spec(image_hash_list, definition, sub_map):
    """asset_feed_spec with optimization_type DEGREES_OF_FREEDOM: Facebook optimizes
    across the given image(s) AND the copy variations (Advantage+ creative). Shared
    by the uniform multi-copy case (one image) and multi_image (several images). The
    link lives in link_urls (a bare asset_feed_spec without it is rejected as 'link
    field is required'); no asset_customization_rules — the assets apply across all
    placements."""
    copy = definition["copy"]
    spec = {
        "ad_formats": ["AUTOMATIC_FORMAT"],
        "images": [{"hash": h} for h in image_hash_list],
        "link_urls": [{"display_url": "", "website_url": _sub(definition["destination_url"], sub_map)}],
        "call_to_action_types": [definition["cta_type"]],
        "optimization_type": "DEGREES_OF_FREEDOM",
    }
    if copy.get("bodies"):
        spec["bodies"] = [{"text": _sub(t, sub_map)} for t in copy["bodies"]]
    if copy.get("titles"):
        spec["titles"] = [{"text": _sub(t, sub_map)} for t in copy["titles"]]
    if copy.get("descriptions"):
        spec["descriptions"] = [{"text": _sub(t, sub_map)} for t in copy["descriptions"]]
    return spec


def _build_uniform_multi_asset_feed_spec(definition, sub_map, image_hashes):
    """One image, MULTIPLE copy variations → DEGREES_OF_FREEDOM asset_feed_spec."""
    images = definition["images"]
    if len(images) != 1:
        raise ValueError(f"uniform image_mode expects exactly 1 image, got {len(images)}")
    return _build_dof_asset_feed_spec([image_hashes[images[0]["image_id"]]], definition, sub_map)


def _build_multi_image_asset_feed_spec(definition, sub_map, image_hashes):
    """Several images (NOT placement-specific) → DEGREES_OF_FREEDOM asset_feed_spec
    so Facebook optimizes across the images and the copy variations."""
    images = definition["images"]
    if not images:
        raise ValueError("multi_image image_mode expects at least 1 image")
    return _build_dof_asset_feed_spec(
        [image_hashes[img["image_id"]] for img in images], definition, sub_map)


def _build_per_placement_asset_feed_spec(definition, sub_map, image_hashes, age_min, age_max):
    """One image per placement, mapped via asset_customization_rules (fb25go shape).
    age_min/age_max come from the campaign (the ad-set audience), mirrored into each
    customization_spec where Facebook requires an age bound."""
    used = {img["placement"] for img in definition["images"]}
    unknown = used - set(PLACEMENT_SPECS)
    if unknown:
        raise ValueError(f"Unknown placement(s): {sorted(unknown)}")
    placements = [p for p in PLACEMENT_PRIORITY if p in used]

    rules = []
    for priority, placement in enumerate(placements, start=1):
        rules.append({
            "body_label": {"name": _label(placement, "body")},
            "image_label": {"name": _label(placement, "image")},
            "link_url_label": {"name": _label(placement, "link")},
            "title_label": {"name": _label(placement, "title")},
            "customization_spec": {
                "age_min": age_min,
                "age_max": age_max,
                **PLACEMENT_SPECS[placement],
            },
            "priority": priority,
        })

    body_labels = [{"name": _label(p, "body")} for p in placements]
    title_labels = [{"name": _label(p, "title")} for p in placements]
    link_labels = [{"name": _label(p, "link")} for p in placements]

    # PLACEMENT-optimized feeds reference bodies/titles/images/links via the rule
    # labels (so multiple of those DOF-optimize), but descriptions carry no rule
    # label — they're unlabeled globals, and Facebook accepts only ONE (multiple
    # → "Multiple descriptions assets can not be applied to rule"). Use the first;
    # the real fb25go/July payloads carried a single description.
    descriptions = definition["copy"].get("descriptions") or []

    return {
        "ad_formats": ["AUTOMATIC_FORMAT"],
        "asset_customization_rules": rules,
        "bodies": [{"adlabels": body_labels, "text": _sub(t, sub_map)}
                   for t in definition["copy"]["bodies"]],
        "call_to_action_types": [definition["cta_type"]],
        "descriptions": [{"text": _sub(descriptions[0], sub_map)}] if descriptions else [],
        "images": [{"adlabels": [{"name": _label(img["placement"], "image")}],
                    "hash": image_hashes[img["image_id"]]}
                   for img in definition["images"]],
        "link_urls": [{
            "adlabels": link_labels,
            "display_url": "",
            "website_url": _sub(definition["destination_url"], sub_map),
        }],
        "optimization_type": "PLACEMENT",
        "titles": [{"adlabels": title_labels, "text": _sub(t, sub_map)}
                   for t in definition["copy"]["titles"]],
    }


def compile_creative(definition, *, store, image_hashes, presets, coupon, name="",
                     age_min=DEFAULT_AGE_MIN, age_max=DEFAULT_AGE_MAX):
    """Compile a Layer 2 creative definition into a Facebook AdCreative payload.

    definition   — Layer 2 dict. kind == "image"; image_mode in
                   {"uniform", "multi_image", "per_placement"}.
    store        — store_directory row dict (store_name, facebook_id,
                   page_backed_instagram_id, scorpion_id)
    image_hashes — {image_id: facebook_image_hash}
    presets      — {"site_links": <creative_sourcing_spec template>}; only used
                   when definition["site_links_enabled"] is true.
    coupon       — promo-OPTION coupon (promo_1_coupon / promo_2_coupon), as ${coupon}
    name         — the AdCreative's name (the creative template's label); ${...}
                   tokens in it are substituted like any other field.
    age_min/age_max — the campaign's audience age; only used by per_placement (mirrored
                   into each customization_spec). Real audience targeting is the ad set's.
    """
    if definition["kind"] != "image":
        raise ValueError(f"Unsupported creative kind: {definition['kind']!r}")

    sub_map = _derive_store_vars(store, coupon=coupon)

    mode = definition.get("image_mode", "per_placement")
    if mode not in ("uniform", "multi_image", "per_placement"):
        raise ValueError(f"Unknown image_mode: {mode!r}")

    name = _sub(name, sub_map)

    # Drop None keys (e.g. a store with no page-backed IG account) so we don't
    # send nulls Facebook would reject.
    object_story_spec = {k: v for k, v in {
        "page_id": sub_map["page_id"],
        "instagram_user_id": sub_map["instagram_user_id"],
    }.items() if v is not None}
    default_features = MULTI_IMAGE_CREATIVE_FEATURES if mode == "multi_image" else ALL_CREATIVE_FEATURES
    dof_spec = {"creative_features_spec": definition.get("optimizations", default_features)}

    if mode == "multi_image":
        # Several images + copy, optimized across all of them (no placement rules).
        # Build (+validate) the asset_feed_spec first, then the link_data: link_data
        # provides the required canonical link (Advantage+ base spec); the
        # asset_feed_spec carries the image/copy variations.
        asset_feed_spec = _build_multi_image_asset_feed_spec(definition, sub_map, image_hashes)
        object_story_spec["link_data"] = _dof_base_link_data(
            definition, sub_map, image_hashes[definition["images"][0]["image_id"]])
        payload = {
            "asset_feed_spec": asset_feed_spec,
            "degrees_of_freedom_spec": dof_spec,
            "name": name,
            "object_story_spec": object_story_spec,
            "object_type": "SHARE",
            "url_tags": _sub(definition["url_tags"], sub_map),
        }
    elif mode == "uniform":
        copy = definition.get("copy", {})
        multi_copy = max(len(copy.get("bodies") or []),
                         len(copy.get("titles") or []),
                         len(copy.get("descriptions") or [])) > 1
        if multi_copy:
            # Multiple texts on one image → asset_feed_spec (DEGREES_OF_FREEDOM) so
            # Facebook optimizes across them. link_data carries the required canonical
            # link (the asset_feed_spec link_urls alone is rejected 'link required').
            asset_feed_spec = _build_uniform_multi_asset_feed_spec(definition, sub_map, image_hashes)
            object_story_spec["link_data"] = _dof_base_link_data(
                definition, sub_map, image_hashes[definition["images"][0]["image_id"]])
            payload = {
                "asset_feed_spec": asset_feed_spec,
                "degrees_of_freedom_spec": dof_spec,
                "name": name,
                "object_story_spec": object_story_spec,
                "object_type": "SHARE",
                "url_tags": _sub(definition["url_tags"], sub_map),
            }
        else:
            # Single text → a classic link ad; the link lives in object_story_spec.link_data.
            object_story_spec["link_data"] = _build_uniform_link_data(definition, sub_map, image_hashes)
            payload = {
                "object_story_spec": object_story_spec,
                "degrees_of_freedom_spec": dof_spec,
                "name": name,
                "url_tags": _sub(definition["url_tags"], sub_map),
            }
    else:  # per_placement — multi-asset dynamic creative (link via asset_feed_spec.link_urls)
        payload = {
            "asset_feed_spec": _build_per_placement_asset_feed_spec(definition, sub_map, image_hashes, age_min, age_max),
            "degrees_of_freedom_spec": dof_spec,
            "name": name,
            "object_story_spec": object_story_spec,
            "object_type": "SHARE",
            "url_tags": _sub(definition["url_tags"], sub_map),
        }

    # Site links: fixed default block, included only when the admin enabled them.
    if definition.get("site_links_enabled"):
        payload["creative_sourcing_spec"] = _deep_sub(presets["site_links"], sub_map)

    return payload
