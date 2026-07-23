"""Canonical, dependency-light domain and email normalization."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import tldextract


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_EMAIL_LOCAL = re.compile(r"^[^\s@]{1,64}$")

_extract = tldextract.TLDExtract(
    suffix_list_urls=(),
    include_psl_private_domains=True,
)


@dataclass(frozen=True, slots=True)
class NormalizedDomain:
    hostname: str
    registrable_domain: str
    tld: str
    country_code: str | None


def _hostname_from_input(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("domain is empty")
    if "@" in candidate and "://" not in candidate:
        candidate = candidate.rsplit("@", 1)[-1]
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("domain has no hostname")
    return hostname.rstrip(".").lower()


def normalize_domain(value: str) -> NormalizedDomain:
    hostname = _hostname_from_input(value)
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("domain contains invalid international characters") from exc

    if hostname.startswith("www."):
        hostname = hostname[4:]
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not public publisher domains")

    labels = hostname.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ValueError("domain is not a valid public hostname")
    extracted = _extract(hostname)
    if not extracted.domain or not extracted.suffix:
        raise ValueError("domain does not have a recognized public suffix")
    registrable = f"{extracted.domain}.{extracted.suffix}"
    tld = labels[-1]
    country_code = tld.upper() if len(tld) == 2 and tld.isalpha() else None
    return NormalizedDomain(
        hostname=hostname,
        registrable_domain=registrable,
        tld=tld,
        country_code=country_code,
    )


def normalize_email(value: str) -> str:
    candidate = value.strip().strip("<>").casefold()
    if candidate.count("@") != 1:
        raise ValueError("email must contain one @ character")
    local, domain = candidate.rsplit("@", 1)
    if not _EMAIL_LOCAL.fullmatch(local):
        raise ValueError("email local part is invalid")
    normalized_domain = normalize_domain(domain).hostname
    normalized = f"{local}@{normalized_domain}"
    if len(normalized) > 320:
        raise ValueError("email is too long")
    return normalized
