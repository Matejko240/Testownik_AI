PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  mime TEXT NOT NULL,
  pages INTEGER,
  sha256 TEXT,              -- hash pliku do deduplikacji uploadów
  imported_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  page INTEGER,       -- 1-based: strona PDF / slajd PPTX
  text TEXT NOT NULL,
  quote TEXT,         -- krótki cytat do listy źródeł
  embedding BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY,       -- uuid
  kind TEXT NOT NULL,        -- 'YN'|'MCQ'
  stem TEXT NOT NULL,
  options JSON,              -- MCQ: ["a) ...","b) ...","c) ...","d) ..."]
  answer TEXT NOT NULL,      -- 'TAK'/'NIE' lub 'a'|'b'|'c'|'d'
  explanation TEXT NOT NULL,
  metadata JSON,             -- {topic,difficulty,timestamp,...}
  fingerprint TEXT,
  created_at TEXT NOT NULL
);

-- index MUSI być po CREATE TABLE questions
CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_fingerprint ON questions(fingerprint);

CREATE TABLE IF NOT EXISTS question_citations (
  question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  page INTEGER NOT NULL,
  quote TEXT NOT NULL,
  PRIMARY KEY (question_id, source_id, page, quote)
);

CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY,
  question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  score INTEGER NOT NULL,   -- 1..5 lub 1..10 (UI decyduje, backend waliduje 1..10)
  feedback TEXT,
  created_at TEXT NOT NULL
);

-- Uczenie na ocenach: waga per chunk (preferuj „dobre” konteksty)
CREATE TABLE IF NOT EXISTS chunk_weights (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  weight REAL NOT NULL DEFAULT 0.0     -- używane w retrieve: sim*(1+weight)
);

CREATE VIEW IF NOT EXISTS question_quality AS
SELECT q.id AS question_id,
       ROUND(AVG(r.score),2) AS avg_score,
       COUNT(r.id) AS votes
FROM questions q LEFT JOIN ratings r ON r.question_id = q.id
GROUP BY q.id;
