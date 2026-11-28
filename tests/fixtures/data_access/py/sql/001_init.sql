CREATE TABLE users (id integer primary key, email text);
CREATE TABLE orders (id integer primary key, user_id integer);
CREATE TABLE audit_log (id integer primary key, user_id integer);
CREATE TABLE never_used (id integer primary key);
