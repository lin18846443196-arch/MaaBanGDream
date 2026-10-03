"""Recorded false jackets and the October 1 regional level mismatch."""
import copy
import unittest
from unittest.mock import patch

import numpy as np

from test_team_live import ROOT, frame
from realtime.chart_repository import LocalChartRepository
from realtime.final_cover import FinalCoverResolver
from realtime.regional_difficulty import catalog_level_metadata, verified_level_variants
from realtime.song_identity import (
    FINAL_SONG_JACKET_ROI, UNKNOWN_SONG_ID, fingerprint_jacket, identify_final_song,
)
from realtime.vision_io import imread_unicode

FIXTURES = ROOT / "tests/fixtures/cooperative-20261001-identity"
NON_BREATH_DIGEST = "329052dd5eeb691a7d90f304a832d3a50777858e2e2858ec2684a9c4ce85080b"
NON_BREATH_FINGERPRINT = "song-jacket-phash-v2-97b513d25c5c5a9c"


class FinalSongPageTests(unittest.TestCase):
    def image(self, name):
        image = imread_unicode(FIXTURES / name)
        self.assertIsNotNone(image, name)
        return image

    def test_member_cards_are_not_final_jackets(self):
        x, y, width, height = FINAL_SONG_JACKET_ROI
        for name in ("prepare-dont-say-lazy.png", "prepare-ai-pai-dancehall.png"):
            with self.subTest(name=name):
                image = self.image(name)
                # The old crop-only path yielded a usable hash for these cards.
                self.assertNotEqual(fingerprint_jacket(image[y:y+height, x:x+width]).song_id,
                                    UNKNOWN_SONG_ID)
                self.assertEqual(identify_final_song(image).song_id, UNKNOWN_SONG_ID)

    def test_recorded_jackets_keep_their_original_hashes_across_modes(self):
        x, y, width, height = FINAL_SONG_JACKET_ROI
        images = [self.image("cover-aokora-rhapsody.png"), frame("055.000"),
                  imread_unicode(ROOT / "tests/fixtures/team/northern-lights-cover.png")]
        for image in images:
            identity = identify_final_song(image)
            self.assertNotEqual(identity.song_id, UNKNOWN_SONG_ID)
            self.assertEqual(identity, fingerprint_jacket(image[y:y+height, x:x+width]))

    def test_resolver_waits_through_member_cards_for_the_actual_cover(self):
        resolver = FinalCoverResolver(repository=LocalChartRepository(ROOT / "resource/charts"),
            difficulty="expert", observed_level=26, observed_title='cYaGnnaYYllowb')
        for _ in range(3):
            self.assertIsNone(resolver.observe(self.image("prepare-dont-say-lazy.png")))
        cover = self.image("cover-aokora-rhapsody.png")
        self.assertIsNone(resolver.observe(cover))
        result = resolver.observe(cover)
        self.assertIsNotNone(result, resolver.last_reason)
        self.assertEqual(result.selection.bestdori_song_id, 418)

    def test_faded_preparation_and_stage_are_rejected(self):
        for name in ("prepare-fading-black.png", "stage-after-cover.png"):
            self.assertEqual(identify_final_song(self.image(name)).song_id, UNKNOWN_SONG_ID)

    def test_black_flanks_without_difficulty_badge_are_not_enough(self):
        image = self.image("cover-aokora-rhapsody.png")
        image[421:449, 600:690] = 0
        self.assertEqual(identify_final_song(image).song_id, UNKNOWN_SONG_ID)

    def test_invalid_and_blank_frames_are_unknown(self):
        for image in (None, object(), np.zeros((720, 1280), np.uint8),
                      np.zeros((448, 1280, 3), np.uint8),
                      np.zeros((720, 1280, 3), np.uint8)):
            self.assertEqual(identify_final_song(image).song_id, UNKNOWN_SONG_ID)


class NonBreathRegionalLevelTests(unittest.TestCase):
    def setUp(self):
        self.repo = LocalChartRepository(ROOT / "resource/charts")

    def test_recorded_cover_and_cn_level_resolve_correct_chart(self):
        result = self.repo.resolve(NON_BREATH_FINGERPRINT, "Expert", level=27,
                                   title="ノンブレス・オブリー")
        self.assertIsNotNone(result.selection, result.reason)
        self.assertEqual(result.selection.bestdori_song_id, 590)
        self.assertEqual(result.selection.level, 27)
        self.assertEqual(result.selection.expected_notes, 921)

    def test_preparation_title_resolves_cn_level_before_loading(self):
        result = self.repo.resolve(UNKNOWN_SONG_ID, "Expert", level=27,
                                   title="ノンブレス・オブリー")
        self.assertIsNotNone(result.selection, result.reason)
        self.assertEqual(result.selection.bestdori_song_id, 590)

    def test_unverified_level_still_fails(self):
        result = self.repo.resolve(NON_BREATH_FINGERPRINT, "Expert", level=28)
        self.assertIsNone(result.selection)
        self.assertIn("level", result.reason)

    def test_regional_exception_is_bound_to_song_difficulty_and_chart_hash(self):
        self.assertEqual(verified_level_variants(590, "Expert", NON_BREATH_DIGEST),
                         frozenset((26, 27)))
        for song_id, difficulty, digest in ((591, "expert", NON_BREATH_DIGEST),
                                            (590, "hard", NON_BREATH_DIGEST),
                                            (590, "expert", "f" * 64)):
            self.assertFalse(verified_level_variants(song_id, difficulty, digest))
        manifest = copy.deepcopy(self.repo._load_manifest())
        song = next(s for s in manifest["songs"] if s["bestdori_song_id"] == 590)
        song["difficulties"]["expert"].update(level=26, chart_sha256="f" * 64)
        with patch.object(self.repo, "_load_manifest", return_value=manifest):
            result = self.repo.resolve(NON_BREATH_FINGERPRINT, "Expert", level=27)
        self.assertIsNone(result.selection)

    def test_catalog_sync_retains_verified_cn_level(self):
        for server, expected in (("cn", 27), ("jp", 26)):
            metadata = catalog_level_metadata(bestdori_song_id=590, difficulty="expert",
                chart_sha256=NON_BREATH_DIGEST, source_level=26, jacket_server=server)
            self.assertEqual(metadata["level"], expected)
            self.assertEqual(metadata["source_level"], 26)
            self.assertEqual(metadata["regional_levels"], {"cn": 27})


if __name__ == "__main__":
    unittest.main()
