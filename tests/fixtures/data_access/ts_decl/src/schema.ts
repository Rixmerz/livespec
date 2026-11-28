const DDL = `
CREATE TABLE IF NOT EXISTS gizmos (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO gizmos (id, name) VALUES (1, 'seed');
`;

export function init(db: { exec(sql: string): void }) {
  db.exec(DDL);
}

export function inline(db: { exec(sql: string): void }) {
  db.exec("CREATE TABLE sprockets (id INTEGER PRIMARY KEY)");
}
