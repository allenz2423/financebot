import pytest

from src.services.transaction_evidence import (
    build_identity_queries,
    evaluate_merchant_search,
    merchant_identity_tokens,
    parse_merchant_descriptor,
    registry_trust_tier,
)


@pytest.mark.parametrize(
    ("raw", "candidate", "processors"),
    [
        ("AplPay WE ARE SISTERBROOKLYN", "we are sisterbrooklyn", ("aplpay",)),
        ("PAYPAL *FOO", "foo", ("paypal",)),
        ("SQ *FOO", "foo", ("sq",)),
        ("STRIPE *FOO", "foo", ("stripe",)),
        ("VENMO *FOO", "foo", ("venmo",)),
        ("GOOGLE PAY BAR", "bar", ("google", "pay")),
        ("GOOGLE ONE G.CO/HELPPAY#", "google one g co helppay", ()),
        ("Apple Store", "apple store", ()),
        ("PAYPAL *GOOGLE ONE", "google one", ("paypal",)),
        ("CLOVER* BAKERY", "bakery", ("clover",)),
        ("TST* XING FU TANG", "xing fu tang", ("tst",)),
        ("SP DANNAM", "dannam", ("sp",)),
        ("Restaurant Direct", "restaurant direct", ()),
    ],
)
def test_descriptor_separates_processor_from_candidate(raw, candidate, processors):
    descriptor = parse_merchant_descriptor(raw)
    assert descriptor.merchant_candidate == candidate
    assert descriptor.processor_tokens == processors
    assert not set(descriptor.processor_tokens) & set(merchant_identity_tokens(descriptor))


def test_descriptor_preserves_raw_and_version():
    descriptor = parse_merchant_descriptor("  AplPay   FOO  ")
    assert descriptor.raw == "AplPay FOO"
    assert descriptor.normalization_version == "merchant-v2"


def test_empty_descriptor_has_no_queries():
    assert build_identity_queries(parse_merchant_descriptor("")) == ()


def test_identity_queries_are_typed_and_exact_first():
    queries = build_identity_queries(parse_merchant_descriptor("AplPay FOO"))
    assert queries[0] == {"query": '"foo"', "kind": "exact_identity"}
    assert queries[1] == {"query": "foo", "kind": "candidate_identity"}


def test_processor_only_result_is_rejected():
    descriptor = parse_merchant_descriptor("AplPay WE ARE SISTERBROOKLYN")
    evidence = evaluate_merchant_search(descriptor, [{
        "title": "Apple Community: Apple Pay help",
        "snippet": "How do I understand my AplPay purchase descriptor?",
        "url": "https://discussions.apple.com/thread/1",
    }])
    assert evidence.accepted is False
    assert evidence.identity == "NO_IDENTITY"
    assert "processor_only_match" in evidence.reasons or "no_identity_phrase_or_strong_token_match" in evidence.reasons


def test_unrelated_result_is_rejected():
    descriptor = parse_merchant_descriptor("AplPay WE ARE SISTERBROOKLYN")
    evidence = evaluate_merchant_search(descriptor, [{
        "title": "Local Apple Pay support",
        "snippet": "Payment wallet documentation",
        "url": "https://example.com/apple-pay",
    }])
    assert not evidence.usable_for_identity


def test_exact_phrase_title_is_strong_identity():
    descriptor = parse_merchant_descriptor("AplPay WE ARE SISTERBROOKLYN")
    evidence = evaluate_merchant_search(descriptor, [{
        "title": "We Are Sisterbrooklyn grocery store",
        "snippet": "About We Are Sisterbrooklyn",
        "url": "https://wearesisterbrooklyn.example/about",
    }])
    assert evidence.identity == "STRONG_IDENTITY"
    assert evidence.usable_for_identity


def test_candidate_tokens_can_establish_identity_without_processor():
    descriptor = parse_merchant_descriptor("AplPay SISTERBROOKLYN")
    evidence = evaluate_merchant_search(descriptor, [{
        "title": "Sisterbrooklyn grocery",
        "snippet": "Sisterbrooklyn neighborhood market",
        "url": "https://sisterbrooklyn.example",
    }])
    assert evidence.usable_for_identity


def test_empty_results_are_no_identity():
    evidence = evaluate_merchant_search(parse_merchant_descriptor("AplPay FOO"), [])
    assert evidence.identity == "NO_IDENTITY"
    assert evidence.reasons == ("no_results",)


@pytest.mark.parametrize("source", ["auto_cache", "model", "imported", "heuristic_cleaner", ""])
def test_untrusted_registry_sources_cannot_bypass(source):
    assert registry_trust_tier(source, "registry_exact") == "UNTRUSTED"


@pytest.mark.parametrize("source", ["human_confirmed", "user_alias", "curated", "user"])
def test_trusted_registry_sources_can_bypass(source):
    assert registry_trust_tier(source, "registry_exact") == "TRUSTED"


@pytest.mark.parametrize("source", ["web_research", "research", "model_proposed"])
def test_suggested_registry_sources_are_not_authoritative(source):
    assert registry_trust_tier(source, "registry_exact") == "SUGGESTED"


def test_processor_tokens_have_no_identity_weight():
    descriptor = parse_merchant_descriptor("AplPay Apple Pay")
    assert merchant_identity_tokens(descriptor) == ()


def test_evidence_retains_accepted_result_metadata():
    result = {"title": "Foo Market", "snippet": "Foo Market grocery", "url": "https://foo.example"}
    evidence = evaluate_merchant_search(parse_merchant_descriptor("Foo Market"), [result])
    assert evidence.results[0]["url"] == result["url"]
    assert evidence.score > 0


def test_google_one_is_not_stripped_as_a_payment_processor():
    descriptor = parse_merchant_descriptor("GOOGLE ONE G.CO/HELPPAY#")
    assert descriptor.processor_tokens == ()
    assert descriptor.merchant_candidate.startswith("google one")


def test_apple_store_is_not_stripped_as_apple_pay():
    descriptor = parse_merchant_descriptor("Apple Store")
    assert descriptor.processor_tokens == ()
    assert descriptor.merchant_candidate == "apple store"
