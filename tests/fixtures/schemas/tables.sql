-- Fixture for SQL schema extraction.
PRAGMA foreign_keys = ON;

CREATE TYPE mood AS ENUM ('happy', 'sad');

-- Application users.
-- @schema: user-profile
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,             -- login; unique
    bio TEXT,
    "display name" VARCHAR(80) NOT NULL DEFAULT 'anon',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    balance NUMERIC(10, 2) NOT NULL CHECK (balance >= (0)),
    feeling mood,
    tags TEXT[],
    payload JSONB,
    born DATE,
    external_id UUID NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    ratio DOUBLE PRECISION,
    seen_at TIMESTAMP WITH TIME ZONE,
    avatar BLOB,
    UNIQUE(email),
    CONSTRAINT bio_len CHECK (length(bio) < 500)
);

/* Orders placed by users.
 * One row per checkout.
 */
create table orders (
    id BIGSERIAL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code CHAR(3),
    total INT GENERATED ALWAYS AS (1) STORED,
    PRIMARY KEY (id)
);

CREATE TABLE order_items (
    order_id INTEGER,
    sku TEXT,
    PRIMARY KEY (order_id, sku),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE VIRTUAL TABLE IF NOT EXISTS users_fts USING fts5(bio);
CREATE TRIGGER IF NOT EXISTS users_ai AFTER INSERT ON users BEGIN
    INSERT INTO users_fts(rowid, bio) VALUES (new.id, new.bio);
    SELECT CASE WHEN new.bio IS NULL THEN 1 ELSE 0 END;
END;
ALTER TABLE users ADD COLUMN extra TEXT;
CREATE VIEW active_users AS SELECT * FROM users;
