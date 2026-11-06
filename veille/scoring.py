"""Notation de pertinence ADR, catégorie de réponse et urgence.

La notation est volontairement lisible : chaque point attribué est justifié
dans `score_detail`, affiché sur la fiche de l'AO dans le site.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Avis
from .util import contient, jours_restants, normaliser

CATEGORIES = ("directe", "groupement", "sous_traitance", "non_pertinente")


@dataclass
class Notation:
    score: int
    categorie: str
    detail: list[str] = field(default_factory=list)
    idf: bool = False


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


def noter(avis: Avis, profil: dict, idf: bool) -> Notation:
    detail: list[str] = []
    titre = normaliser(" ".join([avis.titre, *avis.descripteurs]))
    corps = normaliser(avis.description)
    mc = profil["mots_cles"]
    score = 0

    pts, motif = _points_cpv(avis.cpv, profil["cpv"])
    if motif:
        score += pts
        detail.append(motif)

    for groupe, poids in (("forts", 25), ("moyens", 12), ("contexte", 5)):
        mot = _premier_mot(titre, mc.get(groupe, []))
        if mot:
            score += poids
            detail.append(f"« {mot} » dans l'objet (+{poids})")
            continue
        # Dans le corps de l'avis, les mots comptent à moitié
        mot = _premier_mot(corps, mc.get(groupe, []))
        if mot and groupe != "contexte":
            score += poids // 2
            detail.append(f"« {mot} » dans la description (+{poids // 2})")

    neg = _premier_mot(titre, profil.get("mots_negatifs", []))
    if neg:
        score -= 30
        detail.append(f"« {neg} » : hors cœur de métier (−30)")

    if idf:
        score += profil.get("bonus_idf", 0)
        detail.append(f"Île-de-France (+{profil.get('bonus_idf', 0)})")

    score = max(0, min(100, score))
    categorie = categoriser(avis, score, titre, profil)
    return Notation(score=score, categorie=categorie, detail=detail, idf=idf)


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
