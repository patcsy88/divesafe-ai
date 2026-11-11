-- ADR 0024 A7 hand run ONLY: synthetic knowledge rows for a THROWAWAY production-compose volume.
-- Run by tests/hand_run/a7_seed_knowledge.sh as divesafe_owner; never through kb_load. The rows
-- are labelled synthetic and are not dive guidance. The insert triggers still validate and stamp
-- every row. Refuses a database that already holds any knowledge row.
\set ON_ERROR_STOP on
BEGIN ISOLATION LEVEL READ COMMITTED;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.kb_documents) OR EXISTS (SELECT 1 FROM public.kb_status_events)
    THEN
        RAISE EXCEPTION 'refused: this database already holds knowledge rows; use a new throwaway volume';
    END IF;
END;
$$;
-- Three documents: one APPROVED (hash checked), one APPROVED then WITHDRAWN, one DRAFT only.
INSERT INTO public.kb_documents (document_id, version, title, origin, author_role, approver_role,
    effective_date, review_by_date, licence, licence_reference_present, attribution, site_id,
    all_sites, display_permitted, supersedes, manifest_sha256)
SELECT d.id, 1, 'SYNTHETIC A7 hand-run document ' || d.id, 'dive_professional',
    'dive_professional', 'dive_professional', current_date, (current_date + interval '6 months')::date,
    'synthetic test text, no licence needed', true, NULL, NULL, true, true, NULL,
    repeat(d.hex, 64)
FROM (VALUES ('a7-approved', 'a'), ('a7-withdrawn', 'b'), ('a7-draft', 'c')) AS d(id, hex);
INSERT INTO public.kb_chunks (document_id, version, chunk, section, chunk_text, content_sha256)
SELECT c.id, 1, c.n, 'Synthetic', c.body,
    encode(sha256(convert_to(c.body, 'UTF8')), 'hex')
FROM (VALUES
    ('a7-approved', 1, 'SYNTHETIC A7 hand-run text, chunk 1. Not dive guidance.'),
    ('a7-approved', 2, 'SYNTHETIC A7 hand-run text, chunk 2. Not dive guidance.'),
    ('a7-withdrawn', 1, 'SYNTHETIC A7 hand-run text, withdrawn. Not dive guidance.'),
    ('a7-draft', 1, 'SYNTHETIC A7 hand-run text, draft. Not dive guidance.')
) AS c(id, n, body);
INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) VALUES
    ('a7-approved', 1, 'DRAFT', false),
    ('a7-withdrawn', 1, 'DRAFT', false),
    ('a7-draft', 1, 'DRAFT', false);
INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) VALUES
    ('a7-approved', 1, 'APPROVED', true),
    ('a7-withdrawn', 1, 'APPROVED', false);
INSERT INTO public.kb_status_events (document_id, version, status, hash_checked) VALUES
    ('a7-withdrawn', 1, 'WITHDRAWN', false);
COMMIT;
SELECT 'seeded documents=' || (SELECT count(*) FROM public.kb_documents)
    || ' chunks=' || (SELECT count(*) FROM public.kb_chunks)
    || ' events=' || (SELECT count(*) FROM public.kb_status_events);
