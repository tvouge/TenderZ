"""Modèle commun d'un avis, quelle que soit sa source."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .util import cle_dedoublonnage, empreinte


@dataclass
class Avis:
    source: str                     # boamp | ted | manuel
    ref: str                        # identifiant dans la source (idweb, n° TED…)
    titre: str
    url: str
    acheteur: str = ""
    nature: str = ""                # appel_offre | rectificatif | annulation | attribution | pre_information
    type_marche: list[str] = field(default_factory=list)   # TRAVAUX, FOURNITURES, SERVICES
    procedure: str = ""
    departements: list[str] = field(default_factory=list)
    lieu: str = ""
    nuts: list[str] = field(default_factory=list)
    cpv: list[str] = field(default_factory=list)
    descripteurs: list[str] = field(default_factory=list)
    date_publication: datetime | None = None
    date_limite: datetime | None = None
    titulaire: str = ""             # pour les avis d'attribution
    avis_lies: list[str] = field(default_factory=list)      # réf. des avis initiaux (rectificatifs)
    description: str = ""           # texte libre extrait (pour notation et synthèse)
    brut: dict[str, Any] = field(default_factory=dict)      # données source (non stockées en entier)

    @property
    def id(self) -> str:
        return f"{self.source}-{self.ref}".replace("/", "_")

    @property
    def cle_doublon(self) -> str:
        return cle_dedoublonnage(self.titre, self.acheteur, self.date_limite)

    @property
    def version(self) -> str:
        """Change si l'avis est substantiellement modifié."""
        return empreinte(self.titre, self.date_limite, self.cpv, self.description[:2000])

    def vers_document(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("brut", None)
        d["description"] = self.description[:20000]
        d["id"] = self.id
        d["cle_doublon"] = self.cle_doublon
        d["version"] = self.version
        return d
