-- =====================================================================
--  Veille AO — L'Atelier du Renard : schéma Supabase
--
--  À lancer une fois dans Supabase → SQL Editor → New query → Run.
--  Le script peut être relancé sans risque : il ne supprime aucune donnée.
--
--  AVANT DE LANCER : remplacez l'adresse et le nom du premier
--  administrateur tout en bas du fichier (section 7).
-- =====================================================================


-- 1. Membres de l'équipe ------------------------------------------------
-- Seules les adresses listées ici accèdent aux données.

create table if not exists public.membres (
  email      text primary key check (email = lower(email)),
  nom        text not null,
  role       text not null default 'membre' check (role in ('admin', 'membre')),
  ajoute_le  timestamptz not null default now()
);

create or replace function public.email_courant() returns text
language sql stable as $$
  select lower(coalesce(auth.jwt() ->> 'email', ''))
$$;

create or replace function public.est_membre() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.membres where email = public.email_courant())
$$;

create or replace function public.est_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.membres where email = public.email_courant() and role = 'admin')
$$;


-- 2. Appels d'offres ------------------------------------------------------

create table if not exists public.aos (
  id                  text primary key,
  source              text not null check (source in ('boamp', 'ted', 'manuel')),
  ref                 text not null,
  titre               text not null,
  url                 text,
  acheteur            text default '',
  nature              text default '',
  type_marche         text[] default '{}',
  procedure           text default '',
  departements        text[] default '{}',
  lieu                text default '',
  nuts                text[] default '{}',
  cpv                 text[] default '{}',
  descripteurs        text[] default '{}',
  date_publication    timestamptz,
  date_limite         timestamptz,
  titulaire           text default '',
  avis_lies           text[] default '{}',
  description         text default '',
  portail             text,
  cle_doublon         text,
  version             text,
  -- Notation automatique
  score               integer,
  score_detail        text[] default '{}',
  idf                 boolean default false,
  categorie           text not null default 'directe'
                        check (categorie in ('directe', 'groupement', 'sous_traitance', 'non_pertinente')),
  categorie_corrigee  boolean not null default false,
  categorie_ia        text,
  -- Suivi par l'équipe
  statut              text not null default 'a_qualifier'
                        check (statut in ('a_qualifier', 'pris_en_charge', 'en_redaction', 'depose', 'gagne', 'perdu', 'abandonne')),
  responsable         jsonb,
  motif_abandon       text check (motif_abandon in (
                        'delai_insuffisant', 'hors_competence', 'montant_faible', 'montant_eleve',
                        'charge_atelier', 'concurrence', 'exigences_administratives',
                        'visite_impossible', 'localisation', 'marge_insuffisante', 'doublon', 'autre')),
  commentaire_abandon text,
  notes               text,
  montant_depose      numeric,
  documents           jsonb not null default '[]',
  -- Synthèse IA
  synthese            jsonb,
  synthese_statut     text not null default 'aucune'
                        check (synthese_statut in ('aucune', 'demandee', 'en_cours', 'prete', 'erreur')),
  synthese_le         timestamptz,
  synthese_modele     text,
  synthese_pieces     text[] default '{}',
  synthese_erreur     text,
  visite_obligatoire  boolean,
  -- Suivi des publications
  nouveaute           text,
  modifie             boolean not null default false,
  avis_annule         boolean not null default false,
  rectificatifs       text[] not null default '{}',
  autres_sources      text[] not null default '{}',
  detecte_le          timestamptz not null default now(),
  vu_le               timestamptz default now(),
  maj_veille_le       timestamptz not null default now(),
  maj_par             text,
  maj_le              timestamptz,
  pris_en_charge_le   timestamptz,
  decision_le         timestamptz,

  -- Règles métier garanties par la base
  constraint abandon_motive check (
    statut <> 'abandonne'
    or (motif_abandon is not null and length(trim(coalesce(commentaire_abandon, ''))) >= 10)
  ),
  constraint dossier_en_cours_a_un_responsable check (
    statut = 'a_qualifier' or responsable is not null
  )
);

create index if not exists aos_cle_doublon on public.aos (cle_doublon);
create index if not exists aos_synthese_statut on public.aos (synthese_statut) where synthese_statut = 'demandee';
create index if not exists aos_maj_veille on public.aos (maj_veille_le desc);

-- Horodatage et auteur des modifications faites depuis le site, imposés
-- par la base (impossible à falsifier depuis le navigateur).
create or replace function public.aos_controle_equipe() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' then
    new.maj_par := public.email_courant();
    new.maj_le  := now();
    if tg_op = 'UPDATE'
       and new.synthese_statut is distinct from old.synthese_statut
       and new.synthese_statut <> 'demandee' then
      raise exception 'Depuis le site, la synthèse peut seulement être demandée';
    end if;
    if tg_op = 'INSERT' then
      new.detecte_le    := now();
      new.maj_veille_le := now();
    end if;
  end if;
  return new;
end $$;

drop trigger if exists aos_controle_equipe on public.aos;
create trigger aos_controle_equipe before insert or update on public.aos
  for each row execute function public.aos_controle_equipe();


-- 3. Historique -----------------------------------------------------------

create table if not exists public.evenements (
  id       bigint generated always as identity primary key,
  ao_id    text not null references public.aos (id) on delete cascade,
  type     text not null,
  details  jsonb not null default '{}',
  par      text not null,
  date     timestamptz not null default now()
);
create index if not exists evenements_ao on public.evenements (ao_id, date desc);


-- 4. Comptes rendus de collecte (sources lues, obstacles) ----------------

create table if not exists public.collectes (
  id             text primary key,
  debut          timestamptz,
  fin            timestamptz,
  fenetre_jours  integer,
  sources        jsonb,
  resultats      jsonb,
  erreurs        text[]
);


-- 5. Sécurité (Row Level Security) ---------------------------------------
-- La collecte et la synthèse (GitHub Actions) utilisent la clé
-- « service_role », qui n'est pas soumise à ces règles.

alter table public.membres    enable row level security;
alter table public.aos        enable row level security;
alter table public.evenements enable row level security;
alter table public.collectes  enable row level security;

revoke all on public.membres, public.aos, public.evenements, public.collectes from anon, authenticated;

grant select, insert, update, delete on public.membres to authenticated;
grant select, insert, delete on public.aos to authenticated;
-- L'équipe ne peut modifier que les champs de suivi
grant update (statut, responsable, motif_abandon, commentaire_abandon, categorie, categorie_corrigee,
              notes, documents, synthese_statut, montant_depose, pris_en_charge_le, decision_le)
  on public.aos to authenticated;
grant select, insert on public.evenements to authenticated;
grant select on public.collectes to authenticated;

drop policy if exists membres_lecture on public.membres;
create policy membres_lecture on public.membres for select to authenticated using (public.est_membre());
drop policy if exists membres_gestion on public.membres;
create policy membres_gestion on public.membres for all to authenticated
  using (public.est_admin()) with check (public.est_admin());

drop policy if exists aos_lecture on public.aos;
create policy aos_lecture on public.aos for select to authenticated using (public.est_membre());
drop policy if exists aos_suivi on public.aos;
create policy aos_suivi on public.aos for update to authenticated
  using (public.est_membre()) with check (public.est_membre());
-- Ajout manuel d'un AO repéré sur un portail fermé (RATP, SNCF, PLACE…)
drop policy if exists aos_ajout_manuel on public.aos;
create policy aos_ajout_manuel on public.aos for insert to authenticated
  with check (public.est_membre() and source = 'manuel' and id like 'manuel-%');
drop policy if exists aos_suppression on public.aos;
create policy aos_suppression on public.aos for delete to authenticated
  using (public.est_admin() and source = 'manuel');

drop policy if exists evenements_lecture on public.evenements;
create policy evenements_lecture on public.evenements for select to authenticated using (public.est_membre());
drop policy if exists evenements_ajout on public.evenements;
create policy evenements_ajout on public.evenements for insert to authenticated
  with check (public.est_membre() and par = public.email_courant());

drop policy if exists collectes_lecture on public.collectes;
create policy collectes_lecture on public.collectes for select to authenticated using (public.est_membre());


-- 6. Stockage des DCE et temps réel ----------------------------------------

insert into storage.buckets (id, name, public)
values ('dce', 'dce', false)
on conflict (id) do nothing;

drop policy if exists dce_lecture on storage.objects;
create policy dce_lecture on storage.objects for select to authenticated
  using (bucket_id = 'dce' and public.est_membre());
drop policy if exists dce_depot on storage.objects;
create policy dce_depot on storage.objects for insert to authenticated
  with check (bucket_id = 'dce' and public.est_membre());
drop policy if exists dce_suppression on storage.objects;
create policy dce_suppression on storage.objects for delete to authenticated
  using (bucket_id = 'dce' and public.est_membre());

-- Mise à jour en direct du site quand la veille ou un collègue modifie un AO
do $$
declare t text;
begin
  foreach t in array array['aos', 'evenements', 'membres'] loop
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;


-- 6 bis. Paramètres modifiables depuis le site (destinataires du récap)

create table if not exists public.parametres (
  cle     text primary key,
  valeur  jsonb not null,
  maj_par text,
  maj_le  timestamptz not null default now()
);

create or replace function public.parametres_auteur() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' then
    new.maj_par := public.email_courant();
    new.maj_le  := now();
  end if;
  return new;
end $$;

drop trigger if exists parametres_auteur on public.parametres;
create trigger parametres_auteur before insert or update on public.parametres
  for each row execute function public.parametres_auteur();

alter table public.parametres enable row level security;
revoke all on public.parametres from anon, authenticated;
grant select, insert, update on public.parametres to authenticated;

drop policy if exists parametres_lecture on public.parametres;
create policy parametres_lecture on public.parametres for select to authenticated using (public.est_membre());
drop policy if exists parametres_gestion on public.parametres;
create policy parametres_gestion on public.parametres for all to authenticated
  using (public.est_admin()) with check (public.est_admin());

-- Valeur de départ (modifiable ensuite depuis la page Équipe)
insert into public.parametres (cle, valeur)
values ('recap_destinataires', '["lpernelle@latelierdurenard.com"]')
on conflict (cle) do nothing;


-- 7. Premier administrateur ---------------------------------------------------
-- ⚠️ Remplacez l'adresse et le nom ci-dessous (adresse en minuscules).
-- Les autres membres s'ajoutent ensuite depuis le site, page « Équipe ».

insert into public.membres (email, nom, role)
values ('prenom.nom@latelierdurenard.com', 'Prénom Nom', 'admin')
on conflict (email) do nothing;
