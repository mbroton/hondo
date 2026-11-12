from hondo.runtime.serve import _build_ingress_app


def test_ingress_acks_only_after_the_listener_completes() -> None:
    # The durable-handoff ordering: Slack redelivers only un-acked Socket Mode
    # envelopes, and the socket adapter acks when dispatch returns — so the
    # listener (event append + dispatch) must run *before* the response. With
    # Bolt's default (False), a crash after the ACK loses the event forever.
    assert _build_ingress_app("xoxb-test").process_before_response is True
