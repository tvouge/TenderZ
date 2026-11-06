"""Notation de pertinence ADR, catégorie de réponse et urgence.

La notation est volontairement lisible : chaque point attribué est justifié
dans `score_detail`, affiché sur la fiche de l'AO dans le site.

Version 2 — les critères, par ordre d'importance :
  1. Contenu     : est-ce notre métier ? (CPV + mots-clés, plafonné)
  2. Région      : Île-de-France en priorité, ailleurs possible si très pertinent
  3. Périmètre   : lot que l'on peut tenir seul ; pas plus de la moitié hors atelier
  4. Budget      : 100 000 – 500 000 € idéalement, jusqu'à 1 M€
Les réglages sont dans config/profil_adr.yaml (rubriques « notation » et « travaux »).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .analyse import Analyse, analyser, extraire_montant_texte, titre_court
from .models import Avis
from .util import contient, jours_restants, normaliser

CATEGORIES = ("directe", "groupement", "sous_traitance", "non_pertinente")
NOTATION_VERSION = 2   # à incrémenter quand les règles changent : les AO sont alors renotés


@dataclass
class Notation:
    score: int
    categorie: str
    detail: list[str] = field(default_factory=list)
    idf: bool = False
    montant: float | None = None
    titre_court: str = ""
    travaux: list[dict] = field(default_factory=list)


def _points_cpv(cpvs: list[str], table: dict) -> tuple[int, str | None]:
    meilleur, motif = 0, None
    for code in cpvs:
        for prefixe, pts in table.items():
            if str(code).startswith(str(prefixe)) and pts > meilleur:
                meilleur, motif = pts, f"CPV {code} (+{pts})"
    return meilleur, motif


def _premier_mot(texte: str, mots: list[str]) -> str | None:
    for m in mots:
        if contient(texte, m):
            return m
    return None


def _euros(n: float) -> str:
    return f"{round(n / 1000):,} k€".replace(",", " ") if n < 1_000_000 else f"{n / 1_000_000:.1f} M€".replace(".", ",")


def _points_budget(montant: float | None, cfg: dict) -> tuple[int, str]:
    if montant is None:
        return 0, "Budget non communiqué (0)"
    pts = cfg["points"]
    if montant < cfg["ideal_min"]:
        return pts["bas"], f"Budget {_euros(montant)} : sous {_euros(cfg['ideal_min'])} ({pts['bas']})"
    if montant <= cfg["ideal_max"]:
        return pts["ideal"], f"Budget {_euros(montant)} : dans la cible (+{pts['ideal']})"
    if montant <= cfg["max"]:
        return pts["haut"], f"Budget {_euros(montant)} : au-dessus de la cible, encore jouable (+{pts['haut']})"
    return pts["tres_haut"], f"Budget {_euros(montant)} : au-delà de {_euros(cfg['max'])} ({pts['tres_haut']})"


def _lieu_connu(avis: Avis) -> bool:
    return bool(avis.departements or avis.nuts or avis.lieu)


def noter(avis: Avis, profil: dict, idf: bool) -> Notation:
    detail: list[str] = []
    titre = normaliser(" ".join([avis.titre, *avis.descripteurs]))
    corps = normaliser(avis.description)
    mc = profil["mots_cles"]
    cfg = profil.get("notation", {})
    perim = cfg.get("perimetre", {})

    # 1. Contenu : CPV + mots-clés, plafonné
    metier = 0
    pts, motif = _points_cpv(avis.cpv, profil["cpv"])
    if motif:
        metier += pts
        detail.append(motif)

    for groupe, poids in (("forts", 25), ("moyens", 12), ("contexte", 5)):
        mot = _premier_mot(titre, mc.get(groupe, []))
        if mot:
            metier += poids
            detail.append(f"« {mot} » dans l'objet (+{poids})")
            continue
        # Dans le corps de l'avis, les mots comptent à moitié
        mot = _premier_mot(corps, mc.get(groupe, []))
        if mot and groupe != "contexte":
            metier += poids // 2
            detail.append(f"« {mot} » dans la description (+{poids // 2})")

    plafond = cfg.get("metier_max", 100)
    if metier > plafond:
        detail.append(f"Contenu plafonné à {plafond} (au lieu de {metier})")
        metier = plafond
    score = metier

    neg = _premier_mot(titre, profil.get("mots_negatifs", []))
    if neg:
        score -= 30
        detail.append(f"« {neg} » : hors cœur de métier (−30)")

    # 2. Région
    if idf:
        bonus = profil.get("bonus_idf", 0)
        score += bonus
        detail.append(f"Île-de-France (+{bonus})")
    elif _lieu_connu(avis) and cfg.get("region", {}).get("hors_idf"):
        malus = cfg["region"]["hors_idf"]
        if metier >= cfg["region"].get("exception_metier", 101):
            detail.append("Hors Île-de-France, mais contenu très proche de notre métier : déplacement envisageable (0)")
        else:
            score += malus
            detail.append(f"Hors Île-de-France ({malus})")

    # 3. Périmètre
    a: Analyse = analyser(avis, profil) if "travaux" in profil else Analyse()
    if a.lot_isolable and perim.get("lot_isolable"):
        score += perim["lot_isolable"]
        detail.append(f"Lot métier identifiable (+{perim['lot_isolable']})")
    if a.cotraitance and perim.get("cotraitance"):
        score += perim["cotraitance"]
        detail.append(f"« {a.cotraitance} » : marché global, il faudrait des partenaires ({perim['cotraitance']})")
    # Un marché alloti dont on peut prendre le lot métier seul n'est pas pénalisé
    seul_possible = a.lot_isolable or (a.alloti and a.adr)
    if a.part_hors_metier is not None and len(a.hors_metier) >= 2 and not seul_possible:
        part = round(a.part_hors_metier * 100)
        if a.part_hors_metier > 0.5 and perim.get("hors_metier_majoritaire"):
            score += perim["hors_metier_majoritaire"]
            detail.append(f"Plus de la moitié du périmètre hors métier (~{part} % : {', '.join(a.hors_metier)}) ({perim['hors_metier_majoritaire']})")
        elif a.part_hors_metier > 0.34 and perim.get("hors_metier_important"):
            score += perim["hors_metier_important"]
            detail.append(f"Périmètre en partie hors métier (~{part} % : {', '.join(a.hors_metier)}) ({perim['hors_metier_important']})")

    # 4. Budget
    montant = avis.montant_estime or extraire_montant_texte(avis.description)
    if cfg.get("budget"):
        pts_b, txt_b = _points_budget(montant, cfg["budget"])
        score += pts_b
        detail.append(txt_b)

    score = max(0, min(100, round(score)))
    categorie = categoriser(avis, score, titre, profil)
    return Notation(
        score=score, categorie=categorie, detail=detail, idf=idf,
        montant=montant, titre_court=titre_court(avis.titre), travaux=a.travaux,
    )


def categoriser(avis: Avis, score: int, titre_norm: str, profil: dict) -> str:
    """Première orientation ; la synthèse IA et l'équipe peuvent la corriger."""
    if score < profil["seuils"]["pertinent"]:
        return "non_pertinente"
    if avis.nature == "attribution":
        # Un marché attribué = piste de sous-traitance auprès du titulaire
        return "sous_traitance"
    if _premier_mot(titre_norm, profil.get("indices_groupement", [])):
        return "groupement"
    return "directe"


def urgence(avis: Avis, profil: dict) -> tuple[str, int | None]:
    j = jours_restants(avis.date_limite)
    if j is None:
        return "inconnue", None
    if j < 0:
        return "expiree", j
    if j < profil["urgence"]["critique"]:
        return "critique", j
    if j < profil["urgence"]["urgente"]:
        return "urgente", j
    return "normale", j


ORDRE_CATEGORIE = {c: i for i, c in enumerate(CATEGORIES)}
ORDRE_URGENCE = {"critique": 0, "urgente": 1, "normale": 2, "inconnue": 3, "expiree": 4}
