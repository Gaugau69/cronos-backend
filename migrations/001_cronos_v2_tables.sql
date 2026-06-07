-- ============================================================
-- Migration 001 — CRONOS v2 : nouvelles tables
-- À exécuter une fois sur Supabase via l'éditeur SQL
-- ============================================================

-- 1. Feedback utilisateur sur les séances ─────────────────────
CREATE TABLE IF NOT EXISTS session_feedback (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id   INTEGER NOT NULL,
    session_name VARCHAR(200) NOT NULL,
    feedback     VARCHAR(20) NOT NULL CHECK (feedback IN ('facile', 'ok', 'difficile')),
    done_at      DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_session_feedback_user_date
    ON session_feedback (user_id, done_at DESC);

-- 2. Cache des recommandations (1 entrée par user par jour) ───
CREATE TABLE IF NOT EXISTS recommendations_cache (
    id                   SERIAL PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cache_date           DATE NOT NULL DEFAULT CURRENT_DATE,
    recommendations_json TEXT NOT NULL,
    athlete_json         TEXT,
    recovery_json        TEXT,
    load_json            TEXT,
    computed_with_ml     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, cache_date)
);

-- 3. Tracks GPS des activités (pour la heatmap) ───────────────
CREATE TABLE IF NOT EXISTS activity_tracks (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    activity_id      BIGINT NOT NULL,
    coordinates_json TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, activity_id)
);
CREATE INDEX IF NOT EXISTS idx_activity_tracks_user
    ON activity_tracks (user_id);

-- 4. Préférences de notifications email ───────────────────────
CREATE TABLE IF NOT EXISTS notification_prefs (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    email_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    send_hour     SMALLINT NOT NULL DEFAULT 7 CHECK (send_hour BETWEEN 0 AND 23),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Vérification ────────────────────────────────────────────────
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'session_feedback',
    'recommendations_cache',
    'activity_tracks',
    'notification_prefs'
  )
ORDER BY table_name;
