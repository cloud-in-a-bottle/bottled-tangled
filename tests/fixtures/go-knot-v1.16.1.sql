-- A Go knot (v1.16.1-alpha) database, as app 1.0.0 left it in the app data dir: `sqlite3 knotserver.db .dump` of the
-- database the knot created on its first start (owner did:plc:upstreamcoveragetest2345, set through the setup form),
-- plus one repository added the way the knot's createRepo does it (knotserver/db/db.go storeRepoKeyRow, a random k256
-- signing key; rbac.AddRepo's casbin policies). test_migration.py loads it and creates the matching bare repository.
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE known_dids (
			did text primary key
		);
INSERT INTO known_dids VALUES('did:plc:upstreamcoveragetest2345');
CREATE TABLE public_keys (
			id integer primary key autoincrement,
			did text not null,
			key text not null,
			created text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')), rkey TEXT,
			unique(did, key),
			foreign key (did) references known_dids(did) on delete cascade
		);
CREATE TABLE _jetstream (
			id integer primary key autoincrement,
			last_time_us integer not null
		);
INSERT INTO _jetstream VALUES(1,1794451924610419);
CREATE TABLE events (
			rkey text not null,
			nsid text not null,
			event text not null, -- json
			created integer not null default (strftime('%s', 'now')),
			primary key (rkey, nsid)
		);
CREATE TABLE migrations (
			id integer primary key autoincrement,
			name text unique
		);
INSERT INTO migrations VALUES(1,'add-owner-did-to-repo-keys');
INSERT INTO migrations VALUES(2,'add-repo-name-to-repo-keys');
INSERT INTO migrations VALUES(3,'add-unique-owner-repo-on-repo-keys');
INSERT INTO migrations VALUES(4,'add-key-type-and-nullable-signing-key');
INSERT INTO migrations VALUES(5,'add-repo-aliases');
INSERT INTO migrations VALUES(6,'drop-at-uri-from-repo-keys');
INSERT INTO migrations VALUES(7,'create-knot-members');
INSERT INTO migrations VALUES(8,'add-isolated-at-to-repo-keys');
INSERT INTO migrations VALUES(9,'add-owner-uid-tables');
INSERT INTO migrations VALUES(10,'knot-members-nullable-rkey');
INSERT INTO migrations VALUES(11,'create-collaborators');
INSERT INTO migrations VALUES(12,'knot-members-direct-subject-unique');
INSERT INTO migrations VALUES(13,'add-events-created-index');
INSERT INTO migrations VALUES(14,'add-rkey-to-public-keys');
INSERT INTO migrations VALUES(15,'enforce-global-key-uniqueness');
INSERT INTO migrations VALUES(16,'backfill-knot-members-from-casbin-v1');
INSERT INTO migrations VALUES(17,'backfill-collaborators-from-casbin-v1');
CREATE TABLE repo_aliases (
				owner_did text not null,
				rkey      text not null,
				repo_did  text not null,
				rev       text not null,
				primary key (owner_did, rkey)
			);
INSERT INTO repo_aliases VALUES('did:plc:upstreamcoveragetest2345','migrated-repo','did:plc:migrationfixturerepo2345','0_2026-11-12T02:52:39Z');
CREATE TABLE IF NOT EXISTS "repo_keys" (
				repo_did    text primary key,
				signing_key blob,
				created_at  text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
				owner_did   text,
				repo_name   text,
				key_type    text not null default 'k256'
			, isolated_at DATETIME);
INSERT INTO repo_keys VALUES('did:plc:migrationfixturerepo2345',X'd60eb9611985f5081d634610bf99e306fae00d6d6ae7929dcc0dae5ac8e57887','2026-11-12T02:52:39Z','did:plc:upstreamcoveragetest2345','migrated-repo','k256',NULL);
CREATE TABLE owner_uid_assignments (
				owner_did  TEXT PRIMARY KEY,
				uid        INTEGER NOT NULL UNIQUE,
				created_at DATETIME DEFAULT CURRENT_TIMESTAMP
			);
CREATE TABLE uid_counter (
				next_uid INTEGER NOT NULL DEFAULT 100000
			);
INSERT INTO uid_counter VALUES(100000);
CREATE TABLE IF NOT EXISTS "knot_members" (
				id integer primary key autoincrement,
				did text not null,
				rkey text,
				subject text not null,
				created text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
				unique (did, rkey)
			);
CREATE TABLE collaborators (
				id integer primary key autoincrement,
				repo_did text not null,
				subject_did text not null,
				added_by_did text not null,
				created text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
				unique (repo_did, subject_did)
			);
CREATE TABLE acl(
    p_type VARCHAR(32)  DEFAULT '' NOT NULL,
    v0     VARCHAR(255) DEFAULT '' NOT NULL,
    v1     VARCHAR(255) DEFAULT '' NOT NULL,
    v2     VARCHAR(255) DEFAULT '' NOT NULL,
    v3     VARCHAR(255) DEFAULT '' NOT NULL,
    v4     VARCHAR(255) DEFAULT '' NOT NULL,
    v5     VARCHAR(255) DEFAULT '' NOT NULL,
    CHECK (TYPEOF("p_type") = "text" AND
           LENGTH("p_type") <= 32),
    CHECK (TYPEOF("v0") = "text" AND
           LENGTH("v0") <= 255),
    CHECK (TYPEOF("v1") = "text" AND
           LENGTH("v1") <= 255),
    CHECK (TYPEOF("v2") = "text" AND
           LENGTH("v2") <= 255),
    CHECK (TYPEOF("v3") = "text" AND
           LENGTH("v3") <= 255),
    CHECK (TYPEOF("v4") = "text" AND
           LENGTH("v4") <= 255),
    CHECK (TYPEOF("v5") = "text" AND
           LENGTH("v5") <= 255)
);
INSERT INTO acl VALUES('p','server:owner','thisserver','thisserver','server:invite','','');
INSERT INTO acl VALUES('p','server:member','thisserver','thisserver','repo:create','','');
INSERT INTO acl VALUES('g','server:owner','server:member','thisserver','','','');
INSERT INTO acl VALUES('g','did:plc:upstreamcoveragetest2345','server:owner','thisserver','','','');
INSERT INTO acl VALUES('p','did:plc:upstreamcoveragetest2345','thisserver','did:plc:migrationfixturerepo2345','repo:settings','','');
INSERT INTO acl VALUES('p','did:plc:upstreamcoveragetest2345','thisserver','did:plc:migrationfixturerepo2345','repo:push','','');
INSERT INTO acl VALUES('p','did:plc:upstreamcoveragetest2345','thisserver','did:plc:migrationfixturerepo2345','repo:owner','','');
INSERT INTO acl VALUES('p','did:plc:upstreamcoveragetest2345','thisserver','did:plc:migrationfixturerepo2345','repo:invite','','');
INSERT INTO acl VALUES('p','did:plc:upstreamcoveragetest2345','thisserver','did:plc:migrationfixturerepo2345','repo:delete','','');
INSERT INTO acl VALUES('p','server:owner','thisserver','did:plc:migrationfixturerepo2345','repo:delete','','');
DELETE FROM sqlite_sequence;
INSERT INTO sqlite_sequence VALUES('migrations',17);
INSERT INTO sqlite_sequence VALUES('knot_members',0);
INSERT INTO sqlite_sequence VALUES('_jetstream',1);
CREATE INDEX idx_repo_aliases_repo_did on repo_aliases(repo_did);
CREATE UNIQUE INDEX idx_repo_keys_owner_repo
				on repo_keys(owner_did, repo_name);
CREATE INDEX idx_collaborators_repo_id
				on collaborators(repo_did, id);
CREATE UNIQUE INDEX idx_knot_members_direct_subject
				on knot_members(subject) where rkey is null;
CREATE INDEX idx_knot_members_subject
				on knot_members(subject);
CREATE INDEX idx_events_created on events(created);
CREATE UNIQUE INDEX idx_public_keys_key on public_keys(key);
CREATE INDEX idx_acl ON acl (p_type,v0,v1);
COMMIT;
