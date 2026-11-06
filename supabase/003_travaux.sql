-- Notation v2 : titre court, étiquettes de travaux, budget estimé.
-- À lancer une fois dans Supabase → SQL Editor → Run. Peut être relancé sans risque.
alter table public.aos
  add column if not exists titre_court    text,
  add column if not exists travaux        jsonb not null default '[]'::jsonb,
  add column if not exists montant_estime numeric,
  add column if not exists notation_v     integer;
