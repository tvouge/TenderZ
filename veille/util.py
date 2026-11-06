"""Fonctions utilitaires : normalisation de texte, dates, configuration."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yaml

PARIS = ZoneInfo("Europe/Paris")
RACINE = Path(__file__).resolve().parent.parent


def charger_profil(chemin: str | Path | None = None) -> dict:
    chemin = Path(chemin) if chemin else RACINE / "config" / "profil_adr.yaml"
    with open(chemin, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normaliser(texte: str | None) -> str:
    """Minuscules, sans accents, ligatures dépliées, espaces compactés."""
    if not texte:
        return ""
    t = texte.replace("œ", "oe").replace("Œ", "oe").replace("æ", "ae").replace("’", "'")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    return re.sub(r"\s+", " ", t).strip()


def contient(texte_normalise: str, expression: str) -> bool:
    """Recherche d'une expression entière (frontières de mots)."""
    expr = re.escape(normaliser(expression))
    return re.search(rf"(?<![a-z0-9]){expr}(?![a-z0-9])", texte_normalise) is not None


def en_liste(valeur: Any) -> list:
    if valeur is None or valeur == "":
        return []
    if isinstance(valeur, (list, tuple, set)):
        return [v for v in valeur if v not in (None, "")]
    if isinstance(valeur, str) and valeur.startswith("["):
        try:
            return en_liste(json.loads(valeur))
        except json.JSONDecodeError:
            pass
    return [valeur]


def parser_date(valeur: Any) -> datetime | None:
    """Accepte '2026-11-05', '2026-11-05T12:00:00+01:00', '20261105', '2026-11-05+01:00'."""
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=PARIS)
    if isinstance(valeur, date):
        return datetime(valeur.year, valeur.month, valeur.day, 23, 59, tzinfo=PARIS)
    if isinstance(valeur, list):
        dates = [d for d in (parser_date(v) for v in valeur) if d]
        return min(dates) if dates else None
    s = str(valeur).strip()
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    # Format TED « 2026-11-20+01:00 » (date avec fuseau, sans heure)
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})([+-]\d{2}:\d{2}|Z)", s)
    if m:
        s = f"{m.group(1)}T23:59:00{m.group(2).replace('Z', '+00:00')}"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        if len(s) <= 10:  # date seule → fin de journée
            d = d.replace(hour=23, minute=59)
        d = d.replace(tzinfo=PARIS)
    return d


def maintenant() -> datetime:
    return datetime.now(timezone.utc)


def jours_restants(limite: datetime | None, ref: datetime | None = None) -> int | None:
    if not limite:
        return None
    ref = ref or maintenant()
    delta = limite.astimezone(PARIS).date() - ref.astimezone(PARIS).date()
    return delta.days


def empreinte(*valeurs: Any) -> str:
    """Empreinte stable des champs significatifs, pour détecter une modification."""
    brut = json.dumps(valeurs, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(brut.encode("utf-8")).hexdigest()[:16]


def premier_texte(*valeurs: Any) -> str:
    for v in valeurs:
        for x in en_liste(v):
            if isinstance(x, str) and x.strip():
                return x.strip()
    return ""


def cle_dedoublonnage(titre: str, acheteur: str, limite: datetime | None) -> str:
    """Clé approximative pour rapprocher un même avis publié sur BOAMP et TED."""
    t = re.sub(r"[^a-z0-9]", "", normaliser(titre))[:60]
    a = re.sub(r"[^a-z0-9]", "", normaliser(acheteur))[:30]
    d = limite.astimezone(PARIS).date().isoformat() if limite else ""
    return f"{a}|{t}|{d}"


def unique(seq: Iterable) -> list:
    vus, res = set(), []
    for x in seq:
        if x not in vus:
            vus.add(x)
            res.append(x)
    return res
