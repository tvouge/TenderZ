"""Collecte TED (Journal officiel de l'UE) via l'API publique Search v3, sans clé."""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import requests

from ..models import Avis
from ..util import en_liste, parser_date, premier_texte, unique

log = logging.getLogger(__name__)

URL = "https://api.ted.europa.eu/v3/notices/search"

NATURES = {
    "cn": "appel_offre",
    "pin": "pre_information",
    "can": "attribution",
    "corr": "rectificatif",
    "veat": "attribution",
    "mod": "rectificatif",
}

_session = requests.Session()
_session.headers.update({"User-Agent": "veille-ao-adr/1.0", "Content-Type": "application/json"})


def construire_requete(profil: dict, depuis: date) -> str:
    t = profil["ted"]
    cpv = " ".join(t["cpv_requete"])
    return (
        f"buyer-country={t['pays_acheteur']} AND publication-date>={depuis.strftime('%Y%m%d')} "
        f"AND classification-cpv IN ({cpv})"
    )


def lister(profil: dict, depuis: date, max_pages: int = 20) -> tuple[list[dict], list[str]]:
    requete = construire_requete(profil, depuis)
    notices, erreurs = [], []
    for page in range(1, max_pages + 1):
        corps = {
            "query": requete,
            "fields": profil["ted"]["champs"],
            "limit": 100,
            "page": page,
            "paginationMode": "PAGE_NUMBER",
        }
        try:
            data = _post(corps)
        except Exception as e:
            erreurs.append(f"TED (page {page}) : {e}")
            log.error("TED : %s", e)
            break
        lot = data.get("notices") or []
        notices += lot
        total = data.get("totalNoticeCount") or 0
        if not lot or len(notices) >= total:
            break
    log.info("TED : %d avis", len(notices))
    return notices, erreurs


def _post(corps: dict, essais: int = 4) -> dict:
    for i in range(essais):
        try:
            r = _session.post(URL, json=corps, timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                # Erreur de requête (champ inconnu…) : inutile de réessayer
                raise ValueError(f"HTTP {r.status_code} : {r.text[:300]}")
            return r.json()
        except requests.RequestException:
            if i == essais - 1:
                raise
            time.sleep(5 * (i + 1))
    return {}


def _multilingue(v: Any) -> str:
    """Les champs TED sont des dictionnaires par langue : on privilégie le français."""
    if isinstance(v, dict):
        for langue in ("fra", "FRA", "fr", "eng", "ENG", "en"):
            if langue in v:
                return premier_texte(v[langue])
        for val in v.values():
            t = premier_texte(val)
            if t:
                return t
        return ""
    return premier_texte(v)


def _lien(n: dict) -> str:
    liens = n.get("links") or {}
    for format_ in ("html", "htmlDirect", "pdf"):
        bloc = liens.get(format_) or {}
        if isinstance(bloc, dict):
            for langue in ("FRA", "fra", "ENG", "eng"):
                if bloc.get(langue):
                    return bloc[langue]
            if bloc:
                return next(iter(bloc.values()))
    num = n.get("publication-number", "")
    return f"https://ted.europa.eu/fr/notice/-/detail/{num}"


def vers_avis(n: dict) -> Avis:
    num = str(n.get("publication-number"))
    type_ = str(premier_texte(n.get("notice-type"))).lower()
    nature = next((v for k, v in NATURES.items() if type_.startswith(k)), "autre")
    lieux = [str(x) for x in en_liste(n.get("place-of-performance"))]
    nuts = [x for x in lieux if x.upper().startswith("FR") and len(x) > 2]
    deadlines = en_liste(n.get("deadline-receipt-tender-date-lot"))
    return Avis(
        source="ted",
        ref=num,
        titre=_multilingue(n.get("notice-title")) or "(titre non renseigné)",
        url=_lien(n),
        acheteur=_multilingue(n.get("buyer-name")),
        nature=nature,
        nuts=unique(nuts),
        lieu=", ".join(unique(nuts)),
        cpv=unique(str(c) for c in en_liste(n.get("classification-cpv"))),
        date_publication=parser_date(premier_texte(n.get("publication-date"))),
        date_limite=parser_date(deadlines) if deadlines else None,
        description=_multilingue(n.get("description-lot")) if n.get("description-lot") else "",
    )


def est_idf(avis: Avis, prefixe: str = "FR1") -> bool:
    return any(x.upper().startswith(prefixe) for x in avis.nuts)
