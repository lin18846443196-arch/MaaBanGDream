"""Recorded CN Lv27 identity must survive a global metadata update to Lv28."""
import copy
import json
import unittest
from unittest.mock import patch

from test_team_live import ROOT
from realtime.chart_repository import LocalChartRepository
from realtime.final_cover import FinalCoverResolver
from realtime.regional_difficulty import catalog_level_metadata, verified_level_variants
from realtime.song_identity import UNKNOWN_SONG_ID, identify_final_song
from realtime.vision_io import imread_unicode


DIGEST = "d51ae11b4602098c94302cedd06bfeb6d145ba0bce39aae9dd24e6538413ec82"
INCIDENT_HASH = "song-jacket-phash-v2-c518cbb43dfa4e31"
FIXTURES = ROOT / "tests/fixtures/cooperative-20261002"


class DokusoushusaRegionalTests(unittest.TestCase):
    def setUp(self):
        self.repo = LocalChartRepository(ROOT / "resource/charts")
        self.cover = imread_unicode(FIXTURES / "dokusoushusa-cover.png")
        self.assertIsNotNone(self.cover)
        self.fingerprint = identify_final_song(self.cover).song_id
        self.assertNotEqual(self.fingerprint, UNKNOWN_SONG_ID)

    def test_raw_recorded_cover_resolves_cn_and_global_level_to_same_chart(self):
        for level in (27, 28):
            with self.subTest(level=level):
                result = self.repo.resolve(self.fingerprint, "Expert", level=level,
                                           title="独創収差")
                self.assertIsNotNone(result.selection, result.reason)
                self.assertEqual(result.selection.bestdori_song_id, 442)
                self.assertEqual(result.selection.expected_notes, 905)
                self.assertEqual(result.selection.level, level)
                self.assertEqual(result.selection.path.name, "expert.json")

    def test_october2_fingerprint_and_noisy_title_resolve_cn_level(self):
        result = self.repo.resolve(INCIDENT_HASH, "Expert", level=27,
                                   title="独創収差👍👍🎊")
        self.assertIsNotNone(result.selection, result.reason)
        self.assertEqual(result.selection.bestdori_song_id, 442)
        self.assertEqual(result.selection.level, 27)

    def test_preparation_title_confirms_before_loading(self):
        result = self.repo.resolve(UNKNOWN_SONG_ID, "Expert", level=27,
                                   title="独創収差")
        self.assertIsNotNone(result.selection, result.reason)
        self.assertEqual(result.selection.bestdori_song_id, 442)

    def test_final_cover_stability_and_level_gate_still_apply(self):
        resolver = FinalCoverResolver(repository=self.repo, difficulty="Expert",
            observed_level=27, observed_title="独創収差👍👍🎊",
            observed_title_confidence=.7104621657303402)
        self.assertIsNone(resolver.observe(self.cover))
        result = resolver.observe(self.cover)
        self.assertIsNotNone(result, resolver.failure_reason)
        self.assertEqual(result.selection.bestdori_song_id, 442)
        self.assertEqual(result.selection.level, 27)

    def test_unverified_levels_are_rejected(self):
        for level in (26, 29):
            with self.subTest(level=level):
                result = self.repo.resolve(self.fingerprint, "Expert", level=level)
                self.assertIsNone(result.selection)
                self.assertIn("level", result.reason)

    def test_alias_is_bound_to_song_difficulty_and_exact_chart_digest(self):
        self.assertEqual(verified_level_variants(442, "Expert", DIGEST),
                         frozenset((27, 28)))
        for song_id, difficulty, digest in ((443, "expert", DIGEST),
                (442, "hard", DIGEST), (442, "special", DIGEST),
                (442, "expert", "f" * 64)):
            self.assertFalse(verified_level_variants(song_id, difficulty, digest))
        manifest = copy.deepcopy(self.repo._load_manifest())
        song = next(s for s in manifest["songs"] if s["bestdori_song_id"] == 442)
        song["difficulties"]["expert"]["chart_sha256"] = "f" * 64
        with patch.object(self.repo, "_load_manifest", return_value=manifest):
            result = self.repo.resolve(self.fingerprint, "Expert", level=27)
        self.assertIsNone(result.selection)
        self.assertIn("level", result.reason)

    def test_catalog_sync_retains_cn_level_without_changing_global_level(self):
        for server, expected in (("cn", 27), ("jp", 28)):
            metadata = catalog_level_metadata(bestdori_song_id=442, difficulty="expert",
                chart_sha256=DIGEST, source_level=28, jacket_server=server)
            self.assertEqual(metadata["level"], expected)
            self.assertEqual(metadata["source_level"], 28)
            self.assertEqual(metadata["regional_levels"], {"cn": 27})

    def test_original_cn_wrapper_remains_27_and_905_notes(self):
        payload = json.loads((ROOT / "resource/charts/bestdori/442/expert.json").read_text(
            encoding="utf-8"))
        self.assertEqual(payload["source"]["chart_sha256"], DIGEST)
        self.assertEqual(payload["song"]["bestdori_id"], 442)
        self.assertEqual(payload["difficulty"]["level"], 27)
        self.assertEqual(payload["difficulty"]["expected_notes"], 905)


if __name__ == "__main__":
    unittest.main()
