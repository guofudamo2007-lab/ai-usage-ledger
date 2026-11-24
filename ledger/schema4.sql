CREATE TABLE health(entry TEXT PRIMARY KEY, last_read TEXT, readable INTEGER, added INTEGER, enriched INTEGER, conflicts INTEGER, details TEXT, ever_present INTEGER);
CREATE TABLE batches(id INTEGER PRIMARY KEY, created TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT, undone TEXT);
CREATE TABLE contributions(batch INTEGER NOT NULL, line INTEGER NOT NULL, uid TEXT NOT NULL, payload TEXT NOT NULL, outcome TEXT NOT NULL, PRIMARY KEY(batch,line));
CREATE INDEX contributions_uid ON contributions(uid);
CREATE TABLE legacy_base(uid TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE agy_keys(anchor TEXT NOT NULL, request TEXT NOT NULL, uid TEXT NOT NULL, PRIMARY KEY(anchor,request));
CREATE INDEX agy_request ON agy_keys(request);
CREATE TABLE observations(id INTEGER PRIMARY KEY, uid TEXT NOT NULL, origin TEXT NOT NULL, entry TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, seen TEXT NOT NULL, UNIQUE(uid,origin,payload));
CREATE INDEX observations_uid ON observations(uid);
CREATE TABLE observation_heads(uid TEXT NOT NULL, origin TEXT NOT NULL, source TEXT NOT NULL, observation INTEGER NOT NULL, PRIMARY KEY(uid,origin,source));
CREATE TABLE disputes(uid TEXT PRIMARY KEY, reason TEXT NOT NULL, resolved INTEGER REFERENCES observations(id));
INSERT INTO disputes(uid,reason) SELECT uid,'旧账本子项超过总量，待核对原始统计' FROM events
 WHERE total IS NOT NULL AND max(coalesce(input,0),coalesce(cache_read,0),coalesce(cache_write,0)) + max(coalesce(output,0),coalesce(reasoning,0)) > total;
CREATE TABLE annotations(uid TEXT PRIMARY KEY, project TEXT, account TEXT, task TEXT);
CREATE TABLE aliases(raw TEXT PRIMARY KEY, logical TEXT NOT NULL);
CREATE TABLE changes(id INTEGER PRIMARY KEY, at TEXT NOT NULL, target TEXT NOT NULL, action TEXT NOT NULL, previous TEXT, value TEXT, reason TEXT NOT NULL);
CREATE VIEW ledger_events AS SELECT e.uid,e.ts,e.entry,e.vendor,e.model,coalesce(a.project,l.logical,e.project) project,e.source,e.total,e.input,e.output,e.cache_read,e.cache_write,e.reasoning,e.project raw_project,coalesce(a.account,'账号未确认') account,coalesce(a.task,'未关联') task FROM events e LEFT JOIN annotations a ON a.uid=e.uid LEFT JOIN aliases l ON l.raw=e.project;

