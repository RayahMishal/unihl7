// Checks that the browser engine (<script id="core"> in index.html) gives exactly the same results
// as the Python engine (unihl7.py): same canonical model, same FHIR bundles, same ids, same rejections.
const fs = require("fs"), assert = require("assert"), { execFileSync } = require("child_process");
const html = fs.readFileSync(__dirname + "/index.html", "utf8");
const grab = id => new RegExp('<script id="' + id + '">([\\s\\S]*?)</script>').exec(html)[1];
const m = { exports: {} };
new Function("module", grab("data") + "\n" + grab("core") + "\nmodule.exports.SAMPLE_ORDER=SAMPLE_ORDER;")(m);
const E = m.exports;

let py;
try {
  py = JSON.parse(execFileSync("python3", ["-c", `
import json
from unihl7 import Gateway, load_profiles, samples, parse_ts
gw = Gateway(); out = []
s = samples()
order = ["hospital_a_standard", "hospital_b_custom", "lab_c_results", "hospital_b_bad"]
for k in order:
    ok, info, bundle = gw.process(s[k])
    out.append({"ok": ok, "info": info, "bundle": bundle})
unk = Gateway().process(s["hospital_a_standard"].replace("HOSP_A", "NEWHOSP"))
print(json.dumps({"results": out, "profiles": load_profiles(), "samples": s, "people": len(gw.index.people),
  "stats": gw.stats, "unknownNotes": unk[1]["notes"],
  "ts": [parse_ts(x) for x in ["20261004","202610041015","20261004101500+0100","20260231","2026100410"]]}))
`], { cwd: __dirname, encoding: "utf8" }));
} catch (e) { console.log("python3 not available, skipping parity tests:", e.message.split("\n")[0]); process.exit(0); }

let n = 0; const t = (name, fn) => { fn(); n++; console.log("ok -", name); };
const norm = o => JSON.parse(JSON.stringify(o));

t("embedded profiles and samples match profiles.json and samples()", () => {
  assert.deepStrictEqual(norm(E.PROFILES), py.profiles);
  assert.deepStrictEqual(norm(E.SAMPLES), py.samples);
});
const gw = new E.Gateway(E.PROFILES);
const order = ["hospital_a_standard", "hospital_b_custom", "lab_c_results", "hospital_b_bad"];
const js = order.map(k => { const [ok, info, bundle] = gw.process(E.SAMPLES[k]); return { ok, info, bundle }; });

t("same accept/reject decision and canonical model for every sample", () => {
  js.forEach((r, i) => { assert.strictEqual(r.ok, py.results[i].ok, order[i]); assert.deepStrictEqual(norm(r.info), py.results[i].info, order[i]); });
});
t("same FHIR bundles, including UUIDv5 ids", () => {
  js.forEach((r, i) => assert.deepStrictEqual(norm(r.bundle), py.results[i].bundle, order[i]));
});
t("three dialects become one patient; broken message is rejected with every problem", () => {
  assert.strictEqual(gw.index.people.size, py.people);
  assert.deepStrictEqual(gw.stats, py.stats);
  const bad = js[3].info.problems.join(" ");
  for (const s of ["identifier missing", "31/02/1985", "patient class 'XX'"]) assert.ok(bad.includes(s), s);
});
t("unknown sender falls back to the standard profile with a note", () => {
  const [, notes] = E.normalize(E.SAMPLES.hospital_a_standard.replace("HOSP_A", "NEWHOSP"), E.PROFILES);
  assert.deepStrictEqual(notes, py.unknownNotes);
});
t("timestamps", () => {
  const got = ["20261004", "202610041015", "20261004101500+0100", "20260231", "2026100410"].map(E.parseTs);
  assert.deepStrictEqual(got, py.ts);
});
t("custom delimiters and Z-segment are read from the message", () => {
  const msg = new E.Message(E.SAMPLES.hospital_b_custom);
  assert.deepStrictEqual([msg.f, msg.c], ["#", "$"]);
  assert.strictEqual(msg.get("ZPI-2.1"), "784000000000");
});
console.log(n + " tests passed");
