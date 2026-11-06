# Tenders — veille appels d'offres de L'Atelier du Renard

Outil de veille et de suivi des appels d'offres :

1. **Collecte et mémoire** — chaque jour ouvré, les avis BOAMP et TED sont relus sur 21 jours glissants,
   dédoublonnés (y compris entre BOAMP et TED) et enregistrés. Rectificatifs, reports de date et
   annulations sont rattachés à l'avis d'origine et signalés.
2. **Classement** — chaque avis reçoit un score de pertinence ADR expliqué (codes CPV, mots-clés métier,
   Île-de-France…), une orientation (réponse directe, groupement, sous-traitance, peu pertinent) et une
   urgence (critique moins de 10 jours, urgent moins de 21 jours).
3. **Synthèse IA** — Claude rédige une fiche de décision : lots, visite, critères, pièces à fournir,
   risques, questions à poser et fourchette de coût estimée. Elle porte sur l'avis seul, ou sur le
   dossier de consultation (DCE) complet une fois déposé par l'équipe.
4. **Suivi d'équipe** — un site réservé à l'équipe permet de prendre en charge un dossier, de changer
   son statut, de déposer le DCE et de **ne pas poursuivre en donnant obligatoirement un motif et une
   explication**. Le bilan montre pourquoi les dossiers sont abandonnés.

Un récapitulatif part par mail chaque lundi : sources vérifiées, obstacles rencontrés, nouveaux AO
classés par type de réponse puis par urgence.

```
GitHub Actions ──(collecte quotidienne)──► Supabase (base + DCE) ◄──► Site de l'équipe (Netlify)
      │                                         ▲
      └──(synthèses toutes les 30 min)──────────┘
                     │
                  Claude API
```

## Ce qui est couvert, et ce qui ne l'est pas

| Source | Couverture |
| --- | --- |
| BOAMP (tous acheteurs publics français) | Automatique : Île-de-France tous métiers + France entière pour les mots-clés spécialisés |
| TED (avis européens) | Automatique : codes CPV menuiserie / agencement / mobilier, acheteurs français |
| PLACE, Maximilien, RATP, SNCF, ADP, Viparis, GL events, FFT, bailleurs… | **Manuelle** : ces portails exigent un compte. L'équipe ajoute l'AO dans le site (« + Ajouter un AO ») ; il est ensuite suivi comme les autres |

Le BOAMP reprend la quasi-totalité des avis publics des acheteurs franciliens (Ville de Paris, Région,
départements, bailleurs, universités, musées…). Les consultations privées et celles sur invitation ne
sont pas publiées : elles passent par l'ajout manuel. Pour les portails sur compte, le plus efficace est
de créer des **alertes mail** sur chacun d'eux.

---

## Installation (environ 20 minutes, une seule fois)

### 1. Supabase

1. Créez un projet Supabase (région Europe, par exemple Paris `eu-west-3`).
2. **SQL Editor → New query** : collez `supabase/schema.sql`, remplacez l'adresse et le nom du premier
   administrateur tout en bas (section 7), puis cliquez sur **Run**. *(Si vous avez lancé une version
   antérieure de `schema.sql`, lancez aussi `supabase/002_parametres.sql`.)* Le script crée les tables, les
   règles de sécurité, l'espace de stockage `dce` et le temps réel. Il peut être relancé sans risque.
3. **Comptes : vous gardez la main.** Les accès sont créés par un administrateur, aucun mail n'est envoyé et personne
   ne peut s'inscrire seul :
   - **Authentication → Sign In / Providers** : laissez *Email* activé, mais **désactivez « Allow new users to sign up »** ;
   - **Authentication → Users → Add user → Create new user** : adresse, mot de passe provisoire, cochez
     **Auto Confirm User**. Faites-le d'abord pour vous (même adresse qu'à l'étape 2).
4. Chaque personne peut ensuite changer son mot de passe depuis le site (clic sur son nom en haut à droite).
   Mot de passe oublié : un administrateur le réinitialise dans **Authentication → Users**.
5. **Project Settings → API Keys** : notez l'URL du projet, la clé **publishable** (ou *anon*) et la clé
   **secret** (ou *service_role*).
   - L'URL et la clé publishable vont dans `web/config.js` : elles sont publiques par conception.
   - La clé secret va **uniquement** dans les secrets GitHub. Elle donne un accès complet : ne la
     mettez jamais dans le code ni dans le site.

Limite du forfait gratuit : 50 Mo par fichier déposé. Les DCE plus lourds se déposent en plusieurs
morceaux, sauf si le projet passe sur un forfait payant.

### 2. Secrets et variables GitHub

Dépôt → *Settings → Secrets and variables → Actions*.

**Secrets**

| Nom | Contenu |
| --- | --- |
| `SUPABASE_SERVICE_ROLE_KEY` | Clé *secret* (ou *service_role*) de Supabase |
| `ANTHROPIC_API_KEY` | Clé API Anthropic (<https://console.anthropic.com>) |
| `SMTP_USER` | Adresse Gmail qui envoie le récapitulatif |
| `SMTP_PASSWORD` | Mot de passe d'application de ce compte |

**Variables**

| Nom | Exemple |
| --- | --- |
| `SUPABASE_URL` | `https://abcdefgh.supabase.co` |
| `RECAP_DESTINATAIRES` | facultatif : liste de secours si aucun destinataire n'est réglé dans le site (page **Équipe**) |
| `RECAP_EXPEDITEUR` | `Tenders ADR <veille@…>` (facultatif) |
| `CLAUDE_MODEL` | facultatif, `claude-sonnet-5-5` par défaut |
| `SMTP_HOST` / `SMTP_PORT` | facultatif, `smtp.gmail.com` / `465` par défaut |

Indiquez aussi l'adresse du site dans `config/profil_adr.yaml` (`recap.url_site`).

### 3. Site (Netlify)

Netlify → *Add new project → Import from Git* → choisissez ce dépôt. Aucun réglage : `netlify.toml`
publie le dossier `web/` sans compilation. Chaque modification poussée sur `main` est mise en ligne.

### 4. Bouton « Lancer une collecte » dans le site (facultatif)

Le site peut lancer la collecte et les synthèses à la demande (administrateurs uniquement), via une petite
fonction Supabase qui garde le jeton GitHub côté serveur.

1. **Jeton GitHub** : <https://github.com/settings/personal-access-tokens/new> → *Fine-grained token* ;
   *Repository access* : **Only select repositories** → ce dépôt ; *Permissions → Repository permissions →
   Actions* : **Read and write**. Choisissez une expiration (par exemple 1 an) et notez la date pour le renouveler.
2. **Supabase → Edge Functions → Deploy a new function → Via Editor** : nommez-la exactement `lancer-collecte`,
   collez le contenu de `supabase/functions/lancer-collecte/index.ts`, puis **Deploy** (laissez « Verify JWT » activé).
3. **Supabase → Edge Functions → Secrets** : ajoutez `GITHUB_TOKEN` (le jeton) et `GITHUB_REPO`
   (`tvouge/TenderZ`).

Les boutons apparaissent dans **Bilan** (« Lancer maintenant », avec le suivi des exécutions) et en haut de la
liste des AO. Une fois l'exécution terminée, la liste se recharge seule.

### 4. Mise en route

1. *Actions → Collecte des appels d'offres → Run workflow* (cochez « récapitulatif » pour recevoir
   le premier mail).
2. Ouvrez le site et connectez-vous avec l'adresse et le mot de passe créés à l'étape 1.3.
3. Pour chaque collègue : créez son compte dans **Authentication → Users**, puis ajoutez-le dans la page **Équipe**
   du site. Réglez enfin les destinataires du récapitulatif et les prix dans **Réglages**.
   Les AO apparaissent dans « À qualifier ».

## Utilisation au quotidien

- **À qualifier** : file des nouveaux AO pertinents, classés par type de réponse puis par urgence.
  Un AO à qualifier dont la date limite est passée sort automatiquement de la file.
- **Je prends en charge** : le dossier vous est attribué et passe dans « En cours ».
- **Ne pas poursuivre…** : motif obligatoire choisi dans une liste, plus une explication d'au moins
  10 caractères. Cette règle est appliquée par la base elle-même : il est impossible de la contourner.
- **DCE** : téléchargez le dossier sur la plateforme de l'acheteur, déposez le ZIP sur la fiche, puis
  cliquez sur **Lancer la synthèse**. Elle est prête en 30 minutes au plus, en journée ouvrée.
- **Type de réponse** : si l'orientation automatique est fausse, corrigez-la. La correction n'est
  plus écrasée par la veille.
- **Bilan** : volumes, taux de réussite, motifs d'abandon, compte rendu des dernières collectes
  (sources lues et obstacles).

## Ajuster la veille

Tout se règle dans des fichiers, sans programmation :

- `config/profil_adr.yaml` : mots-clés, codes CPV et leurs points, mots excluants, départements,
  seuils (score minimal, synthèse automatique, urgence), jour du récapitulatif.
- `config/referentiel_prix.yaml` : **à compléter** avec les prix pratiqués par ADR (au mètre linéaire,
  au m², à l'unité). Sans lui, les estimations reposent sur le montant de l'avis ou sur des ordres de
  grandeur, et sont marquées « fiabilité faible ».

Chaque modification poussée sur GitHub s'applique à la collecte suivante.

## Coûts indicatifs

| Poste | Ordre de grandeur |
| --- | --- |
| GitHub Actions (dépôt privé) | Inclus dans les 2 000 minutes gratuites par mois |
| Supabase | Gratuit (forfait Free) ; environ 10 $ par mois si le projet est dans une organisation payante |
| Netlify | Gratuit |
| Claude : synthèse de l'avis seul | Quelques centimes |
| Claude : synthèse d'un DCE complet | Environ 0,30 € à 2 € selon le volume des pièces |

Au-dessus du seuil `synthese_auto` (65 par défaut), la synthèse de l'avis seul est lancée
automatiquement. Mettez ce seuil à 0 pour la désactiver.

## Développement

```bash
pip install -r requirements.txt
python -m pytest -q                       # tests
python -m veille.collect --local --sans-mail   # essai réel, mémoire dans memoire_locale.json
```

Structure :

```
config/          profil de veille et référentiel de prix
veille/
  sources/       boamp.py (API DILA), ted.py (API TED v3)
  scoring.py     score, type de réponse, urgence
  collect.py     collecte, dédoublonnage, rectificatifs, mémoire
  synthesis.py   synthèse Claude (avis + DCE)
  documents.py   lecture des PDF, Word, Excel, ZIP
  digest.py      récapitulatif par mail
  store.py       Supabase (ou fichier local pour les essais)
web/             site de l'équipe (HTML/JS, sans compilation)
supabase/        schema.sql : tables, sécurité, règles métier (motif d'abandon obligatoire…)
```
