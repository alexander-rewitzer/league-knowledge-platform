"""Central defaults preserve existing discovery and collection behavior."""

import inspect
from pathlib import Path
import unittest
from unittest.mock import patch

from league_knowledge_platform import (
    GoldConfig,
    DEFAULT_SOURCE_AUDIT_CONFIG, DeepSeekSourceAuditConfig,
    DEFAULT_SECTION_VALIDATION_CONFIG, DeepSeekSectionJudgeConfig,
    DEFAULT_EXTRACTION_CONFIG, DeepSeekExtractionConfig,
    DEFAULT_SEGMENTATION_CONFIG, DeepSeekSegmentationConfig,
    DEFAULT_SILVER_CONFIG,
    DEFAULT_BRONZE_CONFIG,
    DEFAULT_RANKING_CONFIG, DEFAULT_SELECTION_CONFIG, DEFAULT_TRANSCRIPT_CONFIG,
    DeepSeekSelectionConfig, YouTubeDiscovery,
)
from league_knowledge_platform import defaults, models


class DefaultsTests(unittest.TestCase):
    def test_service_defaults_are_discoverable_and_keep_existing_contracts(self):
        self.assertEqual(GoldConfig().root, Path(defaults.GOLD_ROOT).absolute())
        self.assertEqual(DEFAULT_SOURCE_AUDIT_CONFIG.root, defaults.SOURCE_KNOWLEDGE_ROOT)
        self.assertEqual(DEFAULT_SOURCE_AUDIT_CONFIG.max_semantic_rounds, 3)
        self.assertEqual(DEFAULT_SOURCE_AUDIT_CONFIG.audit_output_limit, defaults.SOURCE_AUDIT_OUTPUT_LIMIT)
        self.assertEqual(DEFAULT_SOURCE_AUDIT_CONFIG.validator_output_limit, defaults.SOURCE_VALIDATOR_OUTPUT_LIMIT)
        self.assertEqual(DeepSeekSourceAuditConfig().model, defaults.SOURCE_AUDIT_MODEL)
        self.assertEqual(DEFAULT_SECTION_VALIDATION_CONFIG.root, defaults.SECTION_KNOWLEDGE_ROOT)
        self.assertEqual(DEFAULT_SECTION_VALIDATION_CONFIG.max_semantic_rounds, 3)
        self.assertEqual(DeepSeekSectionJudgeConfig().model, defaults.SECTION_JUDGE_MODEL)
        self.assertEqual(DEFAULT_EXTRACTION_CONFIG.output_limit, defaults.EXTRACTION_OUTPUT_LIMIT)
        self.assertEqual(DeepSeekExtractionConfig().model, defaults.EXTRACTION_MODEL)
        self.assertEqual(DeepSeekExtractionConfig().max_response_bytes, defaults.EXTRACTION_MAX_RESPONSE_BYTES)
        self.assertEqual(DEFAULT_SEGMENTATION_CONFIG.root, defaults.SEGMENTATION_ROOT)
        self.assertEqual(DEFAULT_SEGMENTATION_CONFIG.max_attempts, defaults.SEGMENTATION_MAX_ATTEMPTS)
        self.assertEqual(DeepSeekSegmentationConfig().model, defaults.SEGMENTATION_MODEL)
        self.assertEqual(DEFAULT_SILVER_CONFIG.root, defaults.SILVER_ROOT)
        self.assertEqual(DEFAULT_BRONZE_CONFIG.root, defaults.BRONZE_ROOT)
        self.assertEqual(defaults.MIN_VIDEO_DURATION_SECONDS, 180)
        self.assertEqual(defaults.MAX_VIDEO_DURATION_SECONDS, 1800)
        self.assertEqual(defaults.RANKING_TOP_K, 3)
        self.assertEqual(defaults.UNSEEN_CANDIDATE_LIMIT, 20)
        self.assertEqual(defaults.MAX_TARGET_ATTEMPTS, 3)
        self.assertEqual(defaults.RUN_STATE_TIMEOUT_SECONDS, 5)
        self.assertEqual(models.UNSEEN_CANDIDATE_LIMIT, defaults.UNSEEN_CANDIDATE_LIMIT)
        self.assertEqual(defaults.DISCOVERY_PAGE_SIZE, 25)
        self.assertEqual(defaults.DISCOVERY_TIMEOUT_SECONDS, 15)
        parameters = inspect.signature(YouTubeDiscovery).parameters
        self.assertEqual(parameters["page_size"].default, defaults.DISCOVERY_PAGE_SIZE)
        self.assertEqual(parameters["timeout_seconds"].default, defaults.DISCOVERY_TIMEOUT_SECONDS)
        self.assertEqual(DEFAULT_RANKING_CONFIG.popularity_weight, defaults.POPULARITY_WEIGHT)
        self.assertEqual(DEFAULT_RANKING_CONFIG.recency_weight, defaults.RECENCY_WEIGHT)
        self.assertEqual(DEFAULT_RANKING_CONFIG.recency_half_life_days, defaults.RECENCY_HALF_LIFE_DAYS)
        self.assertEqual(DEFAULT_RANKING_CONFIG.score_precision, defaults.RANKING_SCORE_PRECISION)
        self.assertEqual(DEFAULT_TRANSCRIPT_CONFIG.connect_timeout_seconds,
                         defaults.TRANSCRIPT_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(DEFAULT_TRANSCRIPT_CONFIG.read_timeout_seconds,
                         defaults.TRANSCRIPT_READ_TIMEOUT_SECONDS)
        self.assertEqual(defaults.TRANSCRIPT_MAX_RETRIES, 0)
        self.assertEqual(defaults.TRANSCRIPT_ENGLISH_LOCALE_PREFERENCE, ("en", "en-us", "en-gb"))
        selection = DeepSeekSelectionConfig()
        self.assertEqual(selection.model, defaults.SELECTION_MODEL)
        self.assertEqual(selection.quality_output_limit, defaults.SELECTION_QUALITY_OUTPUT_LIMIT)
        self.assertEqual(selection.comparison_output_limit, defaults.SELECTION_COMPARISON_OUTPUT_LIMIT)
        self.assertEqual(selection.connect_timeout_seconds, defaults.SELECTION_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(selection.read_timeout_seconds, defaults.SELECTION_READ_TIMEOUT_SECONDS)
        self.assertEqual(selection.max_response_bytes, defaults.SELECTION_MAX_RESPONSE_BYTES)
        self.assertEqual(DEFAULT_SELECTION_CONFIG.max_attempts, defaults.SELECTION_MAX_ATTEMPTS)
        self.assertEqual(DEFAULT_SELECTION_CONFIG.retry_delays_seconds,
                         defaults.SELECTION_RETRY_DELAYS_SECONDS)

    def test_discovery_still_uses_runtime_key_with_central_nonsecret_defaults(self):
        class Transport:
            def __init__(self):
                self.calls = []

            def get_json(self, endpoint, params, timeout):
                self.calls.append((params, timeout))
                return {"items": []}

        transport = Transport()
        with patch.dict("os.environ", {"YOUTUBE_API_KEY": "offline-runtime-key"}):
            discovery = YouTubeDiscovery(transport=transport)
        target = models.ChampionTarget("Ahri", "103", "Ahri")
        discovery.discover_page(target)
        params, timeout = transport.calls[0]
        self.assertEqual(params["key"], "offline-runtime-key")
        self.assertEqual(params["maxResults"], str(defaults.DISCOVERY_PAGE_SIZE))
        self.assertEqual(timeout, defaults.DISCOVERY_TIMEOUT_SECONDS)
        self.assertFalse(any("KEY" in name or "SECRET" in name or "TOKEN" in name
                             for name in vars(defaults)))


if __name__ == "__main__":
    unittest.main()
