"""Mémoire de la veille : Supabase en production, fichier JSON en local.

Tables Supabase (voir supabase/schema.sql) :
  aos          une ligne par appel d'offres
  evenements   historique (détection, modification, prise en charge…)
  collectes    compte rendu de chaque exécution (sources, erreurs)
Stockage : seau privé « dce » pour les pièces déposées par l'équipe.

La collecte utilise la clé « service_role », qui contourne les règles de
sécurité : elle ne doit jamais quitter les secrets GitHub.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .util import maintenant


class Store:
    def get(self, id_: str) -> dict | None: ...
    def creer(self, id_: str, data: dict) -> None: ...
    def maj(self, id_: str, data: dict) -> None: ...
    def evenement(self, id_: str, type_: str, details: dict | None = None, par: str = "veille") -> None: ...
    def par_cle_doublon(self, cle: str) -> dict | None: ...
    def synthese_demandee(self, limite: int = 10) -> list[dict]: ...
    def detectes_depuis(self, depuis: datetime) -> list[dict]: ...
    def enregistrer_collecte(self, id_: str, data: dict) -> None: ...
    def telecharger(self, chemin: str) -> bytes: ...
    def parametre(self, cle: str) -> Any: ...


def _json(o: Any) -> str:
    """Sérialise en JSON compatible Postgres (dates ISO, sans caractère nul)."""
    def defaut(x):
        if isinstance(x, datetime):
            return x.isoformat()
        raise TypeError(type(x))
    return json.dumps(o, default=defaut, ensure_ascii=False).replace("\\u0000", "")


# --- Supabase -------------------------------------------------------------

class SupabaseStore(Store):
    def __init__(self, url: str | None = None, cle: str | None = None) -> None:
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        cle = cle or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        manquants = [n for n, v in (("SUPABASE_URL", self.url), ("SUPABASE_SERVICE_ROLE_KEY", cle)) if not v]
        if manquants:
            raise RuntimeError(
                f"Réglage GitHub manquant : {', '.join(manquants)}. À créer dans Settings → Secrets and variables → "
                "Actions (section « Repository », pas « Environment »)."
            )
        if not self.url.startswith("https://"):
            raise RuntimeError(f"SUPABASE_URL doit commencer par https:// (valeur reçue : {self.url[:40]!r})")
        self.rest_base = f"{self.url}/rest/v1"
        self.s = requests.Session()
        self.s.headers.update({"apikey": cle, "Content-Type": "application/json"})
        # Anciennes clés (JWT « service_role ») : à passer aussi en Authorization.
        # Nouvelles clés « sb_secret_… » : l'en-tête apikey suffit.
        if not cle.startswith("sb_"):
            self.s.headers["Authorization"] = f"Bearer {cle}"

    def _rest(self, methode: str, table: str, params: str = "", corps: Any = None, prefer: str = "") -> Any:
        r = self.s.request(
            methode, f"{self.rest_base}/{table}{'?' + params if params else ''}",
            data=_json(corps) if corps is not None else None,
            headers={"Prefer": prefer} if prefer else None, timeout=60,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {methode} {table} : HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def get(self, id_):
        lignes = self._rest("GET", "aos", f"id=eq.{quote(id_)}&select=*")
        return lignes[0] if lignes else None

    def creer(self, id_, data):
        self._rest("POST", "aos", corps={**data, "id": id_}, prefer="return=minimal")

    def maj(self, id_, data):
        self._rest("PATCH", "aos", f"id=eq.{quote(id_)}", corps=data, prefer="return=minimal")

    def evenement(self, id_, type_, details=None, par="veille"):
        self._rest("POST", "evenements", corps={"ao_id": id_, "type": type_, "details": details or {}, "par": par},
                   prefer="return=minimal")

    def par_cle_doublon(self, cle):
        lignes = self._rest("GET", "aos", f"cle_doublon=eq.{quote(cle, safe='')}&select=*&limit=1")
        return lignes[0] if lignes else None

    def synthese_demandee(self, limite=10):
        return self._rest("GET", "aos", f"synthese_statut=eq.demandee&select=*&limit={limite}")

    def detectes_depuis(self, depuis):
        return self._rest("GET", "aos", f"maj_veille_le=gte.{quote(depuis.isoformat())}&select=*&limit=2000")

    def enregistrer_collecte(self, id_, data):
        self._rest("POST", "collectes", corps={**data, "id": id_},
                   prefer="resolution=merge-duplicates,return=minimal")

    def parametre(self, cle):
        try:
            lignes = self._rest("GET", "parametres", f"cle=eq.{quote(cle)}&select=valeur")
        except Exception:
            return None  # table absente (migration 002 non lancée) ou API injoignable
        return lignes[0]["valeur"] if lignes else None

    def telecharger(self, chemin):
        r = self.s.get(f"{self.url}/storage/v1/object/dce/{quote(chemin)}", timeout=300)
        if r.status_code >= 400:
            raise RuntimeError(f"Stockage : HTTP {r.status_code} {r.text[:200]}")
        return r.content


# --- Fichier local (essais, --local) ----------------------------------------

class FichierStore(Store):
    def __init__(self, chemin: str | Path = "memoire_locale.json") -> None:
        self.chemin = Path(chemin)
        self.data: dict[str, Any] = {"aos": {}, "evenements": {}, "collectes": {}}
        if self.chemin.exists():
            self.data = json.loads(self.chemin.read_text(encoding="utf-8"), object_hook=_dates)

    def _sauver(self):
        self.chemin.write_text(json.dumps(self.data, default=_ser, ensure_ascii=False, indent=1), encoding="utf-8")

    def get(self, id_):
        return self.data["aos"].get(id_)

    def creer(self, id_, data):
        self.data["aos"][id_] = {**data, "id": id_}
        self._sauver()

    def maj(self, id_, data):
        self.data["aos"].setdefault(id_, {}).update(data)
        self._sauver()

    def evenement(self, id_, type_, details=None, par="veille"):
        self.data["evenements"].setdefault(id_, []).append(
            {"type": type_, "details": details or {}, "par": par, "date": maintenant()}
        )
        self._sauver()

    def par_cle_doublon(self, cle):
        return next((a for a in self.data["aos"].values() if a.get("cle_doublon") == cle), None)

    def synthese_demandee(self, limite=10):
        return [a for a in self.data["aos"].values() if a.get("synthese_statut") == "demandee"][:limite]

    def detectes_depuis(self, depuis):
        return [a for a in self.data["aos"].values() if (a.get("maj_veille_le") or depuis) >= depuis]

    def enregistrer_collecte(self, id_, data):
        self.data["collectes"][id_] = data
        self._sauver()

    def telecharger(self, chemin):
        return Path(chemin).read_bytes()

    def parametre(self, cle):
        return self.data.get("parametres", {}).get(cle)


def _ser(o):
    if isinstance(o, datetime):
        return {"__dt__": o.isoformat()}
    raise TypeError(type(o))


def _dates(d):
    if "__dt__" in d:
        return datetime.fromisoformat(d["__dt__"])
    return d


def ouvrir(local: bool = False, chemin: str | None = None) -> Store:
    return FichierStore(chemin or "memoire_locale.json") if local else SupabaseStore()
