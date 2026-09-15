from personal_brain.retrieval.credentials import (
    contains_known_credentials,
    credential_spans,
    redact_known_credentials,
)


def test_redaction_preserves_length_and_masks_known_shapes():
    fake_key = "sk-proj-synthetic-credential-00000000000000000000"
    text = f"公开标记 before {fake_key} after"

    spans = credential_spans(text)
    redacted = redact_known_credentials(text)

    assert contains_known_credentials(text)
    assert spans
    assert fake_key not in redacted
    assert "公开标记" in redacted
    assert len(redacted) == len(text)
    assert len(redacted) - len(text) == 0


def test_assignment_and_bearer_shapes_are_detected_without_false_positive_control():
    assignment = "api_key=synthetic-assignment-credential-000000"
    bearer = "Bearer synthetic-bearer-credential-000000000000"
    ordinary = "这是普通说明，不包含凭据。"

    assert contains_known_credentials(assignment)
    assert contains_known_credentials(bearer)
    assert not contains_known_credentials(ordinary)
    assert redact_known_credentials(ordinary) == ordinary
