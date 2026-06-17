from nous.api.compaction import TokenEstimator


def test_estimate_message_does_not_inflate_on_image():
    est = TokenEstimator()
    big_b64 = "A" * 200_000  # ~150KB base64 blob
    img_msg = {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big_b64}},
        {"type": "text", "text": "what is this?"},
    ]}
    # naive stringification would be ~50K tokens; block-aware must be small
    assert est.estimate_message(img_msg) < 3000


def test_estimate_message_text_passthrough():
    est = TokenEstimator()
    assert est.estimate_message({"role": "user", "content": "hello"}) >= 1


def test_estimate_message_document_block():
    est = TokenEstimator()
    msg = {"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "X" * 100000}},
        {"type": "text", "text": "summarize"},
    ]}
    n = est.estimate_message(msg)
    assert 1000 < n < 20000  # per-page heuristic, not the base64 length


def test_estimate_messages_delegates_and_handles_mixed():
    est = TokenEstimator()
    msgs = [
        {"role": "user", "content": "plain string"},
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "Z" * 100000}},
            {"type": "text", "text": "hi"},
        ]},
    ]
    assert est.estimate_messages(msgs) < 4000
