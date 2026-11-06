"""Extraction du texte des pièces de consultation (DCE) déposées par l'équipe."""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Ordre de lecture : les pièces les plus utiles d'abord
PRIORITES = [
    (r"(?<![a-z])rc(?![a-z])|reglement|règlement", 0),
    (r"cctp|clauses techniques", 1),
    (r"dpgf|bpu|dqe|decomposition|bordereau|quantitatif", 2),
    (r"ccap|clauses administratives", 3),
    (r"(?<![a-z])ae(?![a-z])|acte d.engagement", 4),
    (r"planning|calendrier", 5),
]


@dataclass
class Piece:
    nom: str
    texte: str
    pdf_brut: bytes | None = None   # PDF scanné (sans texte) à envoyer tel quel

    @property
    def priorite(self) -> int:
        n = self.nom.lower().rsplit("/", 1)[-1]
        for motif, rang in PRIORITES:
            if re.search(motif, n):
                return rang
        return 9


def extraire(nom: str, contenu: bytes, profondeur: int = 0) -> list[Piece]:
    ext = nom.lower().rsplit(".", 1)[-1] if "." in nom else ""
    try:
        if ext == "zip" and profondeur < 3:
            pieces = []
            with zipfile.ZipFile(io.BytesIO(contenu)) as z:
                for info in z.infolist():
                    if info.is_dir() or info.file_size > 80_000_000:
                        continue
                    sous_nom = _nom_zip(info)
                    pieces += extraire(sous_nom, z.read(info), profondeur + 1)
            return pieces
        if ext == "pdf":
            return [_pdf(nom, contenu)]
        if ext == "docx":
            return [Piece(nom, _docx(contenu))]
        if ext in ("xlsx", "xlsm"):
            return [Piece(nom, _xlsx(contenu))]
        if ext in ("txt", "csv", "md", "xml", "html", "htm"):
            return [Piece(nom, contenu.decode("utf-8", errors="replace"))]
    except Exception as e:
        log.warning("Lecture impossible de %s : %s", nom, e)
        return [Piece(nom, f"[Fichier illisible : {e}]")]
    return []  # plans DWG, images… ignorés


def _nom_zip(info: zipfile.ZipInfo) -> str:
    nom = info.filename
    if not info.flag_bits & 0x800:  # noms encodés en CP437 par Windows
        try:
            nom = nom.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return nom


def _pdf(nom: str, contenu: bytes) -> Piece:
    from pypdf import PdfReader

    lecteur = PdfReader(io.BytesIO(contenu))
    pages = []
    for i, page in enumerate(lecteur.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
        if i > 300:
            pages.append("[… document tronqué au-delà de 300 pages]")
            break
    texte = "\n".join(pages).strip()
    # Peu de texte pour beaucoup de pages : probablement un scan
    if len(texte) < 200 * max(1, min(len(lecteur.pages), 5)) and len(contenu) < 25_000_000:
        return Piece(nom, texte, pdf_brut=contenu)
    return Piece(nom, texte)


def _docx(contenu: bytes) -> str:
    import docx

    d = docx.Document(io.BytesIO(contenu))
    lignes = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for r in t.rows:
            cellules = [c.text.strip() for c in r.cells]
            if any(cellules):
                lignes.append(" | ".join(cellules))
    return "\n".join(lignes)


def _xlsx(contenu: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(contenu), read_only=True, data_only=True)
    lignes = []
    for ws in wb.worksheets:
        lignes.append(f"## Onglet {ws.title}")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            vals = ["" if v is None else str(v) for v in row]
            if any(vals):
                lignes.append(" | ".join(vals).rstrip(" |"))
            if i > 3000:
                lignes.append("[… onglet tronqué]")
                break
    return "\n".join(lignes)


def assembler(pieces: list[Piece], budget: int = 350_000) -> tuple[str, list[Piece], list[str]]:
    """Concatène les textes dans l'ordre de priorité, dans la limite du budget (caractères).

    Retourne : texte, PDF scannés à joindre (3 max), liste des pièces utilisées.
    """
    pieces = sorted(pieces, key=lambda p: (p.priorite, p.nom))
    morceaux, utilisees, scans = [], [], []
    reste = budget
    for p in pieces:
        if p.pdf_brut is not None and len(scans) < 3:
            scans.append(p)
            utilisees.append(f"{p.nom} (scan)")
            continue
        if not p.texte.strip() or reste <= 0:
            continue
        extrait = p.texte[: max(reste, 0)]
        tronque = len(extrait) < len(p.texte)
        morceaux.append(f"\n\n===== PIÈCE : {p.nom}{' (tronquée)' if tronque else ''} =====\n{extrait}")
        utilisees.append(p.nom + (" (tronquée)" if tronque else ""))
        reste -= len(extrait)
    return "".join(morceaux), scans, utilisees
