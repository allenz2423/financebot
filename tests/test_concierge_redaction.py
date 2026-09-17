"""B0 redaction pipeline tests."""

import io

from src.services.concierge.redaction import (
    is_secret_field,
    redact_value,
    redact_payload,
    strip_otp_from_error,
    screenshot_region_from_field,
    redact_screenshot,
)


def test_is_secret_field():
    for t in ("password", "otp", "card_pan", "card_exp", "card_cvv", "captcha", "token"):
        assert is_secret_field(t)
    for t in ("text", "url"):
        assert not is_secret_field(t)


def test_redact_value_masks():
    masked = redact_value("password", "hunter2")
    assert "\u2022" in masked
    assert "hunter2" not in masked
    assert redact_value("card_pan", "378282246310005") == "\u2022\u2022\u2022\u2022 0005"
    assert redact_value("captcha", "ABCD") == "\u2022\u2022\u2022\u2022"
    assert redact_value("text", "me@example.com") == "me@example.com"


def test_redact_payload_labels_only():
    out = redact_payload({"card_pan|Card": "378282246310005", "otp|Code": "123456"})
    assert set(out) == {"Card", "Code"}
    assert "378282246310005" not in " ".join(out.values())
    assert out["Card"] == "\u2022\u2022\u2022\u2022 0005"
    assert "123456" not in " ".join(out.values())


def test_strip_otp_from_error():
    assert strip_otp_from_error("code 123456 invalid") == "code \u2022\u2022\u2022\u2022 invalid"
    assert strip_otp_from_error("no digits here") == "no digits here"
    assert strip_otp_from_error("long 123456789 run") == "long 123456789 run"


def test_screenshot_regions():
    pan = screenshot_region_from_field("card_pan")
    otp = screenshot_region_from_field("otp")
    exp = screenshot_region_from_field("card_exp")
    assert pan == [(0, 0, 260, 28)]
    assert otp == [(0, 0, 120, 28)]
    assert exp == [(0, 0, 90, 28)]


def test_redact_screenshot_returns_png_and_manifest():
    from PIL import Image

    img = Image.new("RGB", (400, 60), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()
    out, manifest = redact_screenshot(raw, [(10, 10, 260, 28)])
    assert out != raw
    assert out[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(manifest) == 1
    region = manifest[0]
    assert region["x"] == 10
    assert region["y"] == 10
    assert region["w"] == 260
    assert region["h"] == 28