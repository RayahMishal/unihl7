import json
import os
import tempfile
import threading
import unittest

from unihl7 import Gateway, Message, NormalizeError, load_profiles, normalize, parse_ts, samples
import mllp


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.profiles = load_profiles()
        self.s = samples()

    def test_three_dialects_give_the_same_canonical_patient(self):
        people = []
        for key in ("hospital_a_standard", "hospital_b_custom", "lab_c_results"):
            canonical, _ = normalize(self.s[key], self.profiles)
            p = canonical["patient"]
            people.append((p["family"], p["given"][0], p["birthDate"], p["sex"], p["nationalId"]))
        self.assertEqual(len(set(people)), 1)
        self.assertEqual(people[0], ("Doe", "Jane", "1990-02-14", "female", "784000000000"))

    def test_custom_delimiters_and_z_segment(self):
        msg = Message(self.s["hospital_b_custom"])
        self.assertEqual((msg.f, msg.c), ("#", "$"))
        self.assertEqual(msg.get("ZPI-2.1"), "784000000000")

    def test_bad_message_lists_every_problem(self):
        with self.assertRaises(NormalizeError) as cm:
            normalize(self.s["hospital_b_bad"], self.profiles)
        text = " ".join(cm.exception.problems)
        for needle in ("identifier missing", "31/02/1985", "patient class 'XX'"):
            self.assertIn(needle, text)

    def test_unknown_sender_falls_back_with_note(self):
        text = self.s["hospital_a_standard"].replace("HOSP_A", "NEWHOSP")
        _, notes = normalize(text, self.profiles)
        self.assertTrue(any("unknown sender" in n for n in notes))

    def test_timestamps(self):
        self.assertEqual(parse_ts("20261004"), "2026-10-04")
        self.assertEqual(parse_ts("202610041015"), "2026-10-04T10:15:00+03:00")
        self.assertEqual(parse_ts("20261004101500+0100"), "2026-10-04T10:15:00+01:00")
        self.assertIsNone(parse_ts("20260231"))


class GatewayTests(unittest.TestCase):
    def test_master_index_merges_identifiers_and_builds_observations(self):
        gw = Gateway()
        s = samples()
        bundles = [gw.process(s[k])[2] for k in ("hospital_a_standard", "hospital_b_custom", "lab_c_results")]
        self.assertEqual(len(gw.index.people), 1)
        ids = {b["entry"][0]["resource"]["id"] for b in bundles}
        self.assertEqual(len(ids), 1)
        patient = bundles[-1]["entry"][0]["resource"]
        systems = {i["system"] for i in patient["identifier"]}
        self.assertTrue({"urn:unihl7:hosp_a", "urn:unihl7:hosp_b", "urn:unihl7:lab_c"} <= systems)
        obs = [e["resource"] for e in bundles[-1]["entry"] if e["resource"]["resourceType"] == "Observation"]
        k = next(o for o in obs if o["code"]["coding"][0]["display"] == "Potassium")
        self.assertEqual(k["valueQuantity"]["value"], 6.8)
        self.assertEqual(k["interpretation"][0]["coding"][0]["code"], "HH")
        self.assertEqual(k["code"]["coding"][0]["system"], "http://loinc.org")

    def test_reprocessing_is_idempotent(self):
        gw = Gateway()
        a = gw.process(samples()["hospital_a_standard"])[2]
        b = gw.process(samples()["hospital_a_standard"])[2]
        self.assertEqual(a, b)


class MllpTests(unittest.TestCase):
    def test_round_trip_ack_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = mllp.Server(port=0, gateway=Gateway(out_dir=tmp))
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                s = samples()
                good = mllp.send(s["hospital_b_custom"], port=port)
                bad = mllp.send(s["hospital_b_bad"], port=port)
                self.assertIn("MSA|AA|B777", good)
                self.assertIn("MSA|AE|B778", bad)
                self.assertIn("identifier missing", bad)
                self.assertTrue(os.path.exists(os.path.join(tmp, "B777.json")))
                self.assertFalse(os.path.exists(os.path.join(tmp, "B778.json")))
                with open(os.path.join(tmp, "B777.json")) as fh:
                    self.assertEqual(json.load(fh)["resourceType"], "Bundle")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
