"use strict";
// Web UI launcher (ADR 0017, owner #50a, #55a). Next.js reads its two secrets from the
// environment, so this reads them from the files compose mounts under /run/secrets and sets them
// for this process only. It then opens the API relay and starts `next start`, both in this one
// process, so a restart of the container restarts them together. Nothing is printed but fixed
// text.
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const SECRETS = [
  ["DIVESAFE_WEB_SESSION_SECRET", "/run/secrets/divesafe_web_session_secret", /^[0-9a-f]{64}$/],
  [
    "NEXT_SERVER_ACTIONS_ENCRYPTION_KEY",
    "/run/secrets/divesafe_web_actions_key",
    /^[A-Za-z0-9+/]{43}=$/,
  ],
];

function refuse(message) {
  process.stderr.write(`web-start: ${message}\n`);
  process.exit(1);
}

for (const [name, file, shape] of SECRETS) {
  // A value from the environment would bypass the file (and show in `docker inspect`).
  if (name in process.env) refuse(`${name} must not be set in the environment`);
  let raw;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch {
    refuse(`cannot read the secret file for ${name}`);
  }
  const value = raw.endsWith("\n") ? raw.slice(0, -1) : raw;
  if (!shape.test(value)) refuse(`the secret file for ${name} is not in the expected form`);
  // The shape already implies 32 bytes; this second check keeps that true if the shape changes.
  if (name === "NEXT_SERVER_ACTIONS_ENCRYPTION_KEY" && Buffer.from(value, "base64").length !== 32)
    refuse(`the secret file for ${name} is not 32 bytes of base64`);
  process.env[name] = value;
}

// The API relay (owner #48a, #52a, #54a, #55a): 127.0.0.1:8000 here, which is what the web code
// calls, to the API's fixed address on the internal web-api network. It never looks a name up,
// because this container has a route out. Idle connections close after 120 s on either side.
const relay = net.createServer((c) => {
  const u = net.connect(8000, "10.231.78.10");
  c.setTimeout(120000, () => c.destroy());
  u.setTimeout(120000, () => u.destroy());
  c.pipe(u).pipe(c);
  u.on("error", () => c.destroy());
  c.on("error", () => u.destroy());
  u.on("close", () => c.destroy());
  c.on("close", () => u.destroy());
});
relay.maxConnections = 64;
// A relay server error, even after start-up, ends Next too; Docker restarts the container.
relay.on("error", () => refuse("the API relay failed"));

// Next starts only once the relay listens.
relay.listen(8000, "127.0.0.1", () => {
  const bin = path.join(__dirname, "node_modules", "next", "dist", "bin", "next");
  process.argv = [process.argv[0], bin, "start", "-H", "0.0.0.0", "-p", "3100"];
  require(bin);
});
