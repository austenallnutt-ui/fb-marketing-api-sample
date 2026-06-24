"""
Facebook Marketing API integration — code sample for Meta App Review.

This is a SANITIZED, self-contained excerpt of an internal marketing-operations
app. The app is strictly first-party: it manages advertising on a single
owned ad account (FACEBOOK_AD_ACCOUNT_ID) using a system-user token. It does not
access any third-party or end-user data.

This file shows exactly how the app calls the Graph/Marketing API:
  - low-level Graph helpers (POST / GET / UPDATE / DELETE) with error + rate-limit
    handling,
  - the payload builders for Campaigns, Ad Sets, and Ads,
  - image upload to /adimages,
  - AdCreative construction (via facebook_creative_compiler) and preview generation.

DB persistence, idempotency bookkeeping, and the web framework have been removed so
the API usage is easy to read in isolation. All credentials come from environment
variables; nothing account-specific or secret is hardcoded (placeholders are marked
REPLACE_ME).

Env vars:
  FACEBOOK_AD_ACCOUNT_ID        bare numeric ad-account id ('act_' added in code)
  FACEBOOK_ACCESS_TOKEN         system-user token (ads_management + business_management)
  FACEBOOK_PREVIEW_ACCESS_TOKEN read-only (ads_read) token used only for previews
  FACEBOOK_API_VERSION          e.g. 'v24.0'
  FACEBOOK_PIXEL_ID             conversion pixel id (optional)
  FACEBOOK_WRITES_ENABLED       'true' to make real calls; anything else = dry-run
"""

import json
import logging
import os

import requests

from facebook_creative_compiler import OBJECTIVE_OPTIMIZATION_GOAL, compile_creative

logger = logging.getLogger(__name__)

FACEBOOK_REQUEST_TIMEOUT = 30

# Ad-set defaults.
DEFAULT_RADIUS_MILES = 5
DEFAULT_AGE_MIN = 18
DEFAULT_AGE_MAX = 65
AD_SET_BILLING_EVENT = 'IMPRESSIONS'
AD_SET_BID_STRATEGY = 'LOWEST_COST_WITHOUT_CAP'
CONVERSION_CUSTOM_EVENT_STR = 'Scheduler Confirmation'

# Our objective vocabulary → Facebook Campaign objective enum.
OBJECTIVE_CAMPAIGN_OBJECTIVE = {
    'awareness': 'OUTCOME_AWARENESS',
    'leads':     'OUTCOME_LEADS',
    'traffic':   'OUTCOME_TRAFFIC',
}

# Placements previewed in the admin UI.
PREVIEW_AD_FORMATS = ['MOBILE_FEED_STANDARD', 'INSTAGRAM_STANDARD', 'FACEBOOK_STORY_MOBILE']

# Default page used to render previews (a generic stand-in, not a real store).
PREVIEW_PAGE_ID = 'REPLACE_ME_PREVIEW_PAGE_ID'

# Fixed site-links block, injected as creative_sourcing_spec when a creative enables
# site links. URLs carry ${...} tokens resolved per store at compile time; the image
# hashes are account-specific (placeholders here).
SITE_LINK_UTM = ('utm_campaign=localized%20fb&utm_source=facebook&utm_medium=social'
                 '&utm_content=${local_utm_parameter}-site_link&cba_coupon=${coupon}')
SITE_LINKS_SPEC = [
    {'site_link_title': 'CBA ${store_name}',
     'site_link_url': f'https://www.example.com/${{local_url_path}}?{SITE_LINK_UTM}',
     'site_link_image_hash': 'REPLACE_ME_IMAGE_HASH_1'},
    {'site_link_title': 'About Us',
     'site_link_url': f'https://www.example.com/${{local_url_path}}/about-us?{SITE_LINK_UTM}',
     'site_link_image_hash': 'REPLACE_ME_IMAGE_HASH_2'},
    {'site_link_title': 'Our Services',
     'site_link_url': f'https://www.example.com/our-services?l_=${{scorpion_id}}&{SITE_LINK_UTM}',
     'site_link_image_hash': 'REPLACE_ME_IMAGE_HASH_3'},
    {'site_link_title': 'Schedule',
     'site_link_url': f'https://scheduler.example.com/${{local_url_path}}?{SITE_LINK_UTM}',
     'site_link_image_hash': 'REPLACE_ME_IMAGE_HASH_4'},
]
DEFAULT_PRESETS = {'site_links': {'site_links_spec': SITE_LINKS_SPEC}}


class FacebookError(Exception):
    """Raised when a Graph API call fails."""


# ---------------------------------------------------------------------------
# Config accessors — read env at call time so a missing var never breaks import.
# ---------------------------------------------------------------------------
def _graph_base_url():
    version = os.getenv('FACEBOOK_API_VERSION', 'v24.0')
    return f'https://graph.facebook.com/{version}'


def _ad_account_id():
    """The Graph ad-account node, 'act_<id>'. FACEBOOK_AD_ACCOUNT_ID holds the bare
    numeric id; the 'act_' prefix is added here."""
    account_id = os.getenv('FACEBOOK_AD_ACCOUNT_ID')
    if not account_id:
        return None
    return f"act_{str(account_id).strip().removeprefix('act_')}"


def _access_token():
    return os.getenv('FACEBOOK_ACCESS_TOKEN')


def _facebook_writes_enabled():
    """Real Graph writes only when FACEBOOK_WRITES_ENABLED=true; otherwise dry-run
    (no API traffic). Decoupled from environment so going live is always explicit."""
    return os.getenv('FACEBOOK_WRITES_ENABLED', '').strip().lower() == 'true'


# ---------------------------------------------------------------------------
# Low-level Graph API helpers
# ---------------------------------------------------------------------------
def _encode_params(payload):
    """Graph form-encodes scalars and expects nested objects/arrays as JSON strings,
    booleans as 'true'/'false'. None values are dropped."""
    out = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = 'true' if value else 'false'
        elif isinstance(value, (dict, list)):
            out[key] = json.dumps(value)
        else:
            out[key] = value
    return out


def _fake_create_success(edge):
    return {'id': f'DRYRUN_{edge}', 'dryRun': True}


def _error_detail(exc):
    """Surface Facebook's structured error (message/user_msg/subcode/blame_fields/
    fbtrace_id) so failures are actionable. Subcode 33 = object missing / no
    permission — on account-scoped calls that almost always means the ad account is
    misconfigured, not that the specific object is bad, so we spell that out."""
    resp = getattr(exc, 'response', None)
    if resp is None:
        return str(exc)
    try:
        err = resp.json().get('error', {})
    except ValueError:
        return resp.text
    if not err:
        return resp.text

    parts = []
    if err.get('message'):
        parts.append(str(err['message']))
    if err.get('error_user_title'):
        parts.append(f"title={err['error_user_title']}")
    if err.get('error_user_msg'):
        parts.append(f"user_msg={err['error_user_msg']}")
    if err.get('error_subcode'):
        parts.append(f"subcode={err['error_subcode']}")
    if str(err.get('error_subcode')) == '33':
        acct = _ad_account_id()
        if acct and acct in (err.get('message') or ''):
            parts.append(f"HINT: ad account {acct} not found or inaccessible — verify "
                         "FACEBOOK_AD_ACCOUNT_ID and that the access token has permission for it")
        else:
            parts.append("HINT: referenced object not found or the token lacks permission for it")
    error_data = err.get('error_data')
    if isinstance(error_data, dict) and error_data.get('blame_field_specs'):
        parts.append(f"blame_fields={error_data['blame_field_specs']}")
    if err.get('fbtrace_id'):
        parts.append(f"fbtrace_id={err['fbtrace_id']}")
    return '; '.join(parts) or resp.text


def _log_rate_usage(label, response):
    """Log ad-account rate-limit headroom from the response headers so a bulk push's
    usage is visible before it throttles. Best-effort — never raises.

    X-Ad-Account-Usage.acc_id_util_pct = % of the account's hourly call limit used;
    X-Business-Use-Case-Usage carries call_count / total_cputime / total_time (%) +
    estimated_time_to_regain_access (minutes blocked)."""
    try:
        acct = response.headers.get('X-Ad-Account-Usage')
        buc = response.headers.get('X-Business-Use-Case-Usage')
        if not acct and not buc:
            return
        util, tier, blocked = None, None, 0
        if acct:
            a = json.loads(acct)
            util = a.get('acc_id_util_pct')
            tier = a.get('ads_api_access_tier')
        if buc:
            for objs in json.loads(buc).values():
                for o in objs:
                    util = max(util or 0, o.get('call_count') or 0,
                               o.get('total_cputime') or 0, o.get('total_time') or 0)
                    blocked = max(blocked, o.get('estimated_time_to_regain_access') or 0)
        suffix = f' BLOCKED~{blocked}min' if blocked else ''
        msg = f'Facebook rate usage [{label}]: util={util}% tier={tier}{suffix}'
        if blocked or (util is not None and util >= 90):
            logger.warning(msg)
        else:
            logger.info(msg)
    except Exception:
        logger.debug('Failed to parse Facebook rate-usage headers', exc_info=True)


def _graph_post(edge, payload):
    """POST a create to /act_<account>/<edge>. Dry-run short-circuits with no traffic."""
    if not _facebook_writes_enabled():
        logger.info('Facebook dry-run: would POST to %s/%s', _ad_account_id(), edge)
        return _fake_create_success(edge)
    url = f'{_graph_base_url()}/{_ad_account_id()}/{edge}'
    data = _encode_params(payload)
    data['access_token'] = _access_token()
    try:
        response = requests.post(url, data=data, timeout=FACEBOOK_REQUEST_TIMEOUT)
        _log_rate_usage(edge, response)
        response.raise_for_status()
    except requests.RequestException as e:
        raise FacebookError(f'Facebook POST {edge} failed: {_error_detail(e)}') from e
    return response.json()


def _graph_delete(object_id):
    """DELETE a Graph object by id."""
    if not object_id:
        return {'skipped': True, 'reason': 'no external id'}
    if not _facebook_writes_enabled():
        logger.info('Facebook dry-run: would DELETE %s', object_id)
        return {'success': True, 'dryRun': True}
    url = f'{_graph_base_url()}/{object_id}'
    try:
        response = requests.delete(
            url, params={'access_token': _access_token()}, timeout=FACEBOOK_REQUEST_TIMEOUT,
        )
        _log_rate_usage(f'delete {object_id}', response)
        response.raise_for_status()
    except requests.RequestException as e:
        raise FacebookError(f'Facebook DELETE {object_id} failed: {_error_detail(e)}') from e
    return response.json()


def _graph_update(object_id, payload):
    """POST field updates to a Graph object by id (status, budget, creative repoint…)."""
    if not object_id:
        return {'skipped': True, 'reason': 'no external id'}
    if not _facebook_writes_enabled():
        logger.info('Facebook dry-run: would UPDATE %s fields=%s', object_id, sorted(payload))
        return {'success': True, 'dryRun': True}
    url = f'{_graph_base_url()}/{object_id}'
    data = _encode_params(payload)
    data['access_token'] = _access_token()
    try:
        response = requests.post(url, data=data, timeout=FACEBOOK_REQUEST_TIMEOUT)
        _log_rate_usage(f'update {object_id}', response)
        response.raise_for_status()
    except requests.RequestException as e:
        raise FacebookError(f'Facebook UPDATE {object_id} failed: {_error_detail(e)}') from e
    return response.json()


def _graph_get(edge, params):
    """GET an ad-account edge (preview generation). Uses the read-only preview token,
    never the high-privilege system token, since this token is embedded in the
    preview iframe rendered in the admin browser."""
    url = f'{_graph_base_url()}/{_ad_account_id()}/{edge}'
    query = _encode_params(params)
    query['access_token'] = os.getenv('FACEBOOK_PREVIEW_ACCESS_TOKEN')
    try:
        response = requests.get(url, params=query, timeout=FACEBOOK_REQUEST_TIMEOUT)
        _log_rate_usage(edge, response)
        response.raise_for_status()
    except requests.RequestException as e:
        raise FacebookError(f'Facebook GET {edge} failed: {_error_detail(e)}') from e
    return response.json()


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
def _build_campaign_payload(name, objective):
    """Campaign create payload. Created PAUSED. Budgets live at the ad-set level
    (per-store), so this is not a CBO campaign — is_adset_budget_sharing_enabled
    is set False so each ad set keeps its own budget."""
    return {
        'name': name,
        'objective': OBJECTIVE_CAMPAIGN_OBJECTIVE[objective],
        'status': 'PAUSED',
        'special_ad_categories': [],
        'is_adset_budget_sharing_enabled': False,
    }


def _build_targeting(*, latitude, longitude, radius_miles=DEFAULT_RADIUS_MILES,
                     age_min=DEFAULT_AGE_MIN, age_max=DEFAULT_AGE_MAX):
    """Geo + age targeting: a single custom_location (lat/long + radius) for local
    reach; publisher_platforms omitted so Facebook uses automatic placements."""
    return {
        'geo_locations': {
            'custom_locations': [{
                'latitude': float(latitude),
                'longitude': float(longitude),
                'radius': radius_miles,
                'distance_unit': 'mile',
            }],
        },
        'age_min': age_min,
        'age_max': age_max,
    }


def _build_promoted_object(objective):
    """leads optimize for an offsite pixel conversion, so they carry a pixel object
    (when FACEBOOK_PIXEL_ID is set). Other objectives need no promoted_object."""
    if objective != 'leads':
        return None
    pixel_id = os.getenv('FACEBOOK_PIXEL_ID')
    if not pixel_id:
        return None
    return {
        'pixel_id': pixel_id,
        'custom_event_type': 'OTHER',
        'custom_event_str': CONVERSION_CUSTOM_EVENT_STR,
    }


def _build_ad_set_payload(*, name, fb_campaign_id, optimization_goal, lifetime_budget_cents,
                          targeting, end_time, promoted_object=None, start_time=None):
    """Ad Set create payload. Created PAUSED; lifetime_budget is in cents. Facebook
    requires an end_time for lifetime-budget ad sets, so it's mandatory."""
    if not end_time:
        raise FacebookError('end_time is required for a lifetime-budget ad set')
    payload = {
        'name': name,
        'campaign_id': fb_campaign_id,
        'optimization_goal': optimization_goal,
        'billing_event': AD_SET_BILLING_EVENT,
        'bid_strategy': AD_SET_BID_STRATEGY,
        'lifetime_budget': lifetime_budget_cents,
        'targeting': targeting,
        'end_time': end_time,
        'status': 'PAUSED',
    }
    if promoted_object:
        payload['promoted_object'] = promoted_object
    if start_time:
        payload['start_time'] = start_time
    return payload


def _build_ad_payload(name, fb_adset_id, adcreative_id):
    """Ad create payload, referencing the AdCreative by id. PAUSED. Adds conversion
    tracking_specs wired to the pixel when FACEBOOK_PIXEL_ID is set."""
    payload = {
        'name': name,
        'adset_id': fb_adset_id,
        'creative': {'creative_id': adcreative_id},
        'status': 'PAUSED',
    }
    pixel_id = os.getenv('FACEBOOK_PIXEL_ID')
    if pixel_id:
        payload['tracking_specs'] = [
            {'action.type': ['offsite_conversion'], 'fb_pixel': [pixel_id]}
        ]
    return payload


# ---------------------------------------------------------------------------
# Public operations (DB persistence + idempotency bookkeeping removed for clarity)
# ---------------------------------------------------------------------------
def create_campaign(*, name, objective):
    """Create a Facebook Campaign. `objective` is our vocabulary
    ('awareness'|'leads'|'traffic'); it maps to the Facebook objective enum."""
    if objective not in OBJECTIVE_CAMPAIGN_OBJECTIVE:
        raise FacebookError(f'Invalid objective: {objective!r}')
    if not name:
        raise FacebookError('Missing required field: name')
    return _graph_post('campaigns', _build_campaign_payload(name, objective))


def create_ad_set(*, name, fb_campaign_id, objective, lifetime_budget_cents,
                  latitude, longitude, end_time, start_time=None,
                  age_min=DEFAULT_AGE_MIN, age_max=DEFAULT_AGE_MAX):
    """Create a per-store Ad Set under a campaign (geo + age targeting, lifetime
    budget). optimization_goal is derived from the campaign objective."""
    optimization_goal = OBJECTIVE_OPTIMIZATION_GOAL.get(objective)
    if not optimization_goal:
        raise FacebookError(f'Objective {objective!r} has no optimization_goal mapping')
    targeting = _build_targeting(latitude=latitude, longitude=longitude,
                                 age_min=age_min, age_max=age_max)
    return _graph_post('adsets', _build_ad_set_payload(
        name=name, fb_campaign_id=fb_campaign_id, optimization_goal=optimization_goal,
        lifetime_budget_cents=lifetime_budget_cents, targeting=targeting,
        promoted_object=_build_promoted_object(objective),
        start_time=start_time, end_time=end_time,
    ))


def upload_image(image_bytes, content_type='image/jpeg'):
    """Upload image bytes to /adimages and return the Facebook image hash. The hash
    is then referenced when building AdCreatives. /adimages takes the image as a
    multipart FILE (no URL import), so we POST the bytes directly."""
    if not _facebook_writes_enabled():
        logger.info('Facebook dry-run: would upload image (%d bytes)', len(image_bytes))
        return 'DRYRUN_IMAGE_HASH'
    ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif'}.get(content_type, 'jpg')
    url = f'{_graph_base_url()}/{_ad_account_id()}/adimages'
    files = {'filename': (f'image.{ext}', image_bytes, content_type)}
    try:
        response = requests.post(url, files=files, data={'access_token': _access_token()},
                                 timeout=FACEBOOK_REQUEST_TIMEOUT)
        _log_rate_usage('adimages', response)
        response.raise_for_status()
    except requests.RequestException as e:
        raise FacebookError(f'Facebook adimages upload failed: {_error_detail(e)}') from e
    images = response.json().get('images') or {}
    image_hash = next((v.get('hash') for v in images.values() if v.get('hash')), None)
    if not image_hash:
        raise FacebookError(f'adimages returned no hash: {response.json()}')
    return image_hash


def create_ad_creative(definition, *, store, image_hashes, coupon='', name='',
                       age_min=DEFAULT_AGE_MIN, age_max=DEFAULT_AGE_MAX):
    """Compile a creative definition (see facebook_creative_compiler) into an
    AdCreative payload and POST it. Returns the Graph AdCreative response."""
    spec = compile_creative(definition, store=store, image_hashes=image_hashes,
                            presets=DEFAULT_PRESETS, coupon=coupon, name=name,
                            age_min=age_min, age_max=age_max)
    return _graph_post('adcreatives', spec)


def create_ad(*, name, fb_adset_id, fb_adcreative_id):
    """Create an Ad referencing an Ad Set and an AdCreative."""
    return _graph_post('ads', _build_ad_payload(name, fb_adset_id, fb_adcreative_id))


def update_object(object_id, fields):
    """Update fields on any Graph object (pause/resume status, budget, creative repoint)."""
    return _graph_update(object_id, fields)


def delete_object(object_id):
    """Delete any Graph object by id (e.g. an ad or ad set when a store opts out)."""
    return _graph_delete(object_id)


def generate_preview(creative_spec, ad_formats=None):
    """Render Facebook's own ad previews for a compiled creative, per placement,
    via the generatepreviews edge (read-only token)."""
    previews = []
    for fmt in (ad_formats or PREVIEW_AD_FORMATS):
        res = _graph_get('generatepreviews', {'ad_format': fmt, 'creative': creative_spec})
        bodies = [item['body'] for item in res.get('data', []) if item.get('body')]
        previews.append({'ad_format': fmt, 'body': bodies[0] if bodies else None})
    return previews


if __name__ == '__main__':
    # Dry-run demo of a full create flow (no FACEBOOK_WRITES_ENABLED → no API traffic).
    logging.basicConfig(level=logging.INFO)
    campaign = create_campaign(name='June 2026 - $15 Off Oil', objective='leads')
    ad_set = create_ad_set(
        name='June 2026 - 0001 - Example Store', fb_campaign_id=campaign['id'],
        objective='leads', lifetime_budget_cents=15000,
        latitude=30.16, longitude=-95.46, end_time='2026-06-30T23:59:59-0500',
    )
    definition = {
        'kind': 'image', 'image_mode': 'uniform', 'cta_type': 'LEARN_MORE',
        'images': [{'image_id': 1}],
        'copy': {'bodies': ['Get $15 off any oil change at ${store_name}!'],
                 'titles': ['$15 Off Oil'], 'descriptions': []},
        'destination_url': 'https://scheduler.example.com/${local_url_path}',
        'url_tags': 'utm_source=facebook', 'site_links_enabled': False,
    }
    store = {'store_name': 'Example Store', 'facebook_id': PREVIEW_PAGE_ID,
             'page_backed_instagram_id': None, 'scorpion_id': '00000'}
    creative = create_ad_creative(definition, store=store, image_hashes={1: 'DRYRUN_IMAGE_HASH'},
                                  coupon='JUNE15', name='June 2026 - $15 Off Oil')
    create_ad(name='$15 Off Oil - Example Store', fb_adset_id=ad_set['id'],
              fb_adcreative_id=creative['id'])
