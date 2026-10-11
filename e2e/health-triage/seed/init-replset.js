// Replica set and users for the e2e MongoDB (mongosh script, run on the primary's container
// through the localhost exception, so before any user exists):
//
//   rs0: caliban-e2e-mongo (primary, priority 2)
//        caliban-e2e-mongo-analytics (secondary, priority 0, tag workload=analytics)
//
// Caliban's native lane reads with readPreference secondary + tag workload=analytics (core
// crates/caliban-ontology/src/compile/mongo.rs, NativeOptions), so queries need a tagged
// secondary: the analytics member serves them and the primary never does.
//
// Users: E2E_MONGO_ROOT_USER (root, for seeding and the run script's profiler checks) and
// caliban_reader (role `read` on marketplace only: the principal Caliban registers, which the
// connector checks is read only).

const env = process.env;
const ROOT_USER = env.E2E_MONGO_ROOT_USER || "e2e_root";

let initiated = true;
try {
  rs.status();
} catch (e) {
  initiated = false;
}
if (!initiated) {
  rs.initiate({
    _id: "rs0",
    members: [
      { _id: 0, host: "caliban-e2e-mongo:27017", priority: 2 },
      { _id: 1, host: "caliban-e2e-mongo-analytics:27017", priority: 0, tags: { workload: "analytics" } },
    ],
  });
}
for (let i = 0; i < 120 && !db.hello().isWritablePrimary; i++) sleep(500);
if (!db.hello().isWritablePrimary) throw new Error("no primary after 60 s");

const admin = db.getSiblingDB("admin");
if (!initiated || admin.getUsers({ filter: { user: ROOT_USER } }).users.length === 0) {
  try {
    admin.createUser({ user: ROOT_USER, pwd: env.E2E_MONGO_ROOT_PASSWORD, roles: ["root"] });
  } catch (e) {
    if (!String(e).includes("already exists") && !String(e).includes("requires authentication")) throw e;
  }
}
print("replica set rs0 ready");
