"""Ongoing Plex API session, ported from Fetch Log Data/collector/plex.py.

Cookies needed for API calls: plex-customercode, plex-languageculturecode,
plex-auth-prod - NOT apt.uid/apt.sid or the Plex-Cookie-Verification-Token
header seen in a raw browser network-tab capture; the working sibling
integration this was ported from doesn't send those and doesn't need to.

Differs from the sibling version in one way: that one assumes
secret/infos.txt already holds a valid session (populated by a previous
manual run) and applies it at import time. This project starts with no
session yet, so _ensure_session() bootstraps one lazily on first real
call instead - loading it if secret/plex_infos.txt already exists, or
logging in fresh if not.
"""
import os

import requests

from . import config
from .login import load_credentials, login_and_get_credentials, renew_credentials, save_credentials

session = requests.Session()
_login_secrets = None
_secrets = None


def _apply_cookies(creds):
    session.cookies.update({
        "plex-customercode": config.CUSTOMER_CODE,
        "plex-languageculturecode": "en-US",
        "plex-auth-prod": creds["AUTH_PROD"],
    })


def _load_login_secrets():
    global _login_secrets
    if _login_secrets is None:
        if not os.path.exists(config.LOGIN_SECRETS_PATH):
            raise FileNotFoundError(
                f"Missing {config.LOGIN_SECRETS_PATH}. Create it with:\n"
                "  username=<plex username>\n"
                "  password=<plex password>\n"
                "  company_code=<plex company code>"
            )
        _login_secrets = load_credentials(config.LOGIN_SECRETS_PATH)
    return _login_secrets


def _fresh_login():
    login_secrets = _load_login_secrets()
    creds = login_and_get_credentials(
        username=login_secrets["username"],
        password=login_secrets["password"],
        company_code=login_secrets["company_code"],
    )
    save_credentials(config.SESSION_SECRETS_PATH, creds)
    return creds


def _ensure_session():
    global _secrets
    if _secrets is not None:
        return
    if os.path.exists(config.SESSION_SECRETS_PATH):
        _secrets = load_credentials(config.SESSION_SECRETS_PATH)
    else:
        _secrets = _fresh_login()
    _apply_cookies(_secrets)


def _reauth():
    global _secrets
    login_secrets = _load_login_secrets()
    _secrets = renew_credentials(
        secrets_path=config.SESSION_SECRETS_PATH,
        username=login_secrets["username"],
        password=login_secrets["password"],
        company_code=login_secrets["company_code"],
    )
    _apply_cookies(_secrets)


def _post(url, params, json, timeout=15):
    """POST with a single retry: re-login once if the session has expired (401/403/419)."""
    resp = session.post(url, params=params, json=json, timeout=timeout)

    if resp.status_code in (401, 403, 419):
        _reauth()
        resp = session.post(url, params={**params, "__asid": _secrets["ASID"]}, json=json, timeout=timeout)

    if resp.status_code in (401, 403, 419):
        raise PermissionError("Session expired and re-login failed")

    resp.raise_for_status()
    return resp


def search_workcenter_logs(begin_date: str, end_date: str):
    """begin_date/end_date: ISO datetime strings, e.g. '2026-07-28T05:00:00.000Z'."""
    _ensure_session()
    resp = _post(
        "https://cloud.plex.com/ProductionTracking/WorkcenterLog/SearchWorkcenterLogs",
        params={
            "__asid": _secrets["ASID"],
            "limit": "true",
            "sourceActionKey": config.SEARCH_WORKCENTER_LOGS_SOURCE_ACTION_KEY,
        },
        json={
            "ChronologicalSort": False,
            "SelectedWorkcenterAllRows": config.WORKCENTERS,
            "RequestWorkcenterKey": 0,
            "GroupByShift": False,
            "BeginDate": begin_date,
            "EndDate": end_date,
            "DateFrom": begin_date,
            "DateTo": end_date,
            "PlexusCustomerNo": 0,
            "UnreviewedOnly": 0,
            "Workcenter": config.WORKCENTER_KEYS_CSV,
        },
    )
    return resp.json().get("Data")


def search_current_clocked_in(workcenter_key: int):
    """Only takes one workcenter at a time - the Plex UI this was captured
    from doesn't offer a multi-select for this report either, so the two
    Granco workcenter keys (58083, 58079) get queried separately and
    merged by the caller."""
    _ensure_session()
    resp = _post(
        "https://cloud.plex.com/HumanResources/ClockinMaintenance/SearchCurrentClockedInUsers",
        params={
            "__asid": _secrets["ASID"],
            "limit": "true",
            "sourceActionKey": config.SEARCH_CURRENT_CLOCKED_IN_SOURCE_ACTION_KEY,
        },
        json={
            "WorkcenterKey": str(workcenter_key),
            "DirectOnly": False,
            "ReportType": "Currently Clocked In",
        },
    )
    return resp.json().get("Data")
