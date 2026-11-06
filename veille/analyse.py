"""Lecture du contenu d'un avis : nature des travaux, périmètre, budget, titre court.

Tout est déterministe et lisible (pas d'IA) : le vocabulaire vit dans
config/profil_adr.yaml, rubrique « travaux ».
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Avis
from .util import contient, normaliser

# --- Budget ---------------------------------------------------------------

_MOT_MONTANT = r"(?:montant|valeur|budget|enveloppe|estim\w*|pr[ée]visionnel\w*)"
_NOMBRE = r"(\d{1,3}(?:[\s.]\d{3})+|\d+)(?:[,.](\d{1,2}))?"
_UNITE = r"(k\s?€|m\s?€|millions?(?:\s+d')?\s*(?:€|euros?)?|€|euros?|eur)(?![a-z])"
_RE_MONTANT = re.compile(rf"{_MOT_MONTANT}[^0-9\n]{{0,80}}?{_NOMBRE}\s*{_UNITE}")
_MIN_PLAUSIBLE, _MAX_PLAUSIBLE = 5_000, 2_000_000_000


def extraire_montant_texte(texte: str | None) -> float | None:
    """Premier montant précédé d'un mot du type « montant estimé », en euros HT."""
    t = normaliser(texte)
    for m in _RE_MONTANT.finditer(t):
        entier, decimales, unite = m.group(1), m.group(2), m.group(3)
        valeur = float(re.sub(r"[\s.]", "", entier) + ("." + decimales if decimales else ""))
        if unite.startswith("k"):
            valeur *= 1_000
        elif unite.startswith("m"):
            valeur *= 1_000_000
        if _MIN_PLAUSIBLE <= valeur <= _MAX_PLAUSIBLE:
            return round(valeur)
    return None


# --- Étiquettes de travaux ------------------------------------------------

_TYPES = (("adr", "adr"), ("sous_traite", "ss"), ("hors_metier", "hors"))
_LIMITES = {"adr": 4, "ss": 2, "hors": 2}
_RE_LOT = re.compile(r"\blots?\s*(?:n°|no|numero)?\s*0*(\d+)")


def _trouver(texte_norm: str, groupes: list[dict]) -> list[str]:
    return [g["nom"] for g in groupes if any(contient(texte_norm, m) for m in g["mots"])]


@dataclass
class Analyse:
    adr: list[str] = field(default_factory=list)
    sous_traite: list[str] = field(default_factory=list)
    hors_metier: list[str] = field(default_factory=list)
    alloti: bool = False
    lot_isolable: bool = False
    cotraitance: str | None = None
    part_hors_metier: float | None = None

    @property
    def travaux(self) -> list[dict]:
        """Étiquettes pour la liste : [{"n": "Portes", "t": "adr"}, …]."""
        sorties: list[dict] = []
        for (cle, code) in _TYPES:
            for nom in getattr(self, cle)[: _LIMITES[code]]:
                sorties.append({"n": nom, "t": code})
        return sorties


def analyser(avis: Avis, profil: dict) -> Analyse:
    voc = profil["travaux"]
    titre = normaliser(" ".join([avis.titre, *avis.descripteurs]))
    texte = normaliser(" ".join([avis.titre, *avis.descripteurs, avis.description]))
    a = Analyse(
        adr=_trouver(texte, voc["adr"]),
        sous_traite=_trouver(texte, voc["sous_traite"]),
        hors_metier=_trouver(texte, voc["hors_metier"]),
    )

    # Marché alloti ? Un lot métier est-il identifiable ?
    a.alloti = len(set(_RE_LOT.findall(texte))) >= 2
    mots_adr = [m for g in voc["adr"] for m in g["mots"]]
    lignes = [normaliser(x) for x in re.split(r"[\n;]", f"{avis.titre}\n{avis.description}")]
    a.lot_isolable = any(_RE_LOT.search(l) and any(contient(l, m) for m in mots_adr) for l in lignes)

    for indice in profil.get("indices_cotraitance", []):
        if contient(titre, indice):
            a.cotraitance = indice
            break

    # Part des travaux hors métier : estimation par familles de travaux citées
    # (le volume en euros n'est presque jamais dans l'avis).
    n_adr, n_ss, n_hors = len(a.adr), len(a.sous_traite), len(a.hors_metier)
    total = n_adr + 0.5 * n_ss + n_hors
    if total:
        a.part_hors_metier = round(n_hors / total, 2)
    return a


# --- Titre court ------------------------------------------------------------

_PREFIXES = [
    r"march[ée]s?\s+(?:public\s+)?(?:de\s+|d')?(?:travaux|fournitures?|services?)\s*(?:pour|relatifs?\s+[àa]|:|-)?\s*",
    r"accords?[- ]cadres?\s*(?:[àa]\s+bons?\s+de\s+commande\s*)?(?:pour|relatif\s+[àa]|:|-)?\s*",
    r"appel\s+d['’]offres\s+(?:ouvert\s+|restreint\s+)?(?:pour|:|-)?\s*",
    r"consultation\s+(?:pour|relative\s+[àa]|:|-)?\s*",
    r"travaux\s+(?:de\s+|d['’]|pour\s+|relatifs\s+[àa]\s+)",
    r"lot\s*n?°?\s*\d+\s*[:\-–—]\s*",
]
_ACRONYMES = {"ZAC", "PLV", "ERP", "DCE", "CPAM", "IUT", "IDF", "HLM", "TCE", "RATP", "SNCF", "CHU", "AP-HP", "PMR", "ESAT", "CFA", "EHPAD", "CCAS", "OPH"}


def _casse_phrase(t: str) -> str:
    """TITRE EN MAJUSCULES → Titre en minuscules (sigles conservés)."""
    lettres = [c for c in t if c.isalpha()]
    if not lettres or sum(c.isupper() for c in lettres) / len(lettres) < 0.6:
        return t
    mots = [m if m.strip(".,()") in _ACRONYMES else m.lower() for m in t.split(" ")]
    return " ".join(mots)


def titre_court(titre: str, maxi: int = 90) -> str:
    """Titre allégé de ses formules administratives, en casse de phrase."""
    t = re.sub(r"\s+", " ", titre or "").strip().rstrip(".;:,")
    original = t
    t = _casse_phrase(t)
    lot = _RE_LOT_TITRE.match(t)
    for _ in range(3):  # plusieurs formules peuvent s'enchaîner
        avant = t
        for motif in _PREFIXES:
            t = re.sub(rf"^{motif}", "", t, flags=re.I).strip()
        if t == avant:
            break
    if len(t) < 8:
        t = _casse_phrase(original)
        lot = None
    if lot:
        t = f"Lot {lot.group(1)} : {t[:1].lower() + t[1:]}"
    t = t[:1].upper() + t[1:]
    if len(t) > maxi:
        coupe = t[: maxi - 1].rsplit(" ", 1)[0].rstrip(",;:- ")
        t = coupe + "…"
    return t


_RE_LOT_TITRE = re.compile(r"^lot\s*n?°?\s*(\d+)\s*[:\-–—]", re.I)
