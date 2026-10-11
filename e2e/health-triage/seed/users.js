// The read-only principal Caliban connects as, and profiling on both members (mongosh script,
// run as the root user after init-replset.js). The run script reads system.profile on the
// analytics member to show the exact pipelines each node run sent, by their
// `caliban:node:<run>:<step>` comment.

const env = process.env;
const m = db.getSiblingDB("marketplace");
if (m.getUsers({ filter: { user: "caliban_reader" } }).users.length === 0) {
  m.createUser({ user: "caliban_reader", pwd: env.E2E_MONGO_READER_PASSWORD, roles: [{ role: "read", db: "marketplace" }] });
}
// Wait until the analytics member has caught up, so the first query finds the seed there.
for (let i = 0; i < 120; i++) {
  const s = rs.status().members.find((x) => x.name.startsWith("caliban-e2e-mongo-analytics"));
  if (s && s.stateStr === "SECONDARY" && s.optimeDate >= rs.status().members.find((x) => x.stateStr === "PRIMARY").optimeDate) break;
  sleep(500);
}
m.setProfilingLevel(2);
print("caliban_reader ready; profiling on (primary)");
