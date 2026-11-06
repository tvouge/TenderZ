"""Collecte BOAMP via l'API open data de la DILA (Opendatasoft / Huwise).

Deux passes pour limiter le volume :
1. liste légère des avis (sans le champ DONNEES, volumineux) ;
2. détail (DONNEES : CPV, lots, description) uniquement pour les candidats.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Any, Iterator

import requests

from ..models import Avis
from ..util import en_liste, parser_date, premier_texte, unique

log = logging.getLogger(__name__)

BASE = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp"
CHAMPS_LEGERS = (
    "idweb,id,objet,famille,famille_libelle,code_departement,code_departement_prestation,"
    "dateparution,datelimitereponse,nomacheteur,titulaire,procedure_libelle,type_procedure,"
    "nature,nature_libelle,nature_categorise,descripteur_code,descripteur_libelle,type_marche,"
    "annonce_lie,annonces_anterieures,url_avis,etat"
)

NATURES = {
    "APPEL_OFFRE": "appel_offre",
    "RECTIFICATIF": "rectificatif",
    "ANNULATION": "annulation",
    "ATTRIBUTION": "attribution",
    "RESULTAT": "attribution",
    "PRE-INFORMATION": "pre_information",
    "PRE_INFORMATION": "pre_information",
    "MODIFICATION": "rectificatif",
}

_session = requests.Session()
_session.headers["User-Agent"] = "veille-ao-adr/1.0"


def _get(url: str, params: dict, essais: int = 4) -> Any:
    for i in range(essais):
        try:
            r = _session.get(url, params=params, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if i == essais - 1:
                raise
            attente = 5 * (i + 1)
            log.warning("BOAMP : %s — nouvel essai dans %ss", e, attente)
            time.sleep(attente)


def _export(where: str, select: str) -> list[dict]:
    return _get(f"{BASE}/exports/json", {"where": where, "select": select, "order_by": "dateparution desc"})


def _ou(champ: str, valeurs: list[str]) -> str:
    return "(" + " OR ".join(f'{champ}="{v}"' for v in valeurs) + ")"


def requetes(profil: dict, depuis: date) -> list[tuple[str, str]]:
    """Construit les filtres ODSQL : (libellé, where)."""
    natures = profil["boamp"]["natures"]
    base = f"dateparution >= date'{depuis.isoformat()}' AND {_ou('nature', natures)}"
    deps = profil["departements_idf"]
    idf = f"{base} AND ({_ou('code_departement', deps)} OR {_ou('code_departement_prestation', deps)})"
    mots = " OR ".join(f'"{m}"' for m in profil.get("mots_cles_nationaux", []))
    res = [("Île-de-France", idf)]
    if mots:
        nat_ao = f"dateparution >= date'{depuis.isoformat()}' AND {_ou('nature', ['APPEL_OFFRE', 'RECTIFICATIF'])}"
        res.append(("National (mots-clés spécialisés)", f"{nat_ao} AND ({mots})"))
    return res


def lister(profil: dict, depuis: date) -> tuple[list[dict], list[str]]:
    """Retourne les enregistrements légers (dédoublonnés) et les erreurs rencontrées."""
    vus: dict[str, dict] = {}
    erreurs = []
    for libelle, where in requetes(profil, depuis):
        try:
            lignes = _export(where, CHAMPS_LEGERS)
            log.info("BOAMP %s : %d avis", libelle, len(lignes))
            for l in lignes:
                vus.setdefault(str(l.get("idweb") or l.get("id")), l)
        except Exception as e:  # on continue avec les autres requêtes
            erreurs.append(f"BOAMP ({libelle}) : {e}")
            log.error("BOAMP %s : %s", libelle, e)
    return list(vus.values()), erreurs


def detailler(idwebs: list[str], lot: int = 40) -> dict[str, dict]:
    """Récupère le champ DONNEES (JSON complet de l'avis) par paquets."""
    res: dict[str, dict] = {}
    for i in range(0, len(idwebs), lot):
        paquet = idwebs[i : i + lot]
        where = _ou("idweb", paquet)
        try:
            for l in _export(where, "idweb,donnees"):
                res[str(l.get("idweb"))] = _charger_json(l.get("donnees"))
        except Exception as e:
            log.error("BOAMP détail : %s", e)
    return res


def _charger_json(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


# --- Exploitation du JSON DONNEES ---------------------------------------

_CPV = re.compile(r"\b(\d{8})(?:-\d)?\b")
_CLES_TEXTE = re.compile(r"(objet|description|intitule|titre|name|lot|caracteristique|quantite|prestation|note)", re.I)
_CLES_EXCLUES = re.compile(r"(recours|tribunal|adresse|url|mel|email|tel|fax|siret|codepostal|ville|pays|contact)", re.I)


def _parcourir(noeud: Any, chemin: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(noeud, dict):
        for k, v in noeud.items():
            yield from _parcourir(v, f"{chemin}/{k}")
    elif isinstance(noeud, list):
        for v in noeud:
            yield from _parcourir(v, chemin)
    else:
        yield chemin, noeud


def extraire_cpv(donnees: dict) -> list[str]:
    codes = []
    for chemin, val in _parcourir(donnees):
        if val is None or not re.search(r"cpv|classification", chemin, re.I):
            continue
        codes += _CPV.findall(str(val))
    return unique(codes)


def extraire_texte(donnees: dict, limite: int = 15000) -> str:
    morceaux = []
    for chemin, val in _parcourir(donnees):
        if not isinstance(val, str) or len(val) < 15 or val.startswith("http"):
            continue
        dernier = chemin.rsplit("/", 2)[-2:]
        cle = "/".join(dernier)
        if _CLES_EXCLUES.search(cle) or not _CLES_TEXTE.search(chemin):
            continue
        morceaux.append(val.strip())
    texte = "\n".join(unique(morceaux))
    return texte[:limite]


_CLES_MONTANT = re.compile(r"(val_?estim|valeur|montant|budget)", re.I)
_CLES_MONTANT_EXCLUES = re.compile(r"(devise|monnaie|currency|unite|taux|tva|caution|garantie|avance|penalit|seuil|prime)", re.I)


def extraire_montant(donnees: dict) -> float | None:
    """Plus grande valeur estimée numérique trouvée dans le JSON de l'avis (euros HT)."""
    valeurs = []
    for chemin, val in _parcourir(donnees):
        if val is None or not _CLES_MONTANT.search(chemin) or _CLES_MONTANT_EXCLUES.search(chemin):
            continue
        try:
            v = float(str(val).replace("\u00a0", "").replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        if 5_000 <= v <= 2_000_000_000:
            valeurs.append(v)
    return round(max(valeurs)) if valeurs else None


def extraire_visite(texte: str) -> bool | None:
    t = texte.lower()
    if re.search(r"visite[^.]{0,60}(obligatoire|imp[ée]rative)", t):
        return True
    return None


# --- Conversion vers le modèle commun -----------------------------------

def vers_avis(l: dict, donnees: dict | None = None) -> Avis:
    donnees = donnees or {}
    idweb = str(l.get("idweb") or l.get("id"))
    nature_brute = premier_texte(l.get("nature"), l.get("nature_categorise")).upper()
    nature = NATURES.get(nature_brute, nature_brute.lower() or "autre")
    deps = unique([str(d) for d in en_liste(l.get("code_departement")) + en_liste(l.get("code_departement_prestation"))])
    description = extraire_texte(donnees) if donnees else ""
    return Avis(
        source="boamp",
        ref=idweb,
        titre=premier_texte(l.get("objet")) or "(objet non renseigné)",
        url=l.get("url_avis") or f"https://www.boamp.fr/pages/avis/?q=idweb:{idweb}",
        acheteur=premier_texte(l.get("nomacheteur")),
        nature=nature,
        type_marche=[str(t).upper() for t in en_liste(l.get("type_marche"))],
        procedure=premier_texte(l.get("procedure_libelle"), l.get("type_procedure")),
        departements=deps,
        lieu=", ".join(deps),
        cpv=extraire_cpv(donnees) if donnees else [],
        descripteurs=[str(d) for d in en_liste(l.get("descripteur_libelle"))],
        date_publication=parser_date(l.get("dateparution")),
        date_limite=parser_date(l.get("datelimitereponse")),
        titulaire=premier_texte(l.get("titulaire")),
        avis_lies=[a for a in unique(re.findall(r"\b\d{2}-\d{3,7}\b", f"{l.get('annonce_lie')} {l.get('annonces_anterieures')}")) if a != idweb],
        description=description,
        montant_estime=extraire_montant(donnees) if donnees else None,
        brut={"famille": l.get("famille_libelle"), "etat": l.get("etat")},
    )


def depuis_jours(n: int) -> date:
    return date.today() - timedelta(days=n)
