"""
Tests for bead_on_wire.detect_branded_token (Phase 2c semantic co-reference
primitive).

Coverage:
  - empty / None / whitespace input -> None
  - no token match -> None
  - clean (low-noise) token match -> (token, False)
  - high-noise token match -> (token, True)
  - case insensitivity (input case doesn't matter)
  - multi-word tokens match as full phrase
  - Southwest-Fwy regression guard (must NOT match "southwest airlines")
  - airport tokens (iah, hou, hobby airport, george bush)
  - hotel tokens (marriott, hyatt, sheraton, westin)
  - airline full phrases ("american airlines", "southwest airlines")
  - apartment-name co-reference scenario ("The Enclave at Sienna")

Reviewer: Gemini iron-fist Phase 2c mandate, "Galleria cap" rule ratified.
"""

from bead_on_wire import detect_branded_token, _HIGH_NOISE_TOKENS


# ============================================================================
# Empty / null inputs
# ============================================================================

class TestEmptyInputs:
    def test_empty_string_returns_none(self):
        assert detect_branded_token("") is None

    def test_none_returns_none(self):
        assert detect_branded_token(None) is None

    def test_whitespace_only_returns_none(self):
        # "   ".lower() == "   ", no token substring match.
        assert detect_branded_token("   ") is None


# ============================================================================
# No-match cases
# ============================================================================

class TestNoMatch:
    def test_forum_park_dr_returns_park_high_noise(self):
        # "Park" is a standalone word in "Forum Park Dr" — word-boundary
        # regex correctly matches. The HIGH_NOISE flag tells consumer
        # to require secondary anchor before trusting (per audit: 104
        # production hits of "park", ALL road names).
        result = detect_branded_token("Forum Park Dr")
        assert result == ("park", True)

    def test_residential_park_address_returns_park_high_noise(self):
        # Same as test_forum_park_dr — full Houston address still has
        # "Park" as a standalone word that the regex correctly matches.
        # High-noise flag protects against false confidence.
        result = detect_branded_token("7623 Forum Park Dr, Houston, TX")
        assert result == ("park", True)

    def test_intersection_returns_none(self):
        assert detect_branded_token("Sienna Pkwy & Bees Passage") is None

    def test_non_branded_business_returns_none(self):
        # "Excel Dental" matches no branded token — _signal_poi_match Head 1
        # (direct fuzzy) handles this case.
        assert detect_branded_token("Missouri City Dentist - Excel Dental") is None


# ============================================================================
# Clean (low-noise) token matches
# ============================================================================

class TestCleanTokenMatches:
    def test_marriott_match(self):
        # "marriott" -> low-noise. Set iteration order may put "marquis"
        # (high-noise) first if "Marriott Marquis" is the input. We assert
        # the noise flag is consistent with the returned token.
        result = detect_branded_token("Marriott Houston Downtown")
        assert result == ("marriott", False)

    def test_hyatt_match(self):
        result = detect_branded_token("Hyatt Regency Downtown")
        assert result == ("hyatt", False)

    def test_iah_returns_match(self):
        # In "Spirit Airlines, IAH": both "spirit" (high-noise) and "iah"
        # (low-noise) are present. Set iteration order determines which
        # fires first. We assert SOMETHING fires and the noise flag is
        # internally consistent.
        result = detect_branded_token("Spirit Airlines, IAH")
        assert result is not None
        token, is_high_noise = result
        assert token in {"spirit", "iah"}
        assert is_high_noise == (token in _HIGH_NOISE_TOKENS)

    def test_iah_alone_is_low_noise(self):
        result = detect_branded_token("IAH")
        assert result == ("iah", False)

    def test_hobby_airport_full_phrase_match(self):
        result = detect_branded_token("William P. Hobby Airport")
        # "hobby airport" (low-noise) AND "airport" from POI_TYPE_MAP both
        # match. Either may fire first.
        assert result is not None
        token, _ = result
        assert token in {"hobby airport", "airport"}

    def test_george_bush_phrase_match(self):
        result = detect_branded_token("George Bush Intercontinental Airport")
        assert result is not None
        token, _ = result
        assert token in {"george bush", "airport"}

    def test_southwest_airlines_full_phrase_match(self):
        result = detect_branded_token("Southwest Airlines, Hobby Airport")
        assert result is not None
        token, _ = result
        assert token in {"southwest airlines", "hobby airport", "airport"}

    def test_american_airlines_full_phrase_match(self):
        # "american airlines" (low-noise) AND "terminal" (high-noise)
        # both present.
        result = detect_branded_token("American Airlines Terminal D")
        assert result is not None
        token, is_high_noise = result
        assert token in {"american airlines", "terminal"}
        assert is_high_noise == (token in _HIGH_NOISE_TOKENS)

    def test_jetblue_match(self):
        result = detect_branded_token("JetBlue check-in")
        assert result == ("jetblue", False)


# ============================================================================
# High-noise token matches (Galleria cap rule)
# ============================================================================

class TestHighNoiseTokenMatches:
    def test_galleria_alone_returns_clean_match(self):
        # Per 2026-05-05 audit: 3 production hits, ALL real Galleria
        # mall (The Galleria, Residence Inn Houston by The Galleria).
        # NOT high-noise.
        result = detect_branded_token("Galleria")
        assert result == ("galleria", False)

    def test_terminal_alone_returns_clean_match(self):
        # Per 2026-05-05 audit: 25 production hits, ALL real IAH airport
        # terminals (Terminal E, Terminal C). NOT high-noise.
        result = detect_branded_token("Terminal")
        assert result == ("terminal", False)

    def test_alaska_alone_returns_high_noise(self):
        result = detect_branded_token("Alaska")
        assert result == ("alaska", True)

    def test_spirit_alone_returns_clean_match(self):
        # Per 2026-05-05 audit: "Spirit, Houston, Texas" is a real
        # bare-airline-name dropoff pattern. NOT high-noise.
        result = detect_branded_token("Spirit")
        assert result == ("spirit", False)

    def test_delta_alone_returns_clean_match(self):
        # Per 2026-05-05 audit: 4 of 5 production hits are real airline
        # dropoffs ("Delta, Houston, Texas"). The 1 road-name case
        # ("Delta St & Marshall St") is caught by intersection regex
        # upstream and never reaches detect_branded_token.
        result = detect_branded_token("Delta")
        assert result == ("delta", False)

    def test_united_alone_returns_clean_match(self):
        # Per 2026-05-05 audit: 20 production hits, ALL real bare-
        # airline-name dropoffs. United Airlines is the dominant
        # dropoff-pattern token for IAH-bound passengers.
        result = detect_branded_token("United")
        assert result == ("united", False)

    def test_park_alone_returns_high_noise(self):
        # Per 2026-05-05 audit: 104 hits, ALL road names. Even bare
        # "Park" is treated as high-noise so the matcher requires a
        # secondary anchor before treating a park-token co-reference
        # as strong evidence.
        result = detect_branded_token("Park")
        assert result == ("park", True)

    def test_airport_alone_returns_high_noise(self):
        # Per 2026-05-05 audit: 18 hits, ALL road names. Real airport
        # offers fire "hobby airport" or "george bush" first via
        # longest-first regex ordering, so bare "airport" surviving as
        # the matched token always indicates a road-name false positive.
        result = detect_branded_token("Airport")
        assert result == ("airport", True)


# ============================================================================
# Case insensitivity
# ============================================================================

class TestCaseInsensitivity:
    def test_uppercase_input_matches(self):
        assert detect_branded_token("MARRIOTT") == ("marriott", False)

    def test_mixed_case_input_matches(self):
        result = detect_branded_token("MaRrIoTt HoUsToN")
        assert result == ("marriott", False)

    def test_token_returned_is_always_lowercase(self):
        for input_text in ["IAH", "Iah", "iah", "iAh"]:
            result = detect_branded_token(input_text)
            assert result is not None
            token, _ = result
            assert token == token.lower()


# ============================================================================
# Southwest-Fwy regression guard
# ============================================================================

class TestSouthwestFreewayRegression:
    """The bare word 'southwest' is NOT a token. Only 'southwest airlines'
    is. So 'Southwest Fwy, Houston' must NOT trigger a branded match — it
    should fall through to road-name classification."""

    def test_southwest_fwy_does_not_match_southwest_airlines(self):
        assert detect_branded_token("Southwest Fwy, Houston, TX") is None

    def test_southwest_fwy_at_richmond_does_not_match(self):
        assert detect_branded_token("Southwest Fwy at Richmond Ave") is None

    def test_southwest_alone_does_not_match(self):
        assert detect_branded_token("southwest") is None


# ============================================================================
# Apartment-name co-reference
# ============================================================================

class TestApartmentNameCoReference:
    """Apartment names aren't currently in _EXTENDED_POI_TOKENS — they'll
    be handled by Head 1 (direct fuzzy) in _signal_poi_match."""

    def test_the_enclave_at_sienna_no_branded_match(self):
        assert detect_branded_token("The Enclave at Sienna") is None

    def test_camden_apartments_no_branded_match(self):
        assert detect_branded_token("Camden Cypress Creek Apartments") is None


# ============================================================================
# High-noise set integrity
# ============================================================================

class TestHighNoiseSetIntegrity:
    def test_high_noise_set_is_frozen(self):
        # frozenset is immutable — guards against runtime mutation.
        assert isinstance(_HIGH_NOISE_TOKENS, frozenset)

    def test_high_noise_tokens_are_all_lowercase(self):
        # The substring check lowercases input, so tokens themselves must
        # be lowercase or they'll silently fail to match.
        for token in _HIGH_NOISE_TOKENS:
            assert token == token.lower(), (
                f"Token {token!r} is not lowercase"
            )

    def test_canonical_risky_tokens_present(self):
        # Updated 2026-05-05 from 2101-offer production audit. Old
        # assertions (galleria, terminal, spirit, delta, united) were
        # re-classified to clean — their hits are real venues/airlines.
        assert "park" in _HIGH_NOISE_TOKENS         # 104 road-name hits
        assert "airport" in _HIGH_NOISE_TOKENS      # 18 road-name hits
        assert "alaska" in _HIGH_NOISE_TOKENS       # 1 road-name hit

    def test_clean_tokens_NOT_in_high_noise(self):
        # Sanity check: known-safe tokens must not have leaked into the
        # high-noise set. Updated 2026-05-05 with audit-validated
        # additions (galleria, terminal, united, delta, frontier,
        # spirit) — formerly theoretical-noise, now production-clean.
        assert "marriott" not in _HIGH_NOISE_TOKENS
        assert "hyatt" not in _HIGH_NOISE_TOKENS
        assert "iah" not in _HIGH_NOISE_TOKENS
        assert "hou" not in _HIGH_NOISE_TOKENS
        assert "hobby airport" not in _HIGH_NOISE_TOKENS
        assert "southwest airlines" not in _HIGH_NOISE_TOKENS
        assert "american airlines" not in _HIGH_NOISE_TOKENS
        # Audit-cleared (2026-05-05): all production hits are real
        # venues/airlines, not common-word collisions.
        assert "galleria" not in _HIGH_NOISE_TOKENS    # real mall
        assert "terminal" not in _HIGH_NOISE_TOKENS    # real IAH terms
        assert "united" not in _HIGH_NOISE_TOKENS      # 20 real dropoffs
        assert "delta" not in _HIGH_NOISE_TOKENS       # 4/5 real
        assert "frontier" not in _HIGH_NOISE_TOKENS    # 3 real
        assert "spirit" not in _HIGH_NOISE_TOKENS      # 1 real


# ============================================================================
# Substring regression guard — Phase 2c.1.1 word-boundary fix
# ============================================================================
#
# Surfaced by 2c.1 smoke test against live address text (2026-05-05):
# substring matching false-positived "hou" in "Houston", "park" in
# "Forum Park", etc. Audit against 2000 offers showed 120 disagreements
# (92 hou, 26 park, 1 mall, 1 iah). Word-boundary fix eliminates 119/120
# unambiguously; the 1 ambiguous case ("Amazon IAH3") was matching for
# the wrong reason and is recovered by Phase 2c Head 1 fuzzy matching.

class TestSubstringRegressionGuard:
    """Word-boundary regex must reject substring-only matches."""

    def test_houston_does_NOT_match_hou(self):
        # "Houston" contains the letters h-o-u but "hou" is not a
        # standalone word inside it. Must return None.
        assert detect_branded_token("Houston, Texas") is None

    def test_houston_address_does_NOT_match(self):
        # Real Houston dropoff address from production audit.
        assert detect_branded_token("S Sam Houston Pkwy E, Houston, Texas") is None

    def test_forum_park_park_match_is_high_noise(self):
        # "Park" IS a standalone word in "Forum Park Dr" — word-boundary
        # regex correctly matches. Per audit, this is a road name not a
        # park venue, so the HIGH_NOISE flag fires to require secondary
        # anchor before treating as confident POI evidence.
        result = detect_branded_token("7623 Forum Park Dr, Houston, TX")
        assert result == ("park", True)

    def test_westpark_does_NOT_match(self):
        # Real Houston address from production audit.
        assert detect_branded_token("Westpark Dr, Houston, Texas") is None

    def test_briarpark_does_NOT_match(self):
        # "Briarpark" contains "park" as substring but not standalone.
        assert detect_branded_token("Briarpark Dr, Houston, Texas") is None

    def test_southwest_fwy_does_NOT_match(self):
        # Already covered in TestSouthwestFreewayRegression — included
        # here to document that under word-boundary rules, this is now
        # a regex-driven outcome rather than an absence-of-token outcome.
        assert detect_branded_token("Southwest Fwy, Houston, TX") is None

    def test_artechouse_does_NOT_match(self):
        # OCR-style false positive surfaced in audit ("Artechouse"
        # contains "hou"). Real input from offer_history.
        assert detect_branded_token("Artechouse, Houston, Texas") is None

    # -------- positive controls — these MUST still match -------------------

    def test_memorial_park_returns_high_noise(self):
        # "Memorial Park" — a real park venue. Token "park" matches
        # but the audit-driven HIGH_NOISE flag fires regardless of
        # whether the upstream context is a real park or a Park Dr road.
        # The matcher's secondary-anchor logic (Phase 2c) will use
        # absence/presence of street_number and POI type co-reference
        # to disambiguate downstream.
        result = detect_branded_token("Memorial Park")
        assert result == ("park", True)

    def test_iah_alone_DOES_match(self):
        # The 3-letter airport code IAH must still match as a standalone
        # word.
        result = detect_branded_token("IAH")
        assert result == ("iah", False)

    def test_hobby_airport_phrase_DOES_match(self):
        # Multi-word token still matches as a phrase.
        result = detect_branded_token("William P. Hobby Airport")
        assert result is not None
        token, _ = result
        assert token in {"hobby airport", "airport"}

    # -------- _contains_poi_token regression guard -------------------------

    def test_contains_poi_token_houston_returns_false(self):
        from bead_on_wire import _contains_poi_token
        # Latent bug pre-2c.1.1: returned True because "hou" substring.
        # Fixed: now returns False.
        assert _contains_poi_token("S Sam Houston Pkwy E") is False

    def test_contains_poi_token_forum_park_returns_true(self):
        # "Park" IS a standalone word in "7623 Forum Park Dr" — the
        # word-boundary regex correctly matches. _contains_poi_token
        # returns True; the HIGH_NOISE handling happens at the
        # detect_branded_token / consumer layer, not here.
        from bead_on_wire import _contains_poi_token
        assert _contains_poi_token("7623 Forum Park Dr") is True

    def test_contains_poi_token_iah_returns_true(self):
        # Positive control — short branded tokens must still fire.
        from bead_on_wire import _contains_poi_token
        assert _contains_poi_token("IAH") is True

    def test_contains_poi_token_marriott_returns_true(self):
        # Positive control.
        from bead_on_wire import _contains_poi_token
        assert _contains_poi_token("Marriott Marquis") is True
