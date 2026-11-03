-- PRD v1.2 4장 데이터 정의. 기사 본문 컬럼은 두지 않는다(6장 본문 미저장).

-- 4.1 수집한 기사
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id        TEXT NOT NULL,
    media_name      TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    url_norm        TEXT NOT NULL UNIQUE,
    published_date  TEXT,                       -- YYYY-MM-DD
    collected_at    TEXT NOT NULL,              -- ISO8601 (KST)
    search_keyword  TEXT,                       -- section_list: section:{코드} | list:all
    found_by        TEXT NOT NULL DEFAULT 'regular'
                    CHECK (found_by IN ('regular', 'verify')),
    access_level    TEXT CHECK (access_level IN ('full', 'partial', 'blocked')),
    article_type    TEXT CHECK (article_type IN ('single', 'multi', 'history', 'none')),
    type_reason     TEXT,
    process_status  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (process_status IN ('pending', 'prefiltered_out', 'done', 'llm_error', 'fetch_error')),
    llm_provider    TEXT CHECK (llm_provider IN ('claude_cli', 'mock')),
    llm_model       TEXT,
    llm_attempts    INTEGER NOT NULL DEFAULT 0,
    error_msg       TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_media_date ON articles (media_id, published_date);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles (process_status);

-- 4.2 발표 후보와 채택 결과
CREATE TABLE IF NOT EXISTS announcements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id          INTEGER NOT NULL REFERENCES articles (id),
    steelmaker          TEXT NOT NULL,
    apply_date          TEXT NOT NULL,          -- YYYY-MM-DD
    apply_date_inferred INTEGER NOT NULL DEFAULT 0,
    items_json          TEXT NOT NULL,          -- [{plant, grade, grade_raw, change_krw_per_kg, note}]
    content_text        TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('up', 'down', 'mixed')),
    source_type         TEXT NOT NULL CHECK (source_type IN ('single', 'multi', 'history')),
    dedup_key           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'candidate'
                        CHECK (status IN ('candidate', 'adopted', 'adopted_provisional', 'rejected', 'replaced')),
    reject_reason       TEXT,
    confidence          REAL,
    needs_review        INTEGER NOT NULL DEFAULT 0,
    conflict            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_dedup ON announcements (dedup_key);
CREATE INDEX IF NOT EXISTS idx_announcements_maker_date ON announcements (steelmaker, apply_date);
CREATE INDEX IF NOT EXISTS idx_announcements_status ON announcements (status);

-- 4.3 채택 교체 이력
CREATE TABLE IF NOT EXISTS adoption_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key           TEXT NOT NULL,
    old_announcement_id INTEGER REFERENCES announcements (id),
    new_announcement_id INTEGER NOT NULL REFERENCES announcements (id),
    reason              TEXT NOT NULL
                        CHECK (reason IN ('initial', 'multi_to_single', 'history_to_original', 'earlier_article')),
    changed_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adoption_history_dedup ON adoption_history (dedup_key);

-- 4.4 원본 추가 검색
CREATE TABLE IF NOT EXISTS verify_tasks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key               TEXT NOT NULL,
    history_announcement_id INTEGER REFERENCES announcements (id),
    steelmaker              TEXT NOT NULL,
    apply_date              TEXT NOT NULL,
    queries_json            TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'found', 'not_found')),
    attempts                INTEGER NOT NULL DEFAULT 0,
    found_article_id        INTEGER REFERENCES articles (id),
    last_searched_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_verify_tasks_dedup ON verify_tasks (dedup_key);
CREATE INDEX IF NOT EXISTS idx_verify_tasks_status ON verify_tasks (status);

-- 4.5 실행 로그
CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    mode        TEXT NOT NULL CHECK (mode IN ('daily', 'backfill', 'manual', 'reselect')),
    date_from   TEXT,
    date_to     TEXT,
    stats_json  TEXT,
    status      TEXT CHECK (status IN ('success', 'partial', 'failed'))   -- 실행 중에는 NULL
);
