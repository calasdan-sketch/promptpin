// promptpin runtime verifier for Node >= 18. Zero dependencies (node:crypto, node:fs).
// Checks an exported bundle (manifest.json) exactly as promptpin.load_bundle does,
// and returns prompts plus per-request receipts for tracing.
//
//   import { loadBundle, receipt } from "./promptpin-verify.mjs";
//   const bundle = loadBundle("./prompt-bundle", { expectedDigest: process.env.PROMPT_BUNDLE_DIGEST });
//   const { text, rx } = render(bundle.classifier, { ticket_text }); // rx = receipt attributes
//
// Digests are unkeyed SHA-256: they catch drift and edits, not an attacker who can
// rewrite the bundle AND your expectedDigest. Fail closed: every problem throws.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const VAR_RE = /\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g;
const ROLE_RE = /^[ \t]*(system|developer|user|assistant)[ \t]*:[ \t]*\r?$/gm;

export class PromptIntegrityError extends Error {}

const fail = (msg) => {
  throw new PromptIntegrityError(msg);
};

export function loadBundle(dir, { expectedDigest } = {}) {
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8"));
  } catch (e) {
    fail(`cannot read ${dir}/manifest.json: ${e.message} (re-run \`promptpin export\`)`);
  }
  const prompts = manifest && manifest.prompts;
  if (!prompts || typeof prompts !== "object" || Object.keys(prompts).length === 0) {
    fail("no prompts in bundle manifest");
  }
  const out = {};
  for (const [name, meta] of Object.entries(prompts)) {
    if (!NAME_RE.test(name) || !meta || typeof meta !== "object") fail(`invalid entry ${name}`);
    if (meta.file !== `${name}.template.md`) fail(`entry ${name} must reference ${name}.template.md`);
    const raw = readFileSync(join(dir, meta.file));
    const templateSha = sha256(raw);
    if (templateSha !== meta.template_sha256) fail(`template for ${name} does not match its sha256`);
    // digest_input is the exact string the digest hashes (no re-serialisation).
    if (typeof meta.digest_input !== "string" || sha256(meta.digest_input) !== meta.digest) {
      fail(`entry ${name}: digest does not match its digest_input`);
    }
    let bound;
    try {
      bound = JSON.parse(meta.digest_input);
    } catch {
      fail(`entry ${name}: digest_input is not JSON`);
    }
    if (
      bound.template_sha256 !== templateSha ||
      bound.model !== (meta.model || "") ||
      bound.format !== meta.format ||
      JSON.stringify(bound.params) !== JSON.stringify(meta.params ?? {})
    ) {
      fail(`entry ${name}: model/params/format differ from what the digest covers`);
    }
    out[name] = { ...meta, template: raw.toString("utf8") };
  }
  const lines = Object.keys(out)
    .sort()
    .map((n) => `${n}\t${out[n].sha}\t${out[n].digest}\n`)
    .join("");
  const actual = sha256(lines);
  if (manifest.bundle_digest !== actual) fail("bundle_digest does not match its entries");
  if (expectedDigest !== undefined && expectedDigest !== null && expectedDigest !== actual) {
    fail(`bundle digest ${actual.slice(0, 12)} is not the expected ${String(expectedDigest).slice(0, 12)}`);
  }
  Object.defineProperty(out, "bundleDigest", { value: actual, enumerable: false });
  Object.defineProperty(out, "env", { value: manifest.env, enumerable: false });
  return out;
}

const subst = (tpl, vars) =>
  tpl.replace(VAR_RE, (_, n) => {
    if (!Object.hasOwn(vars, n)) throw new KeyError(`template variable '${n}' was not provided`);
    return String(vars[n]);
  });

class KeyError extends Error {}

export function splitMessages(template) {
  const marks = [...template.matchAll(ROLE_RE)];
  if (!marks.length) fail("chat prompt has no role markers");
  if (template.slice(0, marks[0].index).trim()) fail("chat prompt has text before the first role");
  return marks.map((m, i) => {
    const end = i + 1 < marks.length ? marks[i + 1].index : template.length;
    const content = template.slice(m.index + m[0].length, end).replace(/^[\r\n]+|[\r\n]+$/g, "");
    if (!content.trim()) fail(`chat message #${i + 1} is empty`);
    return { role: m[1], content };
  });
}

// Renders and returns { output, attributes }: attributes use the same promptpin.* keys as
// the Python Receipt.attributes(), ready for span.setAttributes().
export function render(entry, vars = {}, bundle = null) {
  const messages = entry.format === "chat";
  const output = messages
    ? splitMessages(entry.template).map((m) => ({ role: m.role, content: subst(m.content, vars) }))
    : subst(entry.template, vars);
  const attributes = {
    "promptpin.prompt.sha": entry.sha,
    "promptpin.prompt.digest": entry.digest,
    "promptpin.prompt.template_sha256": entry.template_sha256,
    "promptpin.verified": true,
  };
  if (bundle) {
    attributes["promptpin.env"] = bundle.env;
    attributes["promptpin.bundle.digest"] = bundle.bundleDigest;
  }
  return { output, attributes };
}
