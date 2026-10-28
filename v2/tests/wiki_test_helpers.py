"""Explicit review of artificial, assertion-controlled offline fixtures."""
def publish_reviewed(store, draft_id):
    store.review(draft_id, decision="approved", reviewer="offline-fixture", reviewer_type="agent",
                 note="Artificial source and expected claims verified by this test case.")
    return store.publish(draft_id)
