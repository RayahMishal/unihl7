"""
UniHL7 - one canonical model for many HL7 v2 dialects.

Problem: every sending system writes "HL7 v2" a little differently (delimiters, where the patient
ID lives, name and date formats, code values, Z-segments). Writing one hard-coded interface per
system multiplies integration effort.

Idea: describe each dialect in a small declarative profile (profiles.json). A single engine reads
a message, picks the sender's profile, normalizes it into ONE canonical model, validates it,
links patients across systems (a small master patient index) and emits FHIR R4.

    python unihl7.py samples                 # normalize the built-in multi-dialect samples
    python unihl7.py file.hl7                # normalize a message file
Real-time (MLLP over TCP) is in mllp.py.  Standard library only. Sample data is FAKE.
"""
import json
import os
import re
import sys
import uuid
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UUID_NS = uuid.UUID("0f5a7c1e-6a62-4a53-9c1d-2d6f4b1d9e11")
CLASS_FHIR = {"I": ("IMP", "inpatient encounter"), "O": ("AMB", "ambulatory"),
              "E": ("EMER", "emergency"), "P": ("PRENC", "pre-admission")}
FLAG_FHIR = {"N": ("N", "Normal"), "L": ("L", "Low"), "H": ("H", "High"), "LL": ("LL", "Critical low"),
             "HH": ("HH", "Critical high"), "A": ("A", "Abnormal"), "AA": ("AA", "Critical abnormal")}
OBS_STATUS = {"F": "final", "P": "preliminary", "C": "corrected", "X": "cancelled"}


class NormalizeError(Exception):
    """The message is not safe to convert; .problems lists every reason."""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


def load_profiles(path=None):
    with open(path or os.path.join(HERE, "profiles.json"), encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


# ----------------------------------------------------------------------------------
# Parsing (delimiters are read from the message, so custom delimiters just work)
# ----------------------------------------------------------------------------------
class Message:
    def __init__(self, text):
        lines = [l for l in re.split(r"\r\n|\r|\n", text.strip().strip("\x0b\x1c")) if l.strip()]
        if not lines or not lines[0].startswith("MSH") or len(lines[0]) < 8:
            raise NormalizeError(["not an HL7 v2 message (must start with MSH)"])
        f = lines[0][3]
        chars = lines[0].split(f)[1]
        if len(chars) < 4:
            raise NormalizeError(["MSH-2 encoding characters incomplete"])
        self.f, self.c, self.r, self.e, self.s = f, chars[0], chars[1], chars[2], chars[3]
        self.segs = [(l.split(f)[0], l.split(f)) for l in lines]

    def _field(self, seg, n):
        parts = next((p for name, p in self.segs if name == seg), None)
        if parts is None:
            return None
        i = n - 1 if seg == "MSH" else n
        return parts[i] if 0 <= i < len(parts) else ""

    def get(self, path, default=""):
        """'PID-3.1' -> component 1 of PID-3.  'PID-3.1@NI' -> same, in the repetition typed NI."""
        path, _, want_type = path.partition("@")
        seg, _, rest = path.partition("-")
        num, _, comp = rest.partition(".")
        raw = self._field(seg, int(num))
        if raw is None:
            return default
        for rep in raw.split(self.r):
            comps = rep.split(self.c)
            if want_type and (comps[4] if len(comps) > 4 else "") != want_type:
                continue
            if comp:
                value = comps[int(comp) - 1] if len(comps) >= int(comp) else ""
            else:
                value = rep
            return self.unescape(value.strip())
        return default

    def unescape(self, t):
        table = {"F": self.f, "S": self.c, "T": self.s, "R": self.r, "E": self.e}
        e = re.escape(self.e)
        return re.sub(e + r"([FSTRE])" + e, lambda m: table[m.group(1)], t)

    def segments(self, name):
        return [p for n, p in self.segs if n == name]


# ----------------------------------------------------------------------------------
# Normalization: dialect -> canonical model
# ----------------------------------------------------------------------------------
def pick_profile(msg, profiles):
    for name, prof in profiles.items():
        match = prof.get("match")
        if match and all(msg.get(k) == v for k, v in match.items()):
            return name, prof
    return "default", profiles["default"]


def parse_date(value, fmt):
    value = value.strip()
    try:
        if fmt == "dmy_slash":
            return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
        m = re.match(r"^(\d{4})(\d{2})(\d{2})", value)
        if not m:
            raise ValueError
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_ts(value):
    """HL7 timestamp (with optional time and +zzzz) -> ISO 8601, or None. Date only stays date only."""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(\d{2})?)?(?:\.\d+)?([+-]\d{4})?$", value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, tz = m.groups()
    try:
        datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    if h is None:
        return f"{y}-{mo}-{d}"
    return f"{y}-{mo}-{d}T{h}:{mi}:{s or '00'}" + (f"{tz[:3]}:{tz[3:]}" if tz else "+03:00")


def parse_name(raw, fmt):
    raw = raw.strip()
    if fmt == "family_comma_given":
        family, _, given = raw.partition(",")
        return family.strip().title(), [g for g in given.split() if g]
    if fmt == "given_family_upper":
        words = raw.title().split()
        return (words[-1], words[:-1]) if words else ("", [])
    return raw, None  # xpn handled by caller


def normalize(text, profiles):
    """Return (canonical, notes). Raises NormalizeError listing every problem."""
    msg = Message(text)
    pname, prof = pick_profile(msg, profiles)
    problems, notes = [], []
    if pname == "default":
        notes.append(f"unknown sender '{msg.get('MSH-4')}': standard HL7 profile used")

    mtype = msg.get("MSH-9.1") + "^" + msg.get("MSH-9.2")
    if mtype.split("^")[0] not in ("ADT", "ORU"):
        problems.append(f"message type {mtype} is not supported (ADT and ORU only)")

    # patient ------------------------------------------------------------------
    mrn, nid = msg.get(prof["mrn"]), msg.get(prof.get("national_id", ""), "") if prof.get("national_id") else ""
    if not mrn:
        problems.append(f"patient identifier missing ({prof['mrn']})")
    if prof["name_format"] == "xpn":
        family, given = msg.get(prof["name"] + ".1"), [g for g in (msg.get(prof["name"] + ".2"),
                                                                    msg.get(prof["name"] + ".3")) if g]
    else:
        family, given = parse_name(msg.get(prof["name"]), prof["name_format"])
    if not family:
        problems.append(f"patient name missing ({prof['name']})")
    dob_raw = msg.get(prof["dob"])
    dob = parse_date(dob_raw, prof["dob_format"]) if dob_raw else None
    if dob_raw and not dob:
        problems.append(f"date of birth '{dob_raw}' is not a valid {prof['dob_format']} date ({prof['dob']})")
    sex_raw = msg.get(prof["sex"])
    sex = prof["sex_map"].get(sex_raw) or prof["sex_map"].get(sex_raw.lower()) or prof["sex_map"].get(sex_raw.upper())
    if sex_raw and not sex:
        notes.append(f"{prof['sex']} value '{sex_raw}' has no mapping - left unknown")
    canonical = {
        "source": {"profile": pname, "facility": msg.get("MSH-4"), "controlId": msg.get("MSH-10"),
                   "messageType": mtype.strip("^")},
        "patient": {"mrn": mrn, "mrnSystem": (msg.get(prof["mrn_authority"]) if re.match(r"^[A-Z]{3}-\d", prof["mrn_authority"])
                                             else prof["mrn_authority"]) or pname,
                    "nationalId": nid, "family": family, "given": given or [],
                    "birthDate": dob, "sex": sex or "unknown"},
        "encounter": None, "observations": []}

    pv1 = msg.segments("PV1")
    if pv1:
        cls_raw = msg.get("PV1-2")
        cls = prof["class_map"].get(cls_raw) or prof["class_map"].get(cls_raw.upper())
        if not cls:
            problems.append(f"patient class '{cls_raw}' has no mapping (PV1-2)")
        start, end = msg.get("PV1-44"), msg.get("PV1-45")
        for label, v in (("PV1-44", start), ("PV1-45", end)):
            if v and not parse_ts(v):
                problems.append(f"{label} '{v}' is not a valid timestamp")
        canonical["encounter"] = {"class": cls, "location": msg.get("PV1-3").replace(msg.c, " / "),
                                  "visit": msg.get("PV1-19.1") or msg.get("PV1-19"),
                                  "start": parse_ts(start) if start else None,
                                  "end": parse_ts(end) if end else None}

    for p in msg.segments("OBX"):
        def fld(i, c=None, p=p):
            if i >= len(p):
                return ""
            v = p[i].split(msg.r)[0]
            if c is None:
                return msg.unescape(v)
            comps = v.split(msg.c)
            return msg.unescape(comps[c - 1]) if len(comps) >= c else ""
        flag = fld(8, 1)
        if flag and flag not in FLAG_FHIR:
            notes.append(f"OBX-8 flag '{flag}' ignored")
        canonical["observations"].append({
            "code": fld(3, 1), "display": fld(3, 2), "system": fld(3, 3), "valueType": fld(2),
            "value": fld(5), "units": fld(6, 1), "flag": flag if flag in FLAG_FHIR else "",
            "status": OBS_STATUS.get(fld(11), "final")})
        if canonical["observations"][-1]["valueType"] == "NM" and not re.match(r"^[+-]?\d*\.?\d+$", fld(5)):
            problems.append(f"OBX result '{fld(5)}' for {fld(3, 2) or fld(3, 1)} is not numeric")

    if problems:
        raise NormalizeError(problems)
    return canonical, notes


# ----------------------------------------------------------------------------------
# Master patient index: the same person from different systems becomes ONE patient
# ----------------------------------------------------------------------------------
class MasterIndex:
    def __init__(self):
        self.people = {}  # match key -> {"id", "identifiers": {(system, value)}}

    @staticmethod
    def key(p):
        if p["nationalId"]:
            return "nid:" + p["nationalId"]
        return "demo:" + "|".join([p["family"].lower(), " ".join(p["given"]).lower(), p["birthDate"] or ""])

    def link(self, p):
        k = self.key(p)
        person = self.people.setdefault(k, {"id": str(uuid.uuid5(UUID_NS, k)), "identifiers": {}, "sources": set()})
        person["identifiers"][(p["mrnSystem"], p["mrn"])] = True
        return person


# ----------------------------------------------------------------------------------
# Canonical -> FHIR R4
# ----------------------------------------------------------------------------------
def to_fhir(canonical, person):
    p = canonical["patient"]
    patient = {"resourceType": "Patient", "id": person["id"],
               "identifier": [{"system": "urn:unihl7:" + s.lower(), "value": v,
                               "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                                                    "code": "MR"}]}} for (s, v) in person["identifiers"]],
               "name": [{"family": p["family"], "given": p["given"]}], "gender": p["sex"]}
    if p["nationalId"]:
        patient["identifier"].append({"system": "urn:unihl7:national-id", "value": p["nationalId"]})
    if p["birthDate"]:
        patient["birthDate"] = p["birthDate"]
    entries = [patient]
    ref = {"reference": f"Patient/{person['id']}"}
    enc = canonical["encounter"]
    enc_id = None
    if enc:
        code, display = CLASS_FHIR[enc["class"]]
        enc_id = str(uuid.uuid5(UUID_NS, f"enc|{canonical['source']['facility']}|{enc['visit'] or canonical['source']['controlId']}"))
        e = {"resourceType": "Encounter", "id": enc_id, "status": "finished" if enc["end"] else "in-progress",
             "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": code, "display": display},
             "subject": ref}
        if enc["start"] or enc["end"]:
            e["period"] = {k: v for k, v in (("start", enc["start"]), ("end", enc["end"])) if v}
        if enc["location"]:
            e["location"] = [{"location": {"display": enc["location"]}}]
        entries.append(e)
    for i, o in enumerate(canonical["observations"]):
        coding = {"code": o["code"], "display": o["display"] or o["code"]}
        if o["system"] in ("LN", "LOINC"):
            coding["system"] = "http://loinc.org"
        obs = {"resourceType": "Observation", "status": o["status"],
               "id": str(uuid.uuid5(UUID_NS, f"obs|{canonical['source']['controlId']}|{i}")),
               "code": {"coding": [coding]}, "subject": ref}
        if enc_id:
            obs["encounter"] = {"reference": f"Encounter/{enc_id}"}
        if o["valueType"] == "NM":
            obs["valueQuantity"] = {"value": float(o["value"]), "unit": o["units"]}
        else:
            obs["valueString"] = o["value"]
        if o["flag"]:
            c, d = FLAG_FHIR[o["flag"]]
            obs["interpretation"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                                  "code": c, "display": d}]}]
        entries.append(obs)
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in entries]}


# ----------------------------------------------------------------------------------
# Sample messages: the SAME fake patient arriving from three different systems
# ----------------------------------------------------------------------------------
def _row(name, fields, sep="|"):
    n = max(fields) if fields else 0
    out = [""] * (n + 1)
    out[0] = name
    for k, v in fields.items():
        out[k] = v
    return sep.join(out)


def samples():
    a = "\r".join([
        "MSH|^~\\&|HIS_A|HOSP_A|UNIHL7|HUB|202610040800||ADT^A01^ADT_A01|A001|P|2.5.1",
        _row("PID", {1: "1", 3: "10001^^^HOSP_A^MR~784000000000^^^NATIONAL^NI", 5: "Doe^Jane^Marie", 7: "19900214", 8: "F"}),
        _row("PV1", {1: "1", 2: "I", 3: "ICU^Room 3^Bed 2", 19: "V9001^^^HOSP_A", 44: "202610040800"})])
    b = "\r".join([
        "MSH#$~\\&#REG_B#HOSP_B#UNIHL7#HUB#20261004093000##ADT$A04#B777#P#2.3",
        _row("PID", {1: "1", 2: "B-7788", 5: "Doe, Jane Marie", 7: "14/02/1990", 8: "2"}, "#"),
        _row("ZPI", {1: "1", 2: "784000000000"}, "#"),
        _row("PV1", {1: "1", 2: "OP", 3: "Clinic 2", 19: "B-V-55", 44: "20261004093000"}, "#")])
    c = "\r".join([
        "MSH|^~\\&|LIS_C|LAB_C|UNIHL7|HUB|20261004101500||ORU^R01^ORU_R01|C555|P|2.4",
        _row("PID", {1: "1", 3: "C-55^^^LAB_C^PI", 5: "JANE MARIE DOE", 7: "19900214000000+0300", 8: "female", 19: "784000000000"}),
        _row("PV1", {1: "1", 2: "O", 3: "LAB", 19: "C-V-9"}),
        _row("OBR", {1: "1", 4: "BMP^Basic metabolic panel^L"}),
        _row("OBX", {1: "1", 2: "NM", 3: "2951-2^Sodium^LN", 5: "128", 6: "mmol/L", 8: "L", 11: "F"}),
        _row("OBX", {1: "2", 2: "NM", 3: "2823-3^Potassium^LN", 5: "6.8", 6: "mmol/L", 8: "HH", 11: "F"})])
    bad = "\r".join([
        "MSH#$~\\&#REG_B#HOSP_B#UNIHL7#HUB#20261004100000##ADT$A01#B778#P#2.3",
        _row("PID", {1: "1", 2: "", 5: "Roe, Sam", 7: "31/02/1985", 8: "7"}, "#"),
        _row("PV1", {1: "1", 2: "XX", 3: "Ward 1"}, "#")])
    return {"hospital_a_standard": a, "hospital_b_custom": b, "lab_c_results": c, "hospital_b_bad": bad}


class Gateway:
    """Normalize -> link -> FHIR, with counters. One instance serves any number of messages."""

    def __init__(self, profiles=None, out_dir=None):
        self.profiles = profiles or load_profiles()
        self.index = MasterIndex()
        self.out_dir = out_dir
        self.stats = {"received": 0, "accepted": 0, "rejected": 0, "byProfile": {}}

    def process(self, text):
        """Returns (ok, info, bundle_or_None). info has controlId, profile, notes or problems."""
        self.stats["received"] += 1
        try:
            canonical, notes = normalize(text, self.profiles)
        except NormalizeError as exc:
            self.stats["rejected"] += 1
            try:
                cid = Message(text).get("MSH-10")
            except NormalizeError:
                cid = ""
            return False, {"controlId": cid, "problems": exc.problems}, None
        person = self.index.link(canonical["patient"])
        bundle = to_fhir(canonical, person)
        self.stats["accepted"] += 1
        prof = canonical["source"]["profile"]
        self.stats["byProfile"][prof] = self.stats["byProfile"].get(prof, 0) + 1
        info = {"controlId": canonical["source"]["controlId"], "profile": prof, "notes": notes,
                "patientId": person["id"], "canonical": canonical}
        if self.out_dir:
            os.makedirs(self.out_dir, exist_ok=True)
            with open(os.path.join(self.out_dir, f"{info['controlId'] or 'message'}.json"), "w", encoding="utf-8") as fh:
                json.dump(bundle, fh, indent=2)
        return True, info, bundle


def main(argv):
    gw = Gateway()
    if len(argv) < 2:
        print(__doc__)
        return 1
    items = samples() if argv[1] == "samples" else {argv[1]: open(argv[1], encoding="utf-8", newline="").read()}
    for name, text in items.items():
        ok, info, bundle = gw.process(text)
        print(f"\n== {name}")
        if not ok:
            print("   REJECTED:", *info["problems"], sep="\n     - ")
            continue
        c = info["canonical"]
        p = c["patient"]
        print(f"   dialect: {info['profile']}   patient: {p['given'][0] if p['given'] else ''} {p['family']}, "
              f"born {p['birthDate']}, {p['sex']}   -> unified patient {info['patientId'][:8]}")
        kinds = [e["resource"]["resourceType"] for e in bundle["entry"]]
        print("   FHIR:", ", ".join(f"{k} x{kinds.count(k)}" for k in dict.fromkeys(kinds)))
        for n in info["notes"]:
            print("   note:", n)
    print(f"\nPatients in master index: {len(gw.index.people)} (from {gw.stats['accepted']} accepted messages)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
